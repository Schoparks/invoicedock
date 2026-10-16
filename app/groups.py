"""Group membership and explicit sharing of individually owned invoices."""
import hashlib
import secrets
import time
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException

from .auth import require_user
from .db import LOCK, connection
from .permissions import require_group

router = APIRouter()


def _now():
    return datetime.now(timezone.utc).isoformat()


def _group(db, group_id, user_id):
    row = db.execute('''SELECT g.id,g.name,g.description,g.created_by,g.created_at,m.role,
        (SELECT COUNT(*) FROM group_members WHERE group_id=g.id) AS member_count
        FROM groups g JOIN group_members m ON m.group_id=g.id WHERE g.id=? AND m.user_id=?''',
        (group_id, user_id)).fetchone()
    if not row:
        raise HTTPException(403, '无权访问此组')
    return dict(row)


def _owned_invoice(db, invoice_id, user_id):
    row = db.execute('SELECT * FROM invoices WHERE id=? AND owner_id=?', (invoice_id, user_id)).fetchone()
    if not row:
        raise HTTPException(403, '仅发票所有者可管理分享')
    return dict(row)


def _active_share(db, invoice_id, group_id):
    return db.execute('''SELECT 1 FROM batch_items bi JOIN batches b ON b.id=bi.batch_id
        WHERE bi.invoice_id=? AND b.group_id=? AND b.status IN ('draft','submitted') LIMIT 1''',
        (invoice_id, group_id)).fetchone()


@router.get('/api/groups')
def list_groups(user: dict = Depends(require_user)):
    with connection() as db:
        return [dict(row) for row in db.execute('''SELECT g.id,g.name,g.description,g.created_by,g.created_at,m.role,
            (SELECT COUNT(*) FROM group_members WHERE group_id=g.id) AS member_count
            FROM groups g JOIN group_members m ON m.group_id=g.id WHERE m.user_id=?
            ORDER BY g.created_at,g.id''', (user['id'],))]


def _details(values, old=None):
    name = values.get('name', old['name'] if old else '')
    description = values.get('description', old['description'] if old else '')
    if not isinstance(name, str) or not 1 <= len(name.strip()) <= 100:
        raise HTTPException(400, '组名须为 1 至 100 个字符')
    if not isinstance(description, str) or len(description.strip()) > 1000:
        raise HTTPException(400, '团队说明最多 1,000 个字符')
    return name.strip(), description.strip()


def _cancel_batches(db, group_id, member_id=None):
    # Cancel complete batches to retain snapshots and totals, releasing only their current claims.
    sql = "SELECT id FROM batches WHERE group_id=? AND status IN ('draft','submitted')"
    args = [group_id]
    if member_id:
        sql += """ AND (owner_id=? OR id IN (SELECT bi.batch_id FROM batch_items bi
            JOIN invoices i ON i.id=bi.invoice_id WHERE i.owner_id=?))"""
        args.extend([member_id, member_id])
    batch_ids = [row['id'] for row in db.execute(sql, args)]
    for batch_id in batch_ids:
        db.execute("UPDATE invoices SET status='available',batch_id=NULL WHERE batch_id=?", (batch_id,))
        db.execute("UPDATE batches SET status='cancelled',updated_at=? WHERE id=?", (_now(), batch_id))
    return len(batch_ids)


def _detach_receivers(db, group_id, member_id=None):
    # Preserve credentials, receipt history and queued mail for the original account.
    for table in ('mailboxes', 'receipt_addresses'):
        if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone():
            continue
        sql = f'UPDATE {table} SET group_id=NULL,enabled=0 WHERE group_id=?'
        args = [group_id]
        if member_id:
            sql += ' AND owner_id=?'
            args.append(member_id)
        db.execute(sql, args)


@router.post('/api/groups')
def create_group(values: dict, user: dict = Depends(require_user)):
    name, description = _details(values)
    with LOCK, connection(write=True) as db:
        group_id, stamp = uuid.uuid4().hex, _now()
        db.execute('INSERT INTO groups(id,name,created_by,created_at,description) VALUES (?,?,?,?,?)',
                   (group_id, name, user['id'], stamp, description))
        db.execute('INSERT INTO group_members VALUES (?,?,?,?)', (group_id, user['id'], 'owner', stamp))
        return _group(db, group_id, user['id'])


@router.patch('/api/groups/{group_id}')
def update_group(group_id: str, values: dict, user: dict = Depends(require_user)):
    with LOCK, connection(write=True) as db:
        require_group(db, user['id'], group_id, admin=True)
        old = _group(db, group_id, user['id'])
        name, description = _details(values, old)
        db.execute('UPDATE groups SET name=?,description=? WHERE id=?', (name, description, group_id))
        return _group(db, group_id, user['id'])


@router.post('/api/groups/{group_id}/transfer')
def transfer_group(group_id: str, values: dict, user: dict = Depends(require_user)):
    member_id = values.get('member_id')
    if not isinstance(member_id, str) or not member_id or member_id == user['id']:
        raise HTTPException(400, '请选择另一位团队成员接任群主')
    with LOCK, connection(write=True) as db:
        actor = require_group(db, user['id'], group_id)
        if actor['role'] != 'owner':
            raise HTTPException(403, '仅群主可转让团队')
        require_group(db, member_id, group_id)
        db.execute("UPDATE group_members SET role='admin' WHERE group_id=? AND user_id=?", (group_id, user['id']))
        db.execute("UPDATE group_members SET role='owner' WHERE group_id=? AND user_id=?", (group_id, member_id))
        # created_by records the original creator, while membership holds current ownership.
        return _group(db, group_id, user['id'])


@router.delete('/api/groups/{group_id}')
def dissolve_group(group_id: str, user: dict = Depends(require_user)):
    with LOCK, connection(write=True) as db:
        actor = require_group(db, user['id'], group_id)
        if actor['role'] != 'owner':
            raise HTTPException(403, '仅群主可解散团队')
        cancelled = _cancel_batches(db, group_id)
        _detach_receivers(db, group_id)
        # Historical batches remain private to their creators, with invoice snapshots intact.
        db.execute('UPDATE batches SET group_id=NULL,project_id=NULL,quota_id=NULL WHERE group_id=?', (group_id,))
        db.execute('UPDATE invoices SET project_id=NULL WHERE project_id IN (SELECT id FROM projects WHERE group_id=?)', (group_id,))
        db.execute('DELETE FROM projects WHERE group_id=?', (group_id,))
        db.execute('DELETE FROM quotas WHERE group_id=?', (group_id,))
        db.execute('DELETE FROM groups WHERE id=?', (group_id,))
    return {'ok': True, 'cancelled_batches': cancelled}


@router.get('/api/groups/{group_id}/members')
def members(group_id: str, user: dict = Depends(require_user)):
    with connection() as db:
        require_group(db, user['id'], group_id)
        return [dict(row) for row in db.execute('''SELECT u.id,u.email,u.name,m.role FROM users u
            JOIN group_members m ON m.user_id=u.id WHERE m.group_id=? ORDER BY m.joined_at,u.id''', (group_id,))]


@router.post('/api/groups/{group_id}/invites')
def create_invite(group_id: str, user: dict = Depends(require_user)):
    with LOCK, connection(write=True) as db:
        require_group(db, user['id'], group_id, admin=True)
        token = secrets.token_urlsafe(24)
        expiry = int(time.time()) + 7 * 86400
        db.execute('INSERT INTO group_invites VALUES (?,?,?,?,1,0)',
                   (hashlib.sha256(token.encode()).hexdigest(), group_id, user['id'], expiry))
    return {'token': token, 'expires_at': datetime.fromtimestamp(expiry, timezone.utc).isoformat()}


@router.post('/api/groups/join')
def join_group(values: dict, user: dict = Depends(require_user)):
    token = values.get('token', '')
    if not isinstance(token, str) or not 1 <= len(token.strip()) <= 100:
        raise HTTPException(400, '邀请码无效或已过期')
    digest = hashlib.sha256(token.strip().encode()).hexdigest()
    with LOCK, connection(write=True) as db:
        invitation = db.execute('''SELECT i.* FROM group_invites i JOIN group_members m
            ON m.group_id=i.group_id AND m.user_id=i.created_by
            WHERE i.token_hash=? AND i.expires_at>? AND i.uses<i.max_uses AND m.role IN ('owner','admin')''',
            (digest, int(time.time()))).fetchone()
        if not invitation:
            raise HTTPException(400, '邀请码无效、已使用或已过期')
        group_id = invitation['group_id']
        if not db.execute('SELECT 1 FROM group_members WHERE group_id=? AND user_id=?', (group_id, user['id'])).fetchone():
            db.execute('INSERT INTO group_members VALUES (?,?,?,?)', (group_id, user['id'], 'member', _now()))
            db.execute('UPDATE group_invites SET uses=uses+1 WHERE token_hash=?', (digest,))
        return _group(db, group_id, user['id'])


@router.delete('/api/groups/{group_id}/members/{member_id}')
def remove_member(group_id: str, member_id: str, user: dict = Depends(require_user)):
    with LOCK, connection(write=True) as db:
        actor = require_group(db, user['id'], group_id)
        target = require_group(db, member_id, group_id)
        if target['role'] == 'owner':
            raise HTTPException(409, '群主须先转让团队后退出，或解散团队；不能移除群主')
        if member_id != user['id']:
            if actor['role'] not in ('owner', 'admin') or (actor['role'] == 'admin' and target['role'] != 'member'):
                raise HTTPException(403, '无权移除此成员')
        cancelled = _cancel_batches(db, group_id, member_id)
        _detach_receivers(db, group_id, member_id)
        db.execute('DELETE FROM invoice_shares WHERE group_id=? AND invoice_id IN (SELECT id FROM invoices WHERE owner_id=?)', (group_id, member_id))
        db.execute('DELETE FROM group_invites WHERE group_id=? AND created_by=?', (group_id, member_id))
        db.execute('DELETE FROM group_members WHERE group_id=? AND user_id=?', (group_id, member_id))
    return {'ok': True, 'cancelled_batches': cancelled}


@router.patch('/api/groups/{group_id}/members/{member_id}')
def change_role(group_id: str, member_id: str, values: dict, user: dict = Depends(require_user)):
    role = values.get('role')
    if role not in ('admin', 'member'):
        raise HTTPException(400, '角色必须为管理员或成员')
    with LOCK, connection(write=True) as db:
        actor = require_group(db, user['id'], group_id)
        if actor['role'] != 'owner':
            raise HTTPException(403, '仅群主可调整管理员角色')
        target = require_group(db, member_id, group_id)
        if target['role'] == 'owner':
            raise HTTPException(409, '群主角色须通过转让团队修改')
        db.execute('UPDATE group_members SET role=? WHERE group_id=? AND user_id=?', (role, group_id, member_id))
        if role == 'member':
            db.execute('DELETE FROM group_invites WHERE group_id=? AND created_by=?', (group_id, member_id))
        row = db.execute('SELECT id,email,name FROM users WHERE id=?', (member_id,)).fetchone()
        return {**dict(row), 'role': role}


@router.get('/api/invoices/{invoice_id}/shares')
def invoice_shares(invoice_id: str, user: dict = Depends(require_user)):
    with connection() as db:
        _owned_invoice(db, invoice_id, user['id'])
        return {'group_ids': [row['group_id'] for row in db.execute('SELECT group_id FROM invoice_shares WHERE invoice_id=? ORDER BY group_id', (invoice_id,))]}


@router.put('/api/invoices/{invoice_id}/shares')
def update_shares(invoice_id: str, values: dict, user: dict = Depends(require_user)):
    group_ids = values.get('group_ids')
    if not isinstance(group_ids, list) or len(group_ids) > 100 or any(not isinstance(value, str) or not value for value in group_ids):
        raise HTTPException(400, '请选择有效的分享组，最多 100 个')
    wanted = set(group_ids)
    with LOCK, connection(write=True) as db:
        invoice = _owned_invoice(db, invoice_id, user['id'])
        for group_id in wanted:
            require_group(db, user['id'], group_id)
        existing = {row['group_id'] for row in db.execute('SELECT group_id FROM invoice_shares WHERE invoice_id=?', (invoice_id,))}
        for group_id in existing - wanted:
            if _active_share(db, invoice_id, group_id):
                raise HTTPException(409, '该组报销单正在占用此发票，请先取消、驳回或完成报销')
        for group_id in wanted - existing:
            duplicate = db.execute('''SELECT 1 FROM invoice_shares s JOIN invoices i ON i.id=s.invoice_id
                WHERE s.group_id=? AND i.id!=? AND ((?!='' AND i.number=?) OR (? IS NOT NULL AND i.file_hash=?)) LIMIT 1''',
                (group_id, invoice_id, invoice['number'], invoice['number'], invoice['file_hash'], invoice['file_hash'])).fetchone()
            if duplicate:
                raise HTTPException(409, '该组中已有相同号码或原件的发票，请核对是否重复')
        for group_id in existing - wanted:
            db.execute('DELETE FROM invoice_shares WHERE invoice_id=? AND group_id=?', (invoice_id, group_id))
        for group_id in wanted - existing:
            db.execute('INSERT INTO invoice_shares VALUES (?,?,?,?)', (invoice_id, group_id, user['id'], _now()))
    return {'group_ids': sorted(wanted)}
