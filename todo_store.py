"""Widget's own local SQLite store. Never opens QQ/WeChat databases.
Assistant updates use stable IDs and preserve completed/removed status.
"""
from __future__ import annotations
import argparse
from contextlib import contextmanager
from datetime import datetime
import json
from pathlib import Path
import re
import sqlite3
import uuid

from chatnotice import ROOT, project_path

APP_ID = 0x434E5431
DB_PATH = 'state/todos.sqlite3'


def normalize_due(value: str | None) -> str | None:
    if not value or not value.strip(): return None
    value = value.strip().replace(' ', 'T')
    if not re.fullmatch(r'\d{4}-\d{2}-\d{2}(?:T\d{2}:\d{2})?', value):
        raise ValueError('日期请填 YYYY-MM-DD 或 YYYY-MM-DD HH:MM，也可留空')
    datetime.fromisoformat(value)
    return value


class TodoStore:
    def __init__(self, path: Path | None = None):
        path = path or project_path(DB_PATH)
        self.relative = str(path.absolute().relative_to(ROOT))
        self.path = project_path(self.relative)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as db:
            app_id = db.execute('PRAGMA application_id').fetchone()[0]
            tables = db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
            if app_id not in (0, APP_ID) or (app_id == 0 and tables):
                raise ValueError('该数据库不属于待办组件，拒绝修改')
            if db.execute('PRAGMA user_version').fetchone()[0] > 1:
                raise ValueError('待办数据库版本较新，请不要用旧版程序修改')
            db.execute(f'PRAGMA application_id={APP_ID}')
            db.execute('PRAGMA user_version=1')
            db.execute('''CREATE TABLE IF NOT EXISTS tasks (
                id TEXT PRIMARY KEY, title TEXT NOT NULL, due TEXT, due_label TEXT NOT NULL DEFAULT '',
                note TEXT NOT NULL DEFAULT '', source TEXT NOT NULL DEFAULT '',
                optional INTEGER NOT NULL DEFAULT 0 CHECK(optional IN (0,1)),
                done INTEGER NOT NULL DEFAULT 0 CHECK(done IN (0,1)), done_at TEXT,
                removed INTEGER NOT NULL DEFAULT 0 CHECK(removed IN (0,1)),
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL)''')
            db.execute('CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY,value TEXT NOT NULL)')

    @contextmanager
    def connection(self):
        path = project_path(self.relative)  # Re-check that no junction appeared.
        db = sqlite3.connect(path, timeout=5)
        db.row_factory = sqlite3.Row
        try:
            with db: yield db
        finally: db.close()

    def upsert(self, title: str, *, id: str | None = None, due: str | None = None,
               due_label: str = '', note: str = '', source: str = '', optional: bool = False) -> str:
        if not isinstance(title, str) or not title.strip() or len(title) > 300:
            raise ValueError('事项标题不能为空，最多300字')
        if len(note) > 12000 or len(source) > 1000 or len(due_label) > 100:
            raise ValueError('待办说明过长')
        if not isinstance(optional, bool): raise ValueError('optional 必须是布尔值')
        id = id or 'manual:' + uuid.uuid4().hex
        if not isinstance(id, str) or not id or len(id) > 300: raise ValueError('待办 ID 无效')
        due = normalize_due(due)
        now = datetime.now().isoformat(timespec='seconds')
        with self.connection() as db:
            db.execute('''INSERT INTO tasks(id,title,due,due_label,note,source,optional,created_at,updated_at)
                VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET
                title=excluded.title,due=excluded.due,due_label=excluded.due_label,
                note=excluded.note,source=excluded.source,optional=excluded.optional,
                updated_at=excluded.updated_at''',
                (id,title.strip(),due,due_label,note,source,int(optional),now,now))
        return id

    def patch_fields(self, id: str, changes: dict, baseline: dict) -> dict:
        """Atomic field-level edit: never overwrite completion or another editor silently."""
        allowed={'title','due','due_label','note','optional'}
        if set(changes)-allowed: raise ValueError('不允许修改的待办字段')
        changes=dict(changes)
        if 'title' in changes:
            changes['title']=changes['title'].strip()
            if not changes['title'] or len(changes['title'])>300: raise ValueError('标题不能为空，最多300字')
        if 'due' in changes: changes['due']=normalize_due(changes['due'])
        if len(changes.get('note',''))>12000: raise ValueError('说明最多12000字')
        if len(changes.get('due_label',''))>100: raise ValueError('截止提示最多100字')
        if 'optional' in changes: changes['optional']=int(bool(changes['optional']))
        with self.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            record=db.execute('SELECT * FROM tasks WHERE id=?',(id,)).fetchone()
            if record is None or record['removed']: raise ValueError('此事项已移除，当前草稿保留在窗口中')
            for key,value in changes.items():
                if record[key]!=baseline.get(key) and record[key]!=value:
                    raise ValueError('此字段已在别处更新，未覆盖。请复制草稿后重新打开核对。')
            if changes:
                columns=','.join(key+'=?' for key in changes)
                db.execute('UPDATE tasks SET '+columns+',updated_at=? WHERE id=?',
                    (*changes.values(),datetime.now().isoformat(timespec='microseconds'),id))
            return dict(db.execute('SELECT * FROM tasks WHERE id=?',(id,)).fetchone())

    def tasks(self, *, done: bool | None = None, include_removed: bool = False) -> list[dict]:
        clauses, args = [], []
        if not include_removed: clauses.append('removed=0')
        if done is not None: clauses.append('done=?'); args.append(int(done))
        where = ' WHERE ' + ' AND '.join(clauses) if clauses else ''
        with self.connection() as db:
            return [dict(r) for r in db.execute('SELECT * FROM tasks' + where +
                ' ORDER BY done, due IS NULL, due, created_at, id', args)]

    def get(self, id: str) -> dict:
        with self.connection() as db:
            row = db.execute('SELECT * FROM tasks WHERE id=?', (id,)).fetchone()
            if row is None: raise ValueError('待办不存在')
            return dict(row)

    def mark(self, id: str, done: bool):
        now = datetime.now().isoformat(timespec='seconds')
        with self.connection() as db:
            cursor = db.execute('UPDATE tasks SET done=?,done_at=?,updated_at=? WHERE id=?',
                                (int(done),now if done else None,now,id))
            if cursor.rowcount != 1: raise ValueError('待办不存在')

    def remove(self, id: str, removed: bool = True):
        with self.connection() as db:
            cursor = db.execute('UPDATE tasks SET removed=?,updated_at=? WHERE id=?',
                                (int(removed),datetime.now().isoformat(timespec='seconds'),id))
            if cursor.rowcount != 1: raise ValueError('待办不存在')

    def setting(self, key: str, default=None):
        with self.connection() as db:
            row = db.execute('SELECT value FROM settings WHERE key=?', (key,)).fetchone()
            return json.loads(row[0]) if row else default

    def set_setting(self, key: str, value):
        with self.connection() as db:
            db.execute('INSERT INTO settings VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',
                       (key,json.dumps(value,ensure_ascii=False)))

    def seed_once(self):
        if self.setting('initial_tasks_loaded', False): return
        tasks = json.loads((ROOT/'initial-todos.json').read_text(encoding='utf-8'))['tasks']
        # Idempotent even if interrupted: existing completion/removal is preserved.
        for task in tasks: self.upsert(**task)
        self.set_setting('initial_tasks_loaded', True)


def main():
    parser = argparse.ArgumentParser(description='桌面待办：只操作项目自己的待办库')
    commands = parser.add_subparsers(dest='command', required=True)
    add = commands.add_parser('add', help='新增或更新；同一 ID 保留勾选/移除状态')
    add.add_argument('--id'); add.add_argument('--title', required=True)
    for field in ('due','due-label','note','source'): add.add_argument('--'+field)
    add.add_argument('--optional', action='store_true')
    listing = commands.add_parser('list'); listing.add_argument('--all', action='store_true')
    done = commands.add_parser('done'); done.add_argument('id'); done.add_argument('--undo', action='store_true')
    remove = commands.add_parser('remove'); remove.add_argument('id'); remove.add_argument('--restore', action='store_true')
    commands.add_parser('seed')
    args = parser.parse_args()
    store = TodoStore()
    if args.command == 'add':
        print(store.upsert(args.title,id=args.id,due=args.due,due_label=args.due_label or '',
                          note=args.note or '',source=args.source or '',optional=args.optional))
    elif args.command == 'list': print(json.dumps(store.tasks(include_removed=args.all),ensure_ascii=False,indent=2))
    elif args.command == 'done': store.mark(args.id,not args.undo)
    elif args.command == 'remove': store.remove(args.id,not args.restore)
    else: store.seed_once(); print('已放入已确认的待办；原勾选状态不变。')


if __name__ == '__main__': main()
