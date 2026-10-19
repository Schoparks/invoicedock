"""Dedicated receipt inboxes: verified TLS, local secret encryption, UID-safe polling.

Run one application worker. A newly bound inbox starts at UIDNEXT - 1; historical
mail is never imported implicitly. Failed attachments are recorded and can be
retried explicitly without blocking later messages.
"""
from __future__ import annotations

import base64
from contextlib import contextmanager
from datetime import datetime, timezone
from email import policy
from email.parser import BytesParser
import imaplib
import ipaddress
import os
from pathlib import PurePosixPath
import re
import socket
import ssl
from threading import Event, Lock, Thread
from typing import Callable
import uuid

from cryptography.fernet import Fernet, InvalidToken
from fastapi import APIRouter, Depends, HTTPException

from .auth import require_user
from . import db as db_store
from .db import LOCK, connection
from .parsing import EXTENSIONS, expand_upload
from .permissions import require_group

router = APIRouter(prefix='/api/mailboxes', tags=['mailboxes'])
MAX_MAIL_BYTES = 20 * 1024 * 1024
MAX_MESSAGES = 50
MAX_ATTACHMENTS = 200
NETWORK_TIMEOUT = 15
_importer: Callable | None = None
_cipher: Fernet | None = None
_locks: dict[str, Lock] = {}
_locks_guard = Lock()
_stop = Event()
_worker: Thread | None = None


class MailboxError(ValueError):
    """A safe user-facing failure; never contains raw server errors or secrets."""


class MessageRejected(MailboxError):
    """A permanent message failure which must not stall the inbox cursor."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


def configure_importer(callback: Callable) -> None:
    global _importer
    _importer = callback


def initialize_mailboxes() -> None:
    global _cipher
    db_store.DATA.mkdir(parents=True, exist_ok=True)
    with LOCK, connection() as db:
        db.executescript('''
        CREATE TABLE IF NOT EXISTS mailboxes (
          id TEXT PRIMARY KEY, name TEXT NOT NULL, address TEXT NOT NULL,
          host TEXT NOT NULL, port INTEGER NOT NULL DEFAULT 993, username TEXT NOT NULL,
          password_encrypted TEXT NOT NULL, folder TEXT NOT NULL DEFAULT 'INBOX',
          owner_id TEXT NOT NULL, group_id TEXT, enabled INTEGER NOT NULL DEFAULT 1,
          uid_validity TEXT NOT NULL DEFAULT '', last_uid INTEGER NOT NULL DEFAULT 0,
          last_sync TEXT NOT NULL DEFAULT '', last_error TEXT NOT NULL DEFAULT ''
        );
        CREATE INDEX IF NOT EXISTS mailboxes_owner ON mailboxes(owner_id,group_id);
        CREATE TABLE IF NOT EXISTS mail_results (
          mailbox_id TEXT NOT NULL REFERENCES mailboxes(id) ON DELETE CASCADE,
          uid_validity TEXT NOT NULL, uid INTEGER NOT NULL, attachment_index TEXT NOT NULL,
          filename TEXT NOT NULL DEFAULT '', status TEXT NOT NULL, error TEXT NOT NULL DEFAULT '',
          created_at TEXT NOT NULL,
          PRIMARY KEY(mailbox_id,uid_validity,uid,attachment_index)
        );
        CREATE INDEX IF NOT EXISTS mail_results_failed ON mail_results(mailbox_id,status,uid);
        ''')
        has_credentials = db.execute('SELECT 1 FROM mailboxes LIMIT 1').fetchone() is not None
    key = os.environ.get('INVOICE_SECRET_KEY', '').strip().encode()
    if not key:
        path = db_store.DATA / 'secret.key'
        if not path.exists():
            if has_credentials:
                raise RuntimeError('邮箱加密密钥缺失；请恢复原 secret.key 或 INVOICE_SECRET_KEY 后启动')
            try:
                descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            except FileExistsError:
                pass
            else:
                with os.fdopen(descriptor, 'wb') as file:
                    file.write(Fernet.generate_key())
        key = path.read_bytes().strip()
        path.chmod(0o600)
    try:
        _cipher = Fernet(key)
    except (ValueError, TypeError):
        raise RuntimeError('邮箱加密密钥格式无效；需要有效的 Fernet key') from None
    if has_credentials:
        with connection() as db:
            for row in db.execute('SELECT password_encrypted FROM mailboxes'):
                try:
                    _cipher.decrypt(row['password_encrypted'].encode())
                except InvalidToken:
                    raise RuntimeError('邮箱加密密钥与已保存凭据不匹配；请恢复原密钥') from None


def _encrypt(password: str) -> str:
    if _cipher is None:
        raise RuntimeError('邮箱加密尚未初始化')
    return _cipher.encrypt(password.encode()).decode()


def _decrypt(value: str) -> str:
    if _cipher is None:
        raise MailboxError('邮箱加密尚未初始化')
    try:
        return _cipher.decrypt(value.encode()).decode()
    except (InvalidToken, UnicodeError):
        raise MailboxError('邮箱凭据无法解密，请检查服务端密钥') from None


def _field(value, label: str, maximum: int = 200, required: bool = True) -> str:
    if not isinstance(value, str) or len(value) > maximum or any(ord(c) < 32 for c in value):
        raise HTTPException(400, f'{label}格式不正确')
    value = value.strip()
    if required and not value:
        raise HTTPException(400, f'请填写{label}')
    return value


def _public(row) -> dict:
    fields = ('id', 'name', 'address', 'host', 'port', 'username', 'folder', 'group_id',
              'enabled', 'last_sync', 'last_error')
    item = {key: row[key] for key in fields}
    item['enabled'] = bool(item['enabled'])
    item['has_password'] = bool(row['password_encrypted'])
    return item


def _allowed(db, user_id: str, mailbox) -> bool:
    return mailbox['owner_id'] == user_id


def _get_managed(db, mailbox_id: str, user_id: str) -> dict:
    row = db.execute('SELECT * FROM mailboxes WHERE id=?', (mailbox_id,)).fetchone()
    if row is None or not _allowed(db, user_id, row):
        raise HTTPException(404, '邮箱不存在或无管理权限')
    return dict(row)


def _lock_for(mailbox_id: str) -> Lock:
    with _locks_guard:
        return _locks.setdefault(mailbox_id, Lock())


@contextmanager
def _exclusive(mailbox_id: str):
    lock = _lock_for(mailbox_id)
    if not lock.acquire(blocking=False):
        raise HTTPException(409, '该邮箱正在收取，请稍后重试')
    try:
        yield
    finally:
        lock.release()


def _public_addresses(host: str) -> list[str]:
    try:
        records = socket.getaddrinfo(host, 993, type=socket.SOCK_STREAM)
    except OSError:
        raise MailboxError('无法解析 IMAP 主机，请检查服务器地址') from None
    addresses = []
    for record in records:
        value = record[4][0]
        try:
            address = ipaddress.ip_address(value)
        except ValueError:
            raise MailboxError('IMAP 主机解析地址无效') from None
        if not address.is_global or address.is_multicast or address.is_reserved:
            raise MailboxError('邮箱服务器必须使用公网地址，不能连接内网、回环或保留地址')
        if value not in addresses:
            addresses.append(value)
    if not addresses:
        raise MailboxError('IMAP 主机未解析到可用的公网地址')
    return addresses


class _PinnedIMAP4SSL(imaplib.IMAP4_SSL):
    """Connect to a previously checked IP, while retaining hostname TLS checks."""

    def __init__(self, host: str, address: str):
        self._pinned_address = address
        self._literal_budget = MAX_MAIL_BYTES
        super().__init__(host, 993, ssl_context=ssl.create_default_context(), timeout=NETWORK_TIMEOUT)

    def _create_socket(self, timeout):
        transport = socket.create_connection((self._pinned_address, self.port), timeout)
        try:
            return self.ssl_context.wrap_socket(transport, server_hostname=self.host)
        except Exception:
            transport.close()
            raise

    def read(self, size):
        if size > self._literal_budget:
            raise MessageRejected('IMAP 返回内容超过单封邮件 20 MB 限制')
        self._literal_budget -= size
        return super().read(size)


def _imap_folder(value: str) -> str:
    # IMAP modified UTF-7; folder names are always quoted to prevent token injection.
    result, pending = [], []
    def flush():
        if pending:
            encoded = base64.b64encode(''.join(pending).encode('utf-16-be')).decode().rstrip('=').replace('/', ',')
            result.append('&' + encoded + '-')
            pending.clear()
    for character in value:
        if 32 <= ord(character) <= 126:
            flush()
            result.append('&-' if character == '&' else character)
        else:
            pending.append(character)
    flush()
    return '"' + ''.join(result).replace('\\', '\\\\').replace('"', '\\"') + '"'


@contextmanager
def _session(mailbox: dict):
    addresses = _public_addresses(mailbox['host'])
    client = None
    try:
        client = _PinnedIMAP4SSL(mailbox['host'], addresses[0])
        try:
            client.login(mailbox['username'], _decrypt(mailbox['password_encrypted']))
        except imaplib.IMAP4.error:
            raise MailboxError('邮箱登录失败，请确认已开启 IMAP 并填写正确的授权码') from None
        status, _ = client.select(_imap_folder(mailbox['folder']), readonly=True)
        if status != 'OK':
            raise MailboxError('无法读取收票文件夹，请检查文件夹名称')
        yield client
    except (ssl.SSLError, ssl.CertificateError):
        raise MailboxError('邮箱服务器 TLS 证书验证失败，连接已拒绝') from None
    except (OSError, TimeoutError):
        raise MailboxError('邮箱服务器连接失败或超时，请稍后重试') from None
    except imaplib.IMAP4.error:
        raise MailboxError('邮箱服务器未完成 IMAP 请求，请检查配置后重试') from None
    finally:
        if client is not None:
            try:
                client.logout()
            except Exception:
                try:
                    client.shutdown()
                except Exception:
                    pass


def _uid_state(client, folder: str) -> tuple[str, int]:
    status, responses = client.status(_imap_folder(folder), '(UIDVALIDITY UIDNEXT)')
    if status != 'OK':
        raise MailboxError('邮箱服务器未返回收取游标，请稍后重试')
    raw = b' '.join(value for value in responses if isinstance(value, bytes))
    validity = re.search(rb'UIDVALIDITY\s+(\d+)', raw, re.I)
    following = re.search(rb'UIDNEXT\s+(\d+)', raw, re.I)
    if not validity or not following or int(following.group(1)) < 1:
        raise MailboxError('邮箱服务器返回的收取游标无效')
    return validity.group(1).decode(), int(following.group(1))


def _values(values: dict, old: dict | None, user_id: str) -> dict:
    data = dict(old or {'name': '', 'address': '', 'host': '', 'port': 993, 'username': '',
                       'folder': 'INBOX', 'owner_id': user_id, 'group_id': None, 'enabled': True})
    for key in ('name', 'address', 'host', 'port', 'username', 'folder', 'group_id', 'enabled'):
        if key in values:
            data[key] = values[key]
    for key, label in (('name', '邮箱名称'), ('address', '收票地址'), ('host', 'IMAP 主机'),
                       ('username', '登录账号'), ('folder', '收票文件夹')):
        data[key] = _field(data[key], label, 254 if key in ('host', 'address') else 200)
    if not re.fullmatch(r'[^\s@]+@[^\s@]+\.[^\s@]+', data['address']):
        raise HTTPException(400, '收票地址格式不正确')
    try:
        data['host'] = data['host'].encode('idna').decode().lower()
    except UnicodeError:
        raise HTTPException(400, 'IMAP 主机格式不正确') from None
    if not re.fullmatch(r'[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?', data['host']):
        raise HTTPException(400, 'IMAP 主机仅填写域名，不包含协议或端口')
    if type(data['port']) is not int or data['port'] != 993:
        raise HTTPException(400, '仅支持经过证书验证的 IMAP TLS 993 端口')
    if type(data['enabled']) not in (bool, int) or data['enabled'] not in (0, 1):
        raise HTTPException(400, '启用状态格式不正确')
    data['enabled'] = bool(data['enabled'])
    data['group_id'] = data['group_id'] or None
    if data['group_id'] is not None:
        data['group_id'] = _field(data['group_id'], '小组编号', 100)
        with connection() as db:
            require_group(db, user_id, data['group_id'])
            # Changing scope must not leave the original creator outside the target group.
            require_group(db, data['owner_id'], data['group_id'])
    password = values.get('password', '')
    if not isinstance(password, str) or len(password) > 4096 or any(ord(c) < 32 for c in password):
        raise HTTPException(400, '邮箱授权码格式不正确')
    if password:
        data['password_encrypted'] = _encrypt(password)
    elif not old:
        raise HTTPException(400, '请填写邮箱授权码')
    return data


@router.get('')
def list_mailboxes(user=Depends(require_user)):
    with connection() as db:
        rows = db.execute('SELECT * FROM mailboxes WHERE owner_id=? ORDER BY name,id', (user['id'],)).fetchall()
        return {'mailboxes': [_public(row) for row in rows if _allowed(db, user['id'], row)]}


@router.post('')
def create_mailbox(values: dict, user=Depends(require_user)):
    data = _values(values, None, user['id'])
    try:
        with _session(data) as client:
            validity, next_uid = _uid_state(client, data['folder'])
    except MailboxError as exc:
        raise HTTPException(400, str(exc)) from None
    mailbox_id = uuid.uuid4().hex
    data.update(id=mailbox_id, uid_validity=validity, last_uid=next_uid - 1, last_sync=_now(), last_error='')
    columns = ('id', 'name', 'address', 'host', 'port', 'username', 'password_encrypted', 'folder',
               'owner_id', 'group_id', 'enabled', 'uid_validity', 'last_uid', 'last_sync', 'last_error')
    with LOCK, connection(write=True) as db:
        if data['group_id']:
            require_group(db, user['id'], data['group_id'])
        db.execute(f"INSERT INTO mailboxes ({','.join(columns)}) VALUES ({','.join('?' for _ in columns)})", [data[key] for key in columns])
    return _public(data)


@router.patch('/{mailbox_id}')
def update_mailbox(mailbox_id: str, values: dict, user=Depends(require_user)):
    with _exclusive(mailbox_id):
        with connection() as db:
            old = _get_managed(db, mailbox_id, user['id'])
        data = _values(values, old, user['id'])
        changed_inbox = any(data[key] != old[key] for key in ('host', 'username', 'folder'))
        if changed_inbox or values.get('password'):
            try:
                with _session(data) as client:
                    validity, next_uid = _uid_state(client, data['folder'])
            except MailboxError as exc:
                raise HTTPException(400, str(exc)) from None
            if changed_inbox:
                data.update(uid_validity=validity, last_uid=next_uid - 1)
        columns = ('name', 'address', 'host', 'port', 'username', 'password_encrypted', 'folder',
                   'group_id', 'enabled', 'uid_validity', 'last_uid')
        with LOCK, connection(write=True) as db:
            current = _get_managed(db, mailbox_id, user['id'])
            if current['group_id'] != old['group_id']:
                raise HTTPException(409, '邮箱归属已变化，请刷新后重新设置')
            if data['group_id']:
                require_group(db, user['id'], data['group_id'])
            db.execute(f"UPDATE mailboxes SET {','.join(key+'=?' for key in columns)},last_error='' WHERE id=?", [*[data[key] for key in columns], mailbox_id])
            if changed_inbox:
                db.execute('DELETE FROM mail_results WHERE mailbox_id=?', (mailbox_id,))
        data['last_error'] = ''
        return _public(data)


@router.delete('/{mailbox_id}')
def delete_mailbox(mailbox_id: str, user=Depends(require_user)):
    with _exclusive(mailbox_id), LOCK, connection(write=True) as db:
        _get_managed(db, mailbox_id, user['id'])
        db.execute('DELETE FROM mailboxes WHERE id=?', (mailbox_id,))
    return {'ok': True}


@router.get('/{mailbox_id}/results')
def mailbox_results(mailbox_id: str, user=Depends(require_user)):
    with connection() as db:
        _get_managed(db, mailbox_id, user['id'])
        results = [dict(row) for row in db.execute('''SELECT uid,filename,status,error,created_at FROM mail_results
                    WHERE mailbox_id=? ORDER BY created_at DESC,uid DESC LIMIT 50''', (mailbox_id,))]
        return {'results': results}


def _record(mailbox: dict, uid: int, index: str, filename: str, status: str, error: str = '') -> None:
    with LOCK, connection(write=True) as db:
        db.execute('''INSERT INTO mail_results VALUES (?,?,?,?,?,?,?,?)
            ON CONFLICT(mailbox_id,uid_validity,uid,attachment_index)
            DO UPDATE SET filename=excluded.filename,status=excluded.status,error=excluded.error,created_at=excluded.created_at''',
            (mailbox['id'], mailbox['uid_validity'], uid, index, filename[:200], status, error[:300], _now()))


def _done(mailbox: dict, uid: int, index: str) -> bool:
    with connection() as db:
        row = db.execute('''SELECT status FROM mail_results WHERE mailbox_id=? AND uid_validity=?
                         AND uid=? AND attachment_index=?''', (mailbox['id'], mailbox['uid_validity'], uid, index)).fetchone()
        return row is not None and row['status'] in ('imported', 'duplicate', 'ignored')


def _error(result: dict, filename: str, error: str) -> None:
    result['error_count'] += 1
    if len(result['errors']) < 20:
        result['errors'].append({'filename': filename[:200], 'error': error[:300]})


def _fetch_message(client, uid: int) -> bytes:
    status, data = client.uid('FETCH', str(uid), '(RFC822.SIZE)')
    if status != 'OK':
        raise MailboxError('读取邮件大小失败，请稍后重试')
    raw = b' '.join(part for part in data if isinstance(part, bytes))
    size = re.search(rb'RFC822\.SIZE\s+(\d+)', raw, re.I)
    if not size:
        raise MessageRejected('邮件已不存在，或服务器未返回邮件大小')
    if int(size.group(1)) > MAX_MAIL_BYTES:
        raise MessageRejected('邮件超过 20 MB，请拆分附件后重新发送')
    client._literal_budget = MAX_MAIL_BYTES
    status, data = client.uid('FETCH', str(uid), '(BODY.PEEK[])')
    if status != 'OK':
        raise MailboxError('读取邮件失败，请稍后重试')
    bodies = [part[1] for part in data if isinstance(part, tuple) and len(part) > 1 and isinstance(part[1], bytes)]
    if len(bodies) != 1 or len(bodies[0]) > MAX_MAIL_BYTES:
        raise MessageRejected('邮件内容无效或超过 20 MB')
    return bodies[0]


def _import_error(exc: Exception) -> str:
    if isinstance(exc, HTTPException) and isinstance(exc.detail, str):
        return exc.detail[:300]
    if isinstance(exc, ValueError):
        return str(exc)[:300]
    return '附件入库失败，请稍后重试或下载原邮件附件手动导入'


def iter_message_attachments(content: bytes):
    """Yield bounded MIME attachments and safe result records for either mail transport."""
    try:
        message = BytesParser(policy=policy.default).parsebytes(content)
    except Exception:
        yield {'index': 'message', 'filename': '', 'status': 'failed',
               'error': '邮件格式损坏，请下载附件后手动导入'}
        return
    processed = 0
    has_supported_attachment = False
    for position, part in enumerate(message.walk()):
        if part.is_multipart():
            continue
        filename = part.get_filename()
        if not filename:
            continue
        filename = PurePosixPath(filename.replace('\\', '/')).name
        filename = ''.join(c for c in filename if ord(c) >= 32)[:200]
        extension = PurePosixPath(filename).suffix.lower()
        if extension not in EXTENSIONS | {'.zip'}:
            continue
        has_supported_attachment = True
        index = str(position)
        try:
            payload = part.get_payload(decode=True)
            if not payload:
                raise ValueError('附件内容为空或编码无效')
            entries = expand_upload(payload, filename)
        except Exception as exc:
            yield {'index': index, 'filename': filename, 'status': 'failed', 'error': _import_error(exc)}
            continue
        for child, (name, data) in enumerate(entries):
            processed += 1
            if processed > MAX_ATTACHMENTS:
                yield {'index': 'message', 'filename': '', 'status': 'failed',
                       'error': '单封邮件最多处理 200 个发票文件，请拆分后重新发送'}
                return
            yield {'index': f'{position}:{child}', 'parent_index': index,
                   'filename': name, 'data': data, 'status': 'pending', 'error': ''}
    if not has_supported_attachment:
        yield {'index': 'message', 'filename': '', 'status': 'ignored',
               'error': '未找到受支持的附件；若邮件只有正文链接，请下载发票附件后手动导入。'}


def _process_uid(client, mailbox: dict, uid: int, result: dict) -> None:
    try:
        content = _fetch_message(client, uid)
    except MessageRejected as exc:
        _record(mailbox, uid, 'message', '', 'failed', str(exc))
        _error(result, f'邮件 UID {uid}', str(exc))
        return
    message_failed = False
    for attachment in iter_message_attachments(content):
        index, filename = attachment['index'], attachment['filename']
        if attachment['status'] != 'pending':
            _record(mailbox, uid, index, filename, attachment['status'], attachment['error'])
            if attachment['status'] == 'failed':
                _error(result, filename or f'邮件 UID {uid}', attachment['error'])
                message_failed = message_failed or index == 'message'
            continue
        with LOCK, connection(write=True) as db:
            db.execute("DELETE FROM mail_results WHERE mailbox_id=? AND uid_validity=? AND uid=? AND attachment_index=? AND status='failed'", (mailbox['id'], mailbox['uid_validity'], uid, attachment['parent_index']))
        if _done(mailbox, uid, index):
            continue
        try:
            if _importer is None:
                raise RuntimeError('Importer is not configured')
            item = _importer(attachment['data'], filename, mailbox['owner_id'], mailbox['group_id'])
            status = item.get('status')
            if status not in ('imported', 'duplicate'):
                raise RuntimeError('Invalid importer result')
        except Exception as exc:
            error = _import_error(exc)
            _record(mailbox, uid, index, filename, 'failed', error)
            _error(result, filename, error)
            continue
        _record(mailbox, uid, index, filename, status)
        result['imported' if status == 'imported' else 'duplicates'] += 1
    if not message_failed:
        with LOCK, connection(write=True) as db:
            db.execute("DELETE FROM mail_results WHERE mailbox_id=? AND uid_validity=? AND uid=? AND attachment_index='message' AND status='failed'", (mailbox['id'], mailbox['uid_validity'], uid))


def sync_mailbox(mailbox_id: str, retry: bool = False, user_id: str | None = None) -> dict:
    with _exclusive(mailbox_id):
        with connection() as db:
            if user_id is not None:
                mailbox = _get_managed(db, mailbox_id, user_id)
            else:
                row = db.execute('SELECT * FROM mailboxes WHERE id=?', (mailbox_id,)).fetchone()
                if row is None or not row['enabled']:
                    return {'imported': 0, 'duplicates': 0, 'errors': [], 'error_count': 0}
                mailbox = dict(row)
            if mailbox['group_id']:
                try:
                    require_group(db, mailbox['owner_id'], mailbox['group_id'])
                except HTTPException:
                    with LOCK, connection(write=True) as write_db:
                        write_db.execute("UPDATE mailboxes SET enabled=0,last_error='邮箱创建者已不在所属小组，收取已停用' WHERE id=?", (mailbox_id,))
                    raise MailboxError('邮箱创建者已不在所属小组，收取已停用') from None
        result = {'imported': 0, 'duplicates': 0, 'errors': [], 'error_count': 0}
        try:
            with _session(mailbox) as client:
                validity, next_uid = _uid_state(client, mailbox['folder'])
                if validity != mailbox['uid_validity']:
                    with LOCK, connection(write=True) as db:
                        db.execute('UPDATE mailboxes SET uid_validity=?,last_uid=?,last_sync=?,last_error=? WHERE id=?',
                                   (validity, next_uid - 1, _now(), '邮箱 UID 已重置，已从当前时点重新收取；历史邮件请手动导入', mailbox_id))
                    return {**result, 'notice': '邮箱 UID 已重置，从当前时点接收后续新邮件；历史邮件请手动导入'}
                if retry:
                    with connection() as db:
                        uids = [row['uid'] for row in db.execute("SELECT DISTINCT uid FROM mail_results WHERE mailbox_id=? AND uid_validity=? AND status='failed' ORDER BY uid LIMIT ?", (mailbox_id, validity, MAX_MESSAGES))]
                else:
                    status, search = client.uid('SEARCH', None, 'UID', f"{mailbox['last_uid'] + 1}:*")
                    if status != 'OK':
                        raise MailboxError('检索新邮件失败，请稍后重试')
                    raw = b' '.join(part for part in search if isinstance(part, bytes))
                    uids = sorted({int(value) for value in raw.split() if value.isdigit() and int(value) > mailbox['last_uid']})[:MAX_MESSAGES]
                for uid in uids:
                    if user_id is None and _stop.is_set():
                        break
                    _process_uid(client, mailbox, uid, result)
                    if not retry:
                        with LOCK, connection(write=True) as db:
                            db.execute('UPDATE mailboxes SET last_uid=? WHERE id=?', (uid, mailbox_id))
                with connection() as db:
                    unresolved = db.execute("SELECT COUNT(*) FROM mail_results WHERE mailbox_id=? AND uid_validity=? AND status='failed'", (mailbox_id, validity)).fetchone()[0]
                summary = f'{unresolved} 项附件或邮件未成功处理，可查看记录并重试' if unresolved else ''
                with LOCK, connection(write=True) as db:
                    db.execute('UPDATE mailboxes SET last_sync=?,last_error=? WHERE id=?', (_now(), summary, mailbox_id))
                return result
        except MailboxError as exc:
            with LOCK, connection(write=True) as db:
                db.execute('UPDATE mailboxes SET last_sync=?,last_error=? WHERE id=?', (_now(), str(exc)[:300], mailbox_id))
            raise


@router.post('/{mailbox_id}/sync')
def manual_sync(mailbox_id: str, user=Depends(require_user)):
    try:
        return sync_mailbox(mailbox_id, user_id=user['id'])
    except MailboxError as exc:
        raise HTTPException(400, str(exc)) from None


@router.post('/{mailbox_id}/retry')
def retry_failed(mailbox_id: str, user=Depends(require_user)):
    try:
        return sync_mailbox(mailbox_id, retry=True, user_id=user['id'])
    except MailboxError as exc:
        raise HTTPException(400, str(exc)) from None


def _poll(interval: int) -> None:
    while not _stop.is_set():
        with connection() as db:
            mailbox_ids = [row['id'] for row in db.execute('SELECT id FROM mailboxes WHERE enabled=1 ORDER BY last_sync,id')]
        for mailbox_id in mailbox_ids:
            if _stop.is_set():
                return
            try:
                sync_mailbox(mailbox_id)
            except (MailboxError, HTTPException):
                pass
            except Exception:
                # Do not log raw exceptions: third-party responses could include credentials.
                with LOCK, connection(write=True) as db:
                    db.execute("UPDATE mailboxes SET last_error='收取过程发生异常，请检查服务状态后重试' WHERE id=?", (mailbox_id,))
        _stop.wait(interval)


def start_mailbox_worker() -> None:
    global _worker
    if os.environ.get('INVOICE_MAIL_WORKER', '1').lower() in ('0', 'false', 'off', 'no'):
        return
    if _worker is not None and _worker.is_alive():
        return
    try:
        interval = int(os.environ.get('INVOICE_MAIL_POLL_SECONDS', '60'))
    except ValueError:
        raise RuntimeError('INVOICE_MAIL_POLL_SECONDS 须为 10 至 60 的整数') from None
    if not 10 <= interval <= 60:
        raise RuntimeError('INVOICE_MAIL_POLL_SECONDS 须为 10 至 60 的整数')
    _stop.clear()
    _worker = Thread(target=_poll, args=(interval,), name='invoice-mailboxes', daemon=True)
    _worker.start()


def stop_mailbox_worker() -> None:
    global _worker
    _stop.set()
    if _worker is not None:
        _worker.join(timeout=20)
        if not _worker.is_alive():
            _worker = None
