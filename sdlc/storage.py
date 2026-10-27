"""Versioned SQLite initialization. Migration preserves immutable run history."""

import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path

VERSION = 2
LEGACY_PROJECT = "legacy-project"
PROJECT_SQL = """
CREATE TABLE IF NOT EXISTS projects (
    id TEXT PRIMARY KEY, name TEXT NOT NULL, name_key TEXT NOT NULL UNIQUE,
    description TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'active',
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL, operator TEXT NOT NULL,
    revision INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS project_events (
    id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id),
    action TEXT NOT NULL, operator TEXT NOT NULL, created_at TEXT NOT NULL,
    details_json TEXT NOT NULL
);
"""

SCHEMA = """
                CREATE TABLE IF NOT EXISTS runs (
                    id TEXT PRIMARY KEY, parent_run_id TEXT REFERENCES runs(id),
                    project_id TEXT NOT NULL REFERENCES projects(id),
                    created_at TEXT NOT NULL, operator TEXT NOT NULL, status TEXT NOT NULL,
                    input_json TEXT NOT NULL, config_json TEXT NOT NULL, prompt_json TEXT NOT NULL,
                    raw_output TEXT, validation_json TEXT, output_json TEXT,
                    error TEXT, latency_ms REAL, total_latency_ms REAL, usage_json TEXT,
                    revision INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS case_versions (
                    run_id TEXT NOT NULL REFERENCES runs(id), case_id TEXT NOT NULL,
                    version INTEGER NOT NULL, content_json TEXT NOT NULL,
                    created_at TEXT NOT NULL, operator TEXT NOT NULL,
                    PRIMARY KEY(run_id,case_id,version)
                );
                CREATE TABLE IF NOT EXISTS case_heads (
                    run_id TEXT NOT NULL, case_id TEXT NOT NULL, version INTEGER NOT NULL,
                    revision INTEGER NOT NULL, status TEXT NOT NULL,
                    PRIMARY KEY(run_id,case_id),
                    FOREIGN KEY(run_id,case_id,version) REFERENCES case_versions(run_id,case_id,version)
                );
                CREATE TABLE IF NOT EXISTS clarifications (
                    run_id TEXT NOT NULL REFERENCES runs(id), id TEXT NOT NULL,
                    content_json TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'open',
                    revision INTEGER NOT NULL DEFAULT 1, resolution_json TEXT,
                    PRIMARY KEY(run_id,id)
                );
                CREATE TABLE IF NOT EXISTS events (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT UNIQUE NOT NULL,
                    run_id TEXT NOT NULL REFERENCES runs(id), case_id TEXT, version INTEGER,
                    action TEXT NOT NULL, operator TEXT NOT NULL, created_at TEXT NOT NULL,
                    reason TEXT NOT NULL, before_status TEXT, after_status TEXT,
                    details_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS exports (
                    id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES runs(id),
                    format TEXT NOT NULL, filename TEXT NOT NULL, mime TEXT NOT NULL,
                    operator TEXT NOT NULL, created_at TEXT NOT NULL, content BLOB NOT NULL,
                    content_hash TEXT NOT NULL, snapshot_json TEXT NOT NULL, project_name TEXT
                );
                CREATE TABLE IF NOT EXISTS requests (
                    id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, result_json TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS events_run ON events(run_id,seq);

"""


def statements(db, script):
    for statement in script.split(';'):
        if statement.strip():
            db.execute(statement)


def initialize(db, db_path):
    version = db.execute('PRAGMA user_version').fetchone()[0]
    if version > VERSION:
        raise ValueError('数据库版本高于当前应用支持版本，请使用更新的应用。')
    if version == VERSION:
        return
    existing = bool(db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='runs'").fetchone())
    if existing:
        backup = Path(str(db_path) + '.before-projects-' + uuid.uuid4().hex[:8] + '.sqlite3')
        backup.touch(mode=0o600, exist_ok=False)
        with sqlite3.connect(backup) as target:
            db.backup(target)
        backup.chmod(0o600)
    db.execute('PRAGMA foreign_keys=OFF')
    db.execute('BEGIN IMMEDIATE')
    try:
        # Another initializer may have completed while this connection waited.
        if db.execute('PRAGMA user_version').fetchone()[0] == VERSION:
            db.commit()
            return
        statements(db, PROJECT_SQL)
        if existing:
            stamp = datetime.now(timezone.utc).isoformat()
            db.execute('INSERT INTO projects VALUES (?,?,?,?,?,?,?,?,?)',
                       (LEGACY_PROJECT, '历史项目', '历史项目', '项目管理上线前的全部运行与审核记录',
                        'active', stamp, stamp, 'migration', 1))
            db.execute('INSERT INTO project_events VALUES (?,?,?,?,?,?)',
                       (uuid.uuid4().hex, LEGACY_PROJECT, 'migration', 'migration', stamp,
                        '{"source_schema_version":1}'))
            run_sql = next(x for x in SCHEMA.split(';') if 'CREATE TABLE IF NOT EXISTS runs (' in x)
            db.execute(run_sql.replace('CREATE TABLE IF NOT EXISTS runs (', 'CREATE TABLE runs_v2 ('))
            columns = [row[1] for row in db.execute('PRAGMA table_info(runs)')]
            names = ','.join(columns)
            db.execute(f'INSERT INTO runs_v2 ({names},project_id) SELECT {names},? FROM runs', (LEGACY_PROJECT,))
            db.execute('DROP TABLE runs')
            db.execute('ALTER TABLE runs_v2 RENAME TO runs')
            db.execute('ALTER TABLE exports ADD COLUMN project_name TEXT')
        statements(db, SCHEMA)
        db.execute('CREATE INDEX IF NOT EXISTS runs_project_time ON runs(project_id,created_at DESC)')
        for table in ('case_versions', 'events', 'exports', 'project_events'):
            for action in ('UPDATE', 'DELETE'):
                db.execute(f"""CREATE TRIGGER IF NOT EXISTS immutable_{table}_{action}
                    BEFORE {action} ON {table} BEGIN
                    SELECT RAISE(ABORT, 'immutable history'); END""")
        db.execute("""CREATE TRIGGER IF NOT EXISTS immutable_run_project
            BEFORE UPDATE OF project_id ON runs WHEN NEW.project_id IS NOT OLD.project_id
            BEGIN SELECT RAISE(ABORT, 'run project is immutable'); END""")
        for action in ('INSERT', 'UPDATE OF parent_run_id'):
            suffix = 'insert' if action == 'INSERT' else 'update'
            db.execute(f"""CREATE TRIGGER IF NOT EXISTS parent_project_{suffix}
                BEFORE {action} ON runs WHEN NEW.parent_run_id IS NOT NULL AND NOT EXISTS
                (SELECT 1 FROM runs p WHERE p.id=NEW.parent_run_id AND p.project_id=NEW.project_id)
                BEGIN SELECT RAISE(ABORT, 'parent belongs to another project'); END""")
        db.execute("""CREATE TRIGGER IF NOT EXISTS no_project_delete BEFORE DELETE ON projects
            BEGIN SELECT RAISE(ABORT, 'archive projects instead'); END""")
        if db.execute('PRAGMA foreign_key_check').fetchall():
            raise ValueError('项目迁移的外键验证失败，迁移已回滚。')
        db.execute(f'PRAGMA user_version={VERSION}')
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.execute('PRAGMA foreign_keys=ON')
