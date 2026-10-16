"""SQLite persistence and the additive migration from the local-only library."""
import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from threading import RLock

ROOT = Path(__file__).resolve().parent.parent
DATA = Path(os.environ.get('INVOICE_DATA_DIR', ROOT / 'data')).resolve()
LOCK = RLock()

PROJECT_SCHEMA = '''CREATE TABLE {table} (
 id TEXT PRIMARY KEY, name TEXT NOT NULL, note TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL,
 owner_id TEXT REFERENCES users(id), group_id TEXT REFERENCES groups(id))'''
QUOTA_SCHEMA = '''CREATE TABLE {table} (
 id TEXT PRIMARY KEY, month TEXT NOT NULL, category TEXT NOT NULL,
 amount_cents INTEGER NOT NULL CHECK(amount_cents > 0),
 owner_id TEXT REFERENCES users(id), group_id TEXT REFERENCES groups(id))'''
INVOICE_SCHEMA = '''CREATE TABLE {table} (
 id TEXT PRIMARY KEY, number TEXT NOT NULL DEFAULT '', seller TEXT NOT NULL DEFAULT '',
 buyer TEXT NOT NULL DEFAULT '', date TEXT NOT NULL DEFAULT '', category TEXT NOT NULL DEFAULT '其他',
 amount_cents INTEGER NOT NULL DEFAULT 0 CHECK(amount_cents >= 0),
 project_id TEXT REFERENCES projects(id), note TEXT NOT NULL DEFAULT '', verified INTEGER NOT NULL DEFAULT 0,
 file_name TEXT NOT NULL DEFAULT '', file_path TEXT NOT NULL DEFAULT '', file_hash TEXT,
 source TEXT NOT NULL DEFAULT '手动录入', review_note TEXT NOT NULL DEFAULT '', raw_text TEXT NOT NULL DEFAULT '',
 status TEXT NOT NULL DEFAULT 'available', batch_id TEXT REFERENCES batches(id), created_at TEXT NOT NULL,
 owner_id TEXT REFERENCES users(id))'''


def _columns(db, table):
    return [row['name'] for row in db.execute(f'PRAGMA table_info({table})')]


def _scoped_table(db, table, schema):
    columns = _columns(db, table)
    if not columns:
        db.execute(schema.format(table=table))
    elif 'owner_id' not in columns:
        # Rebuild to remove the old global UNIQUE constraints while retaining every row.
        replacement = table + '_scope_migration'
        db.execute(schema.format(table=replacement))
        names = ','.join(columns)
        db.execute(f'INSERT INTO {replacement} ({names}) SELECT {names} FROM {table}')
        db.execute(f'DROP TABLE {table}')
        db.execute(f'ALTER TABLE {replacement} RENAME TO {table}')


def initialize():
    DATA.mkdir(parents=True, exist_ok=True)
    (DATA / 'originals').mkdir(exist_ok=True)
    with LOCK, connection() as db:
        db.execute('PRAGMA journal_mode=WAL')
        # Foreign-key checking is restored after the transactional table rebuild.
        db.execute('PRAGMA foreign_keys=OFF')
        db.execute('BEGIN IMMEDIATE')
        statements = [
            '''CREATE TABLE IF NOT EXISTS users (
                id TEXT PRIMARY KEY, email TEXT NOT NULL UNIQUE, name TEXT NOT NULL,
                password_hash TEXT NOT NULL, created_at TEXT NOT NULL)''',
            '''CREATE TABLE IF NOT EXISTS sessions (
                token_hash TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                expires_at INTEGER NOT NULL)''',
            '''CREATE TABLE IF NOT EXISTS auth_attempts (
                bucket TEXT PRIMARY KEY, count INTEGER NOT NULL, window_start INTEGER NOT NULL)''',
            '''CREATE TABLE IF NOT EXISTS groups (
                id TEXT PRIMARY KEY, name TEXT NOT NULL, created_by TEXT NOT NULL REFERENCES users(id),
                created_at TEXT NOT NULL)''',
            '''CREATE TABLE IF NOT EXISTS group_members (
                group_id TEXT NOT NULL REFERENCES groups(id) ON DELETE CASCADE,
                user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                role TEXT NOT NULL CHECK(role IN ('owner','admin','member')), joined_at TEXT NOT NULL,
                PRIMARY KEY(group_id,user_id))''',
            '''CREATE TABLE IF NOT EXISTS group_invites (
                token_hash TEXT PRIMARY KEY, group_id TEXT NOT NULL REFERENCES groups(id) ON DELETE CASCADE,
                created_by TEXT NOT NULL REFERENCES users(id), expires_at INTEGER NOT NULL,
                max_uses INTEGER NOT NULL DEFAULT 1, uses INTEGER NOT NULL DEFAULT 0)''',
            '''CREATE TABLE IF NOT EXISTS batches (
                id TEXT PRIMARY KEY, name TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'draft',
                project_id TEXT REFERENCES projects(id), quota_id TEXT REFERENCES quotas(id),
                month TEXT NOT NULL, total_cents INTEGER NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                owner_id TEXT REFERENCES users(id), group_id TEXT REFERENCES groups(id))''',
        ]
        for statement in statements:
            db.execute(statement)
        for table, schema in [('projects', PROJECT_SCHEMA), ('quotas', QUOTA_SCHEMA), ('invoices', INVOICE_SCHEMA)]:
            _scoped_table(db, table, schema)
        if 'description' not in _columns(db, 'groups'):
            db.execute("ALTER TABLE groups ADD COLUMN description TEXT NOT NULL DEFAULT ''")
        batch_columns = _columns(db, 'batches')
        for column, reference in [('owner_id', 'users'), ('group_id', 'groups')]:
            if column not in batch_columns:
                db.execute(f'ALTER TABLE batches ADD COLUMN {column} TEXT REFERENCES {reference}(id)')
        db.execute('''CREATE TABLE IF NOT EXISTS batch_items (
            batch_id TEXT NOT NULL REFERENCES batches(id), invoice_id TEXT NOT NULL REFERENCES invoices(id),
            snapshot TEXT NOT NULL, PRIMARY KEY(batch_id,invoice_id))''')
        db.execute('''CREATE TABLE IF NOT EXISTS invoice_shares (
            invoice_id TEXT NOT NULL REFERENCES invoices(id) ON DELETE CASCADE,
            group_id TEXT NOT NULL REFERENCES groups(id) ON DELETE CASCADE,
            shared_by TEXT NOT NULL REFERENCES users(id), created_at TEXT NOT NULL,
            PRIMARY KEY(invoice_id,group_id))''')
        indexes = [
            "CREATE UNIQUE INDEX IF NOT EXISTS invoices_owner_number ON invoices(owner_id,number) WHERE number != ''",
            'CREATE UNIQUE INDEX IF NOT EXISTS invoices_owner_hash ON invoices(owner_id,file_hash) WHERE file_hash IS NOT NULL',
            'CREATE INDEX IF NOT EXISTS invoices_candidate ON invoices(status,verified,category,date)',
            'CREATE INDEX IF NOT EXISTS invoices_owner ON invoices(owner_id)',
            'CREATE INDEX IF NOT EXISTS invoice_shares_group ON invoice_shares(group_id)',
            'CREATE INDEX IF NOT EXISTS sessions_expiry ON sessions(expires_at)',
            'CREATE UNIQUE INDEX IF NOT EXISTS projects_personal_name ON projects(owner_id,name) WHERE group_id IS NULL',
            'CREATE UNIQUE INDEX IF NOT EXISTS projects_group_name ON projects(group_id,name) WHERE group_id IS NOT NULL',
            'CREATE UNIQUE INDEX IF NOT EXISTS quotas_personal_scope ON quotas(owner_id,month,category) WHERE group_id IS NULL',
            'CREATE UNIQUE INDEX IF NOT EXISTS quotas_group_scope ON quotas(group_id,month,category) WHERE group_id IS NOT NULL',
        ]
        db.execute('DROP INDEX IF EXISTS invoices_number')
        for statement in indexes:
            db.execute(statement)
        violations = db.execute('PRAGMA foreign_key_check').fetchall()
        if violations:
            raise RuntimeError('数据库迁移发现关联异常，已回滚，请保留原数据库检查')
        db.execute('PRAGMA user_version=2')
        db.commit()
        db.execute('PRAGMA foreign_keys=ON')


@contextmanager
def connection(write=False):
    db = sqlite3.connect(DATA / 'invoices.sqlite3', timeout=20)
    db.row_factory = sqlite3.Row
    db.execute('PRAGMA foreign_keys=ON')
    try:
        if write:
            db.execute('BEGIN IMMEDIATE')
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()
