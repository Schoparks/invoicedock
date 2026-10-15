"""Shared authorization rules. IDs supplied by the browser never grant access."""
from fastapi import HTTPException


def require_group(db, user_id, group_id, admin=False):
    if not isinstance(group_id, str) or not group_id:
        raise HTTPException(403, '无权访问此组')
    row = db.execute('SELECT * FROM group_members WHERE group_id=? AND user_id=?', (group_id, user_id)).fetchone()
    if not row or (admin and row['role'] not in ('owner', 'admin')):
        raise HTTPException(403, '需要该组管理员权限' if admin else '无权访问此组')
    return dict(row)


def invoice_access(db, invoice, user_id, group_id=None, edit=False):
    invoice = dict(invoice)
    owned = invoice.get('owner_id') == user_id
    if edit and not owned:
        return False
    if group_id is None:
        return owned
    membership = db.execute('SELECT 1 FROM group_members WHERE group_id=? AND user_id=?', (group_id, user_id)).fetchone()
    shared = db.execute('SELECT 1 FROM invoice_shares WHERE group_id=? AND invoice_id=?', (group_id, invoice['id'])).fetchone()
    return bool(membership and shared)


def scope_access(db, row, user_id, group_id=None, admin=False):
    row = dict(row)
    if row.get('group_id'):
        if group_id is not None and group_id != row['group_id']:
            return False
        require_group(db, user_id, row['group_id'], admin=admin)
        return True
    return group_id is None and row.get('owner_id') == user_id
