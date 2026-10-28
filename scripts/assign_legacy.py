"""Local maintainer-only ownership migration. No web endpoint, never assigns on registration."""
import argparse
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from sdlc.accounts import Accounts, uid  # noqa: E402
import json  # noqa: E402
import time  # noqa: E402


def assign(path, project_id, workspace_id):
    store = Accounts(path)
    with store.db(write=True) as db:
        project = db.execute('SELECT * FROM projects WHERE id=?', (project_id,)).fetchone()
        workspace = db.execute('SELECT * FROM workspaces WHERE id=?', (workspace_id,)).fetchone()
        if not project or not workspace or workspace['archived']:
            raise ValueError('项目或目标空间不存在，或目标空间已归档。')
        if db.execute('SELECT 1 FROM project_workspaces WHERE project_id=?', (project_id,)).fetchone():
            raise ValueError('项目已有归属，不能重分配。')
        key = workspace_id + ':' + project['name'].casefold()
        if db.execute('SELECT 1 FROM projects WHERE name_key=?', (key,)).fetchone():
            raise ValueError('目标空间已有同名项目。')
        db.execute('UPDATE projects SET name_key=? WHERE id=?', (key, project_id))
        db.execute('INSERT INTO project_workspaces VALUES (?,?)', (project_id, workspace_id))
        db.execute('INSERT INTO account_events VALUES (?,?,?,?,?,?,?)',
                   (uid(), workspace_id, 'local-maintainer', '本机维护者（归属迁移）', 'legacy_assigned', time.time(),
                    json.dumps({'project_id': project_id, 'historical_operators': 'self-reported, unchanged'})))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db', default='data/sdlc.sqlite3')
    parser.add_argument('--list', action='store_true', help='列出未归属项目和可选工作空间（仅本机）')
    parser.add_argument('--project')
    parser.add_argument('--workspace')
    args = parser.parse_args()
    if args.list:
        with Accounts(args.db).db() as db:
            print('未归属项目：')
            for row in db.execute('SELECT id,name FROM projects WHERE id NOT IN (SELECT project_id FROM project_workspaces)'):
                print(dict(row))
            print('目标空间：')
            for row in db.execute('SELECT w.id,w.kind,w.name,u.username FROM workspaces w JOIN users u ON u.id=w.owner_id WHERE w.archived=0'):
                print(dict(row))
        raise SystemExit(0)
    if not args.project or not args.workspace:
        parser.error('归属分配必须同时指定 --project 与 --workspace。')
    assign(args.db, args.project, args.workspace)
    print('归属迁移完成；历史身份标签与快照保持原样。')
