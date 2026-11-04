"""Narrow audit reads under the caller's current authenticated project scope.

The Agent receives decision records, never a database handle, global user search,
credentials, invitation secrets or unrelated account history.
"""
from __future__ import annotations

from sdlc.service import ServiceError


def _page(limit, offset):
    if type(limit) is not int or not 1 <= limit <= 100 or type(offset) is not int or offset < 0:
        raise ServiceError('审批记录分页参数无效。')


def _identifier(value):
    if not isinstance(value, str) or not 1 <= len(value) <= 128:
        raise ServiceError('查询对象标识无效。')
    return value


def case_review_history(service, run_id, case_id, *, limit=50, offset=0):
    """Return actual review decisions for a case in the current project only."""
    _identifier(run_id)
    _identifier(case_id)
    _page(limit, offset)
    with service.connection() as db:
        case = db.execute('''SELECT h.version,h.revision,h.status FROM case_heads h
            JOIN runs r ON r.id=h.run_id JOIN project_workspaces pw ON pw.project_id=r.project_id
            WHERE h.run_id=? AND h.case_id=? AND r.project_id=? AND pw.workspace_id=?''',
            (run_id, case_id, service.project_id, service.workspace_id)).fetchone()
        if not case:
            raise ServiceError('用例不存在或无权访问。')
        condition = "run_id=? AND case_id=? AND action IN ('approve','return','same_document_decision_inherited')"
        total = db.execute('SELECT COUNT(*) FROM events WHERE ' + condition, (run_id, case_id)).fetchone()[0]
        rows = db.execute('SELECT id,version,action,operator,created_at,reason,before_status,after_status '
                          'FROM events WHERE ' + condition + ' ORDER BY seq DESC LIMIT ? OFFSET ?',
                          (run_id, case_id, limit, offset)).fetchall()
        history = []
        for row in rows:
            # The immutable authenticated event snapshot is the proof of actor
            # identity. Legacy self-reported operator strings are not upgraded.
            actor = db.execute('''SELECT user_id,display_name FROM account_events
                WHERE workspace_id=? AND user_id=? AND json_extract(details_json,'$.event_id')=?
                ORDER BY created_at,id LIMIT 1''',
                (service.workspace_id, row['operator'], row['id'])).fetchone()
            history.append({
                'event_id': row['id'], 'case_version': row['version'], 'action': row['action'],
                'actor_id': actor['user_id'] if actor else None,
                'actor_name': actor['display_name'] if actor else None,
                'actor_verified': actor is not None,
                'recorded_operator': row['operator'], 'created_at': row['created_at'],
                'reason': row['reason'], 'before_status': row['before_status'],
                'after_status': row['after_status'],
                'is_current_version': row['version'] == case['version'],
            })
        return {'run_id': run_id, 'case_id': case_id, 'current_version': case['version'],
                'current_revision': case['revision'], 'current_status': case['status'],
                'total': total, 'limit': limit, 'offset': offset, 'events': history,
                'notice': '审批历史不代表当前版本仍然通过；请同时核对当前版本与状态。未验证的历史自报身份不能当作已认证账号。'}


def team_join_history(service, user_id=None, username=None, *, limit=50, offset=0):
    """Self may read own admission; managers may read this team's applicants.

    A supplied user identifier is resolved inside this team's requests. The
    function deliberately does not provide a global account lookup capability.
    """
    _page(limit, offset)
    if user_id is not None and username is not None:
        raise ServiceError('请选择用户标识或账号中的一种查询方式。')
    if user_id is not None:
        _identifier(user_id)
    if username is not None:
        _identifier(username)
    with service.connection() as db:
        actor = service.user
        if actor['kind'] != 'team':
            raise ServiceError('请先进入相关团队，再查询该团队的成员加入审批。')
        if user_id is None and username is None:
            user_id = actor['id']
        own = user_id == actor['id'] or (username is not None and username == actor['username'])
        if not own and 'manage' not in actor['permissions']:
            raise ServiceError('仅团队管理者可查看其他成员的加入审批；你可以查询自己的申请。')
        selector = 'u.id=?' if user_id is not None else 'u.username=?'
        value = user_id if user_id is not None else username
        target = db.execute('''SELECT u.id,u.username,u.display_name FROM join_requests j
            JOIN users u ON u.id=j.user_id WHERE j.workspace_id=? AND ''' + selector + ' LIMIT 1',
            (service.workspace_id, value)).fetchone()
        if not target:
            return {'workspace_id': service.workspace_id, 'subject': None,
                    'total': 0, 'limit': limit, 'offset': offset, 'requests': [],
                    'notice': '当前团队没有该对象的加入申请记录；不能据此推断其他团队或账号的信息。'}
        total = db.execute('SELECT COUNT(*) FROM join_requests WHERE workspace_id=? AND user_id=?',
                           (service.workspace_id, target['id'])).fetchone()[0]
        rows = db.execute('''SELECT id,status,created_at,decided_by,decided_at FROM join_requests
            WHERE workspace_id=? AND user_id=? ORDER BY created_at DESC,id LIMIT ? OFFSET ?''',
            (service.workspace_id, target['id'], limit, offset)).fetchall()
        requests = []
        for row in rows:
            decision = db.execute('''SELECT user_id,display_name FROM account_events
                WHERE workspace_id=? AND action='join_decided' AND user_id=?
                AND json_extract(details_json,'$.request_id')=? ORDER BY created_at,id LIMIT 1''',
                (service.workspace_id, row['decided_by'], row['id'])).fetchone()
            requests.append({'request_id': row['id'], 'status': row['status'],
                             'created_at': row['created_at'], 'decided_at': row['decided_at'],
                             'decision_actor_id': decision['user_id'] if decision else None,
                             'decision_actor_name': decision['display_name'] if decision else None,
                             'actor_verified': decision is not None})
        return {'workspace_id': service.workspace_id, 'subject': dict(target), 'total': total,
                'limit': limit, 'offset': offset, 'requests': requests,
                'notice': '仅当前团队加入申请的真实审批历史；不包含申请说明、邀请码、其他团队或个人空间资料。'}
