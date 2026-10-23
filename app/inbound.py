"""Domain receipt addresses and a signed, durable raw-email ingress.

Only the trusted envelope recipient selects the destination. Message headers never
participate in routing. Run a single application process for this local queue.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import os
from pathlib import Path
import re
import sqlite3
from threading import Event, Lock, Thread
import time
from typing import Callable
import uuid

from fastapi import APIRouter, Depends, HTTPException, Request

from . import db as db_store
from .auth import require_user
from .db import LOCK, connection
from .mailbox import MAX_MAIL_BYTES, _import_error, iter_message_attachments
from .permissions import require_group
from .mail_original import original_response

router = APIRouter(tags=['domain receipts'])
RESERVED_PREFIXES = {'postmaster', 'abuse', 'admin', 'administrator', 'root',
                     'hostmaster', 'webmaster', 'mailer-daemon', 'noreply', 'no-reply'}
_importer: Callable | None = None
_processing = Lock()
_stop = Event()
_wake = Event()
_worker: Thread | None = None
MAIL_RETENTION = timedelta(days=30)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


def _domain() -> str:
    domain = os.environ.get('INVOICE_RECEIPT_DOMAIN', '').strip().lower()
    if not domain:
        return ''
    try:
        domain = domain.encode('idna').decode()
    except UnicodeError:
        return ''
    labels = domain.split('.')
    if len(domain) > 253 or len(labels) < 2 or any(
        not re.fullmatch(r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?', label)
        for label in labels
    ):
        return ''
    return domain


def _secret() -> str:
    value = os.environ.get('INVOICE_INBOUND_SECRET', '')
    return value if len(value) >= 32 else ''


def configured() -> bool:
    return bool(_domain() and _secret())


def configure_importer(callback: Callable | None) -> None:
    global _importer
    _importer = callback


def _raw_path(delivery_id: str) -> Path:
    return db_store.DATA / 'inbound' / f'{delivery_id}.eml'


def _retention_cutoff() -> str:
    return (datetime.now(timezone.utc) - MAIL_RETENTION).isoformat(timespec='seconds')


def _original_available(row) -> bool:
    return bool(row['raw_path'] and row['received_at'] > _retention_cutoff()
                and _raw_path(row['id']).is_file())


def _purge_expired_deliveries() -> None:
    with LOCK, connection(write=True) as db:
        rows = db.execute("""SELECT id FROM inbound_deliveries WHERE received_at<=?
                          ORDER BY received_at,rowid LIMIT 50""", (_retention_cutoff(),)).fetchall()
        for row in rows:
            try:
                _raw_path(row['id']).unlink(missing_ok=True)
            except OSError:
                continue  # Keep the record for cleanup retry without blocking new receipts.
            # Attachment results cascade; imported invoices and their originals are independent.
            db.execute('DELETE FROM inbound_deliveries WHERE id=?', (row['id'],))


def initialize_inbound() -> None:
    directory = db_store.DATA / 'inbound'
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    directory.chmod(0o700)
    with LOCK, connection(write=True) as db:
        db.execute('''CREATE TABLE IF NOT EXISTS receipt_addresses (
            id TEXT PRIMARY KEY, prefix TEXT NOT NULL, domain TEXT NOT NULL,
            address TEXT NOT NULL UNIQUE, name TEXT NOT NULL,
            owner_id TEXT NOT NULL REFERENCES users(id), group_id TEXT REFERENCES groups(id),
            enabled INTEGER NOT NULL DEFAULT 1 CHECK(enabled IN (0,1)),
            created_at TEXT NOT NULL, last_received TEXT NOT NULL DEFAULT '',
            UNIQUE(domain,prefix))''')
        db.execute('''CREATE TABLE IF NOT EXISTS inbound_deliveries (
            id TEXT PRIMARY KEY, address_id TEXT NOT NULL REFERENCES receipt_addresses(id),
            raw_hash TEXT NOT NULL, raw_path TEXT NOT NULL, received_at TEXT NOT NULL,
            status TEXT NOT NULL CHECK(status IN ('queued','processing','completed','failed')),
            UNIQUE(address_id,raw_hash))''')
        db.execute('''CREATE TABLE IF NOT EXISTS inbound_results (
            delivery_id TEXT NOT NULL REFERENCES inbound_deliveries(id) ON DELETE CASCADE,
            attachment_index TEXT NOT NULL, filename TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL CHECK(status IN ('imported','duplicate','ignored','failed')),
            error TEXT NOT NULL DEFAULT '', invoice_id TEXT,
            PRIMARY KEY(delivery_id,attachment_index))''')
        if 'deleted_at' not in [r['name'] for r in db.execute('PRAGMA table_info(receipt_addresses)')]:
            db.execute("ALTER TABLE receipt_addresses ADD COLUMN deleted_at TEXT NOT NULL DEFAULT ''")
        if 'group_id' not in [r['name'] for r in db.execute('PRAGMA table_info(inbound_deliveries)')]:
            db.execute('ALTER TABLE inbound_deliveries ADD COLUMN group_id TEXT')
            db.execute('UPDATE inbound_deliveries SET group_id=(SELECT group_id FROM receipt_addresses a WHERE a.id=address_id)')
        db.execute('CREATE INDEX IF NOT EXISTS inbound_pending ON inbound_deliveries(status,received_at)')
        db.execute('CREATE INDEX IF NOT EXISTS inbound_received ON inbound_deliveries(received_at)')
        db.execute('CREATE INDEX IF NOT EXISTS receipt_address_owner ON receipt_addresses(owner_id,group_id)')
        # A crash can leave a claimed delivery unfinished; per-attachment results make replay safe.
        db.execute("UPDATE inbound_deliveries SET status='queued' WHERE status='processing'")


def _can_manage(db, address, user_id: str) -> bool:
    return address['owner_id'] == user_id and not address['deleted_at']


def _managed(db, address_id: str, user_id: str) -> dict:
    row = db.execute('SELECT * FROM receipt_addresses WHERE id=?', (address_id,)).fetchone()
    if row is None or not _can_manage(db, row, user_id):
        raise HTTPException(404, '收票地址不存在或无管理权限')
    return dict(row)


def _public(db, row, user_id: str) -> dict:
    fields = ('id', 'address', 'prefix', 'name', 'owner_id', 'group_id', 'last_received')
    return {**{field: row[field] for field in fields}, 'enabled': bool(row['enabled']),
            'can_manage': _can_manage(db, row, user_id)}


def _prefix(value) -> str:
    if not isinstance(value, str):
        raise HTTPException(400, '请填写收票地址前缀')
    value = value.strip().lower()
    if not re.fullmatch(r'[a-z0-9](?:[a-z0-9._-]{0,62}[a-z0-9])?', value) or '..' in value:
        raise HTTPException(400, '前缀须为 1 至 64 个字母、数字、点、下划线或短横线，首尾为字母或数字，不能包含连续点')
    if value in RESERVED_PREFIXES:
        raise HTTPException(400, '此前缀为系统保留名称，请选择其他名称')
    return value


@router.get('/api/receipt-addresses')
def list_addresses(user=Depends(require_user)):
    with connection() as db:
        rows = db.execute("SELECT * FROM receipt_addresses WHERE owner_id=? AND deleted_at='' ORDER BY created_at,id", (user['id'],)).fetchall()
        return {'domain': _domain(), 'configured': configured(),
                'addresses': [_public(db, row, user['id']) for row in rows]}


@router.post('/api/receipt-addresses')
def create_address(values: dict, user=Depends(require_user)):
    domain = _domain()
    if not domain:
        raise HTTPException(503, '服务端尚未配置有效的收票域名')
    prefix = _prefix(values.get('prefix'))
    name = values.get('name', '')
    if not isinstance(name, str) or not name.strip() or len(name) > 100 or any(ord(c) < 32 for c in name):
        raise HTTPException(400, '请填写 1 至 100 字的地址名称')
    group_id = values.get('group_id') or None
    if group_id is not None and not isinstance(group_id, str):
        raise HTTPException(400, '群组编号格式不正确')
    row = {'id': uuid.uuid4().hex, 'prefix': prefix, 'domain': domain,
           'address': f'{prefix}@{domain}', 'name': name.strip(), 'owner_id': user['id'],
           'group_id': group_id, 'enabled': 1, 'created_at': _now(), 'last_received': '', 'deleted_at': ''}
    with LOCK, connection(write=True) as db:
        if group_id:
            require_group(db, user['id'], group_id)
        try:
            fields = ','.join(row)
            db.execute(f"INSERT INTO receipt_addresses ({fields}) VALUES ({','.join('?' for _ in row)})", list(row.values()))
        except sqlite3.IntegrityError:
            raise HTTPException(409, '该收票地址已被占用；停用地址也会保留原归属') from None
        return _public(db, row, user['id'])


@router.patch('/api/receipt-addresses/{address_id}')
def update_address(address_id: str, values: dict, user=Depends(require_user)):
    if not values or set(values) - {'name', 'group_id', 'enabled'}:
        raise HTTPException(400, '只能修改名称、归属及启用状态')
    with LOCK, connection(write=True) as db:
        row = _managed(db, address_id, user['id'])
        if 'name' in values:
            name = values['name']
            if not isinstance(name, str) or not name.strip() or len(name) > 100 or any(ord(c) < 32 for c in name):
                raise HTTPException(400, '请填写 1 至 100 字的地址名称')
            row['name'] = name.strip()
        if 'group_id' in values:
            row['group_id'] = values['group_id'] or None
        if 'enabled' in values:
            if type(values['enabled']) is not bool:
                raise HTTPException(400, '启用状态格式不正确')
            row['enabled'] = values['enabled']
        if row['group_id']:
            require_group(db, user['id'], row['group_id'])
        db.execute('UPDATE receipt_addresses SET name=?,group_id=?,enabled=? WHERE id=?',
                   (row['name'], row['group_id'], int(row['enabled']), address_id))
        return _public(db, row, user['id'])


@router.delete('/api/receipt-addresses/{address_id}')
def delete_address(address_id: str, user=Depends(require_user)):
    with LOCK, connection(write=True) as db:
        _managed(db, address_id, user['id'])
        # Reserve the address permanently so future invoices cannot reach another owner.
        # Accepted deliveries finish with their original destination.
        db.execute('UPDATE receipt_addresses SET enabled=0,deleted_at=? WHERE id=?', (_now(), address_id))
    return {'ok': True}


@router.get('/api/receipt-addresses/{address_id}/deliveries')
def list_deliveries(address_id: str, user=Depends(require_user)):
    with connection() as db:
        _managed(db, address_id, user['id'])
        deliveries = []
        for row in db.execute('''SELECT id,received_at,status,raw_path FROM inbound_deliveries
                             WHERE address_id=? AND received_at>? ORDER BY received_at DESC,rowid DESC LIMIT 50''',
                              (address_id, _retention_cutoff())):
            results = [dict(result) for result in db.execute('''SELECT filename,status,error,invoice_id
                       FROM inbound_results WHERE delivery_id=? ORDER BY rowid''', (row['id'],))]
            deliveries.append({'id': row['id'], 'received_at': row['received_at'], 'status': row['status'],
                               'original_available': _original_available(row), 'results': results})
        return {'deliveries': deliveries}


@router.get('/api/receipt-addresses/{address_id}/deliveries/{delivery_id}/original')
def delivery_original(address_id: str, delivery_id: str, download: bool = False, user=Depends(require_user)):
    with LOCK, connection() as db:
        _managed(db, address_id, user['id'])
        row = db.execute('SELECT * FROM inbound_deliveries WHERE id=? AND address_id=?',
                         (delivery_id, address_id)).fetchone()
        if row is None:
            raise HTTPException(404, '收票记录不存在')
        if row['received_at'] <= _retention_cutoff():
            raise HTTPException(410, '邮件及收票记录仅保留 30 天，该邮件已到期')
        if not _original_available(row):
            raise HTTPException(410, '原始邮件已清理或不可用；旧版处理完成的邮件无法补回')
        content = _raw_path(row['id']).read_bytes()
    return original_response(content, download)


@router.post('/api/receipt-addresses/{address_id}/retry')
def retry_delivery(address_id: str, values: dict, user=Depends(require_user)):
    delivery_id = values.get('delivery_id')
    if not isinstance(delivery_id, str):
        raise HTTPException(400, '请指定要重试的收票记录')
    with LOCK, connection(write=True) as db:
        address = _managed(db, address_id, user['id'])
        if address['group_id']:
            require_group(db, address['owner_id'], address['group_id'])
        row = db.execute('SELECT * FROM inbound_deliveries WHERE id=? AND address_id=?',
                         (delivery_id, address_id)).fetchone()
        if row is None:
            raise HTTPException(404, '收票记录不存在')
        if row['received_at'] <= _retention_cutoff():
            raise HTTPException(410, '邮件及收票记录仅保留 30 天，该邮件已到期')
        if row['status'] != 'failed':
            raise HTTPException(409, '只有失败的收票记录可以重试')
        if not row['raw_path'] or not _raw_path(delivery_id).is_file():
            raise HTTPException(409, '原始邮件已不可用，请重新发送附件')
        db.execute("UPDATE inbound_deliveries SET status='queued' WHERE id=?", (delivery_id,))
    _wake.set()
    return {'id': delivery_id, 'status': 'queued'}


@router.post('/api/inbound/email', status_code=202)
async def receive_email(request: Request):
    """Service-to-service endpoint; it must not require browser cookies or CSRF headers."""
    domain, secret = _domain(), _secret()
    if not domain or not secret:
        raise HTTPException(503, '域名收信入口尚未配置')
    timestamp = request.headers.get('x-invoice-timestamp', '')
    recipient = request.headers.get('x-invoice-recipient', '')
    signature = request.headers.get('x-invoice-signature', '')
    if (not re.fullmatch(r'\d{1,12}', timestamp)
            or abs(time.time() - int(timestamp)) > 300
            or not re.fullmatch(r'[0-9a-fA-F]{64}', signature)
            or len(recipient) > 320 or '\n' in recipient or '\r' in recipient):
        raise HTTPException(401, '收信签名无效或已过期')
    length = request.headers.get('content-length', '')
    if length.isdigit() and int(length) > MAX_MAIL_BYTES:
        raise HTTPException(413, '单封原始邮件不得超过 20 MB')
    content = bytearray()
    async for part in request.stream():
        if len(content) + len(part) > MAX_MAIL_BYTES:
            raise HTTPException(413, '单封原始邮件不得超过 20 MB')
        content.extend(part)
    digest = hashlib.sha256(content).hexdigest()
    signing_input = f'{timestamp}\n{recipient}\n{digest}'.encode()
    expected = hmac.new(secret.encode(), signing_input, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(signature.lower(), expected):
        raise HTTPException(401, '收信签名无效或已过期')
    if not content:
        raise HTTPException(400, '原始邮件不能为空')
    normalized = recipient.lower()
    if recipient != recipient.strip() or normalized.count('@') != 1 or normalized.rsplit('@', 1)[-1] != domain:
        raise HTTPException(404, '收票地址不存在或已停用')
    delivery_id = uuid.uuid4().hex
    path = _raw_path(delivery_id)
    created = False
    try:
        with LOCK, connection(write=True) as db:
            address = db.execute('SELECT * FROM receipt_addresses WHERE address=? AND enabled=1', (normalized,)).fetchone()
            if address is None:
                raise HTTPException(404, '收票地址不存在或已停用')
            if address['group_id']:
                require_group(db, address['owner_id'], address['group_id'])
            previous = db.execute('SELECT id,status FROM inbound_deliveries WHERE address_id=? AND raw_hash=?',
                                  (address['id'], digest)).fetchone()
            if previous:
                return {**dict(previous), 'duplicate': True}
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            created = True
            with os.fdopen(descriptor, 'wb') as output:
                output.write(content)
                output.flush()
                os.fsync(output.fileno())
            received_at = _now()
            db.execute('''INSERT INTO inbound_deliveries(id,address_id,raw_hash,raw_path,received_at,status,group_id) VALUES (?,?,?,?,?,?,?)''',
                       (delivery_id, address['id'], digest, f'inbound/{delivery_id}.eml', received_at, 'queued', address['group_id']))
            db.execute('UPDATE receipt_addresses SET last_received=? WHERE id=?', (received_at, address['id']))
    except Exception:
        if created:
            path.unlink(missing_ok=True)
        raise
    _wake.set()
    return {'id': delivery_id, 'status': 'queued', 'duplicate': False}


def _record(delivery_id: str, index: str, filename: str, status: str,
            error: str = '', invoice_id: str | None = None) -> None:
    with LOCK, connection(write=True) as db:
        db.execute('''INSERT INTO inbound_results VALUES (?,?,?,?,?,?)
            ON CONFLICT(delivery_id,attachment_index) DO UPDATE SET
            filename=excluded.filename,status=excluded.status,error=excluded.error,invoice_id=excluded.invoice_id''',
            (delivery_id, index, filename[:200], status, error[:300], invoice_id))


def _process_delivery(delivery: dict) -> None:
    delivery_id = delivery['id']
    try:
        with connection() as db:
            address = dict(db.execute('SELECT * FROM receipt_addresses WHERE id=?', (delivery['address_id'],)).fetchone())
        # The importer resolves a concurrent departure into the original owner's personal library.
        content = _raw_path(delivery_id).read_bytes()
        for attachment in iter_message_attachments(content):
            index = attachment['index']
            with connection() as db:
                old = db.execute('SELECT status FROM inbound_results WHERE delivery_id=? AND attachment_index=?',
                                 (delivery_id, index)).fetchone()
            if old and old['status'] in ('imported', 'duplicate', 'ignored'):
                continue
            if attachment['status'] != 'pending':
                _record(delivery_id, index, attachment['filename'], attachment['status'], attachment['error'])
                continue
            with LOCK, connection(write=True) as db:
                db.execute("DELETE FROM inbound_results WHERE delivery_id=? AND attachment_index=? AND status='failed'",
                           (delivery_id, attachment['parent_index']))
            try:
                if _importer is None:
                    raise RuntimeError('Importer is not configured')
                result = _importer(attachment['data'], attachment['filename'], address['owner_id'], delivery['group_id'])
                status = result.get('status')
                if status not in ('imported', 'duplicate'):
                    raise RuntimeError('Invalid importer result')
                invoice_id = result.get('invoice', {}).get('id')
                _record(delivery_id, index, attachment['filename'], status, invoice_id=invoice_id)
            except Exception as exc:
                _record(delivery_id, index, attachment['filename'], 'failed', _import_error(exc))
        with LOCK, connection(write=True) as db:
            # A prior interruption/error at the delivery level is resolved by a full MIME traversal.
            db.execute("DELETE FROM inbound_results WHERE delivery_id=? AND attachment_index='delivery'", (delivery_id,))
    except Exception as exc:
        _record(delivery_id, 'delivery', '', 'failed', _import_error(exc))
    with LOCK, connection(write=True) as db:
        failed = db.execute("SELECT 1 FROM inbound_results WHERE delivery_id=? AND status='failed' LIMIT 1", (delivery_id,)).fetchone()
        status = 'failed' if failed else 'completed'
        db.execute('UPDATE inbound_deliveries SET status=? WHERE id=?', (status, delivery_id))


def process_pending(limit: int = 10) -> int:
    """Process queued receipts synchronously; useful for CLI checks and bounded tests."""
    if not _processing.acquire(blocking=False):
        return 0
    processed = 0
    try:
        _purge_expired_deliveries()
        for _ in range(limit):
            with LOCK, connection(write=True) as db:
                row = db.execute("""SELECT * FROM inbound_deliveries WHERE status='queued' AND received_at>?
                                 ORDER BY received_at,rowid LIMIT 1""", (_retention_cutoff(),)).fetchone()
                if row is None:
                    break
                delivery = dict(row)
                db.execute("UPDATE inbound_deliveries SET status='processing' WHERE id=?", (delivery['id'],))
            _process_delivery(delivery)
            processed += 1
        return processed
    finally:
        _processing.release()


def _poll() -> None:
    while not _stop.is_set():
        _wake.clear()
        try:
            count = process_pending(limit=1)
        except Exception:
            # Do not log raw mail, request headers, or third-party parser exceptions.
            count = 0
        if not count:
            _wake.wait(30)


def start_inbound_worker() -> None:
    global _worker
    if not configured() or os.environ.get('INVOICE_INBOUND_WORKER', '1').lower() in ('0', 'false', 'off', 'no'):
        return
    if _worker is not None and _worker.is_alive():
        return
    _stop.clear()
    _worker = Thread(target=_poll, name='invoice-inbound', daemon=True)
    _worker.start()


def stop_inbound_worker() -> None:
    global _worker
    _stop.set()
    _wake.set()
    if _worker is not None:
        _worker.join(timeout=20)
        if not _worker.is_alive():
            _worker = None
