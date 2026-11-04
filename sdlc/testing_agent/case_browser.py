"""Authorized, live case queries; chat stores references rather than model copies."""
from __future__ import annotations

import json

from sdlc.service import ServiceError, dumps, identifier, now


STATUSES = {'all', 'approved', 'pending_review', 'returned'}


def list_cases(service, *, status='all', task_id=None, keyword='', page=1, page_size=5):
    if status not in STATUSES or type(page) is not int or page < 1 or type(page_size) is not int or not 1 <= page_size <= 20:
        raise ServiceError('用例查询条件或分页参数无效。')
    if not isinstance(keyword, str) or len(keyword) > 200:
        raise ServiceError('搜索文字不能超过200字符。')
    with service.connection() as db:
        clauses, args = ['r.project_id=?'], [service.project_id]
        if task_id:
            task = service._task(db, task_id)
            clauses.append('r.id=?')
            args.append(task['run_id'] or '')
        if status != 'all':
            clauses.append('h.status=?')
            args.append(status)
        if keyword:
            clauses.append("(instr(lower(h.case_id),lower(?))>0 OR instr(lower(json_extract(v.content_json,'$.title')),lower(?))>0)")
            args.extend([keyword, keyword])
        source = ''' FROM case_heads h JOIN runs r ON r.id=h.run_id
            JOIN case_versions v ON v.run_id=h.run_id AND v.case_id=h.case_id AND v.version=h.version
            WHERE ''' + ' AND '.join(clauses)
        total = db.execute('SELECT count(*)' + source, args).fetchone()[0]
        pages = max(1, (total + page_size - 1) // page_size)
        page = min(page, pages)
        rows = db.execute('SELECT h.*,v.content_json' + source +
                          ' ORDER BY r.created_at DESC,r.id,h.case_id LIMIT ? OFFSET ?',
                          [*args, page_size, (page - 1) * page_size]).fetchall()
        return {'total': total, 'page': page, 'pages': pages, 'page_size': page_size,
                'items': [{**{k: row[k] for k in ('run_id', 'case_id', 'status', 'version', 'revision')},
                           'content': json.loads(row['content_json'])} for row in rows]}


def get_case(service, run_id, case_id):
    # get_run rechecks run/project/workspace ownership and returns current heads.
    run = service.legacy.get_run(run_id)
    case = next((c for c in run['cases'] if c['case_id'] == case_id), None)
    if case is None:
        raise ServiceError('用例不存在或无权访问。')
    with service.connection() as db:
        task = db.execute('SELECT id,status FROM testing_agent_tasks WHERE run_id=? AND project_id=? AND workspace_id=? ORDER BY created_at DESC LIMIT 1',
                          (run_id, service.project_id, service.workspace_id)).fetchone()
    ids = set(case['content']['paragraph_ids'])
    return {'case': case, 'run_id': run_id, 'task_id': task['id'] if task else None,
            'task_status': task['status'] if task else None,
            'clarifications': [q for q in run['clarifications'] if q['content']['scope'] == 'run'
                or case_id in q['content']['affected_case_ids']
                or set(case['content']['requirement_ids']) & set(q['content']['requirement_ids'])],
            'paragraphs': [p for p in run['input']['paragraphs'] if p['id'] in ids]}


def publish_collection(service, chat_task_id, query=None, reference=None):
    """Publish before the next model call, including when that call later fails."""
    from sdlc.testing_agent.chat import ChatService
    ChatService(service)
    if reference:
        detail = get_case(service, reference['run_id'], reference['case_id'])
        result = {'total': 1, 'items': [detail['case']]}
    else:
        result = list_cases(service, **(query or {}))
    payload = {'query': query or {}, 'reference': reference,
               'shown_references': [{k: item[k] for k in ('run_id', 'case_id', 'version', 'revision')} for item in result['items']]}
    mid, timestamp = identifier(), now()
    with service.connection('view', True) as db:
        service._task(db, chat_task_id)
        db.execute('INSERT INTO testing_agent_chat_messages(id,task_id,actor_id,role,kind,content,state,created_at,updated_at,audience_user_id) VALUES(?,?,?,?,?,?,?,?,?,?)',
                   (mid, chat_task_id, 'agent', 'assistant', 'case_collection', dumps(payload), 'completed', timestamp, timestamp, service.user['id']))
    return {**{k: v for k, v in result.items() if k != 'items'}, 'message_id': mid,
            'displayed_in_chat': True,
            'cases': [{'run_id': c['run_id'], 'case_id': c['case_id'], 'version': c['version'],
                       'status': c['status'], 'title': c['content']['title']} for c in result['items']],
            'notice': '真实用例已在聊天卡片显示；分页和详情无需模型改写。不要把草稿当成批准结果。'}
