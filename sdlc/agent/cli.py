"""CLI adapter to the same task/Agent application used by the web workspace."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path


def main(argv=None):
    parser = argparse.ArgumentParser(description='TraceCase 项目测试 Agent')
    parser.add_argument('--db', default=os.environ.get('SDLC_DB_PATH', 'data/sdlc.sqlite3'))
    parser.add_argument('--workspace', required=True)
    parser.add_argument('--project', required=True)
    commands = parser.add_subparsers(dest='command', required=True)
    commands.add_parser('list', help='列出当前项目的测试任务')
    new = commands.add_parser('new', help='从 Markdown 文件创建测试任务')
    new.add_argument('file', type=Path)
    new.add_argument('--goal', default='')
    new.add_argument('--scope', default='')
    new.add_argument('--parent')
    new.add_argument('--resource', action='append', dest='resource_ids', help='绑定项目共享资源 ID，可重复指定')
    for command in ('show', 'resume', 'sessions'):
        sub = commands.add_parser(command)
        sub.add_argument('task_id')
    send = commands.add_parser('send')
    send.add_argument('task_id')
    send.add_argument('message')
    args = parser.parse_args(argv)
    token = os.environ.get('TRACECASE_SESSION_TOKEN', '')
    if not token:
        parser.error('请在本机环境变量 TRACECASE_SESSION_TOKEN 配置有效会话，不要将令牌写进命令参数或文件。')
    from sdlc.testing_agent.service import TestingService
    from sdlc.testing_agent.worker import enqueue
    service = TestingService(args.db, token, args.workspace, args.project)
    try:
        if args.command == 'list':
            result = [{'id': t['id'], 'goal': t['goal'], 'status': t['status']} for t in service.list_tasks()]
        elif args.command == 'new':
            if args.file.suffix.lower() not in {'.md', '.markdown'} or args.file.stat().st_size > 102400:
                raise ValueError('需要 100 KiB 以内的 UTF-8 Markdown 文件。')
            task_id = service.create_task(args.file.read_text(encoding='utf-8'), args.goal, args.scope, args.parent, resource_ids=args.resource_ids)
            result = {'task_id': task_id, 'job_id': enqueue(service, task_id)}
        elif args.command == 'show':
            result = service.task_summary(args.task_id)
        elif args.command == 'sessions':
            task = service.get_task(args.task_id)
            result = {'task_id': args.task_id, 'session_id': task.get('session_id'),
                      'events': [{'action': e['action'], 'created_at': e['created_at']} for e in task['events']]}
        else:
            service.resume_task(args.task_id)
            result = {'job_id': enqueue(service, args.task_id, 'message' if args.command == 'send' else 'resume', getattr(args, 'message', ''))}
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (ValueError, RuntimeError) as exc:
        parser.exit(1, str(exc) + '\n')


if __name__ == '__main__':
    main()
