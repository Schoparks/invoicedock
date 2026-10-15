"""Password authentication with hashed opaque sessions and persistent rate limits."""
import hashlib
import hmac
import ipaddress
import os
import re
import secrets
import time
import uuid
import unicodedata
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request, Response

from .db import LOCK, connection

router = APIRouter()
COOKIE_NAME = 'invoice_session'
SESSION_SECONDS = 7 * 24 * 3600
SCRYPT_N = 2 ** 15


def enabled(name, default=False):
    return os.environ.get(name, str(default)).lower() in ('true', '1', 'yes', 'on')


def _public_user(row):
    return {key: row[key] for key in ('id', 'email', 'name')}


def _token_hash(token):
    return hashlib.sha256(token.encode()).hexdigest()


def password_hash(password):
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=SCRYPT_N, r=8, p=1, dklen=32, maxmem=64 * 1024 * 1024)
    return 'scrypt-v1$' + salt.hex() + '$' + digest.hex()


def verify_password(password, encoded):
    try:
        version, salt_hex, digest_hex = encoded.split('$')
        salt, expected = bytes.fromhex(salt_hex), bytes.fromhex(digest_hex)
        if version != 'scrypt-v1' or len(salt) != 16 or len(expected) != 32:
            raise ValueError
    except (AttributeError, ValueError):
        salt, expected = bytes(16), bytes(32)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=SCRYPT_N, r=8, p=1, dklen=32, maxmem=64 * 1024 * 1024)
    return hmac.compare_digest(digest, expected)


def _email(value):
    if not isinstance(value, str) or len(value) > 254:
        raise HTTPException(400, '请填写有效邮箱')
    value = value.strip().lower()
    if not re.fullmatch(r'[^\s@]+@[^\s@]+\.[^\s@]+', value):
        raise HTTPException(400, '请填写有效邮箱')
    return value


def _name(value):
    if not isinstance(value, str):
        raise HTTPException(400, '用户名须为 1 至 80 个字符')
    value = value.strip()
    if not 1 <= len(value) <= 80 or any(unicodedata.category(c).startswith('C') for c in value):
        raise HTTPException(400, '用户名须为 1 至 80 个字符，不能包含控制字符')
    return value


def _password(value, registering=False):
    if not isinstance(value, str) or not 1 <= len(value) <= 128:
        raise HTTPException(400, '密码长度应为 10 至 128 个字符' if registering else '邮箱或密码不正确')
    if registering and len(value) < 10:
        raise HTTPException(400, '密码至少 10 个字符')
    return value


def _rate_limit(request, identity, registration=False, purpose='login'):
    stamp = int(time.time())
    address = request.client.host if request.client else 'unknown'
    # Each bucket is hashed so the limiter does not create an extra email/IP log.
    rules = [('register-ip:' + address, 10)] if registration else [(purpose + '-ip:' + address, 60), (purpose + '-identity:' + identity, 10)]
    with LOCK, connection(write=True) as db:
        db.execute('DELETE FROM auth_attempts WHERE window_start<?', (stamp - 900,))
        for value, limit in rules:
            bucket = _token_hash(value)
            row = db.execute('SELECT count FROM auth_attempts WHERE bucket=?', (bucket,)).fetchone()
            if row and row['count'] >= limit:
                raise HTTPException(429, '尝试次数过多，请在 15 分钟后重试', headers={'Retry-After': '900'})
        for value, _ in rules:
            db.execute('INSERT INTO auth_attempts VALUES (?,1,?) ON CONFLICT(bucket) DO UPDATE SET count=count+1', (_token_hash(value), stamp))


def current_user(request: Request) -> dict | None:
    token = request.cookies.get(COOKIE_NAME, '')
    if not token or len(token) > 100:
        return None
    with connection() as db:
        row = db.execute('''SELECT u.id,u.email,u.name FROM users u JOIN sessions s ON s.user_id=u.id
            WHERE s.token_hash=? AND s.expires_at>?''', (_token_hash(token), int(time.time()))).fetchone()
    return _public_user(row) if row else None


def require_user(request: Request) -> dict:
    user = current_user(request)
    if not user:
        raise HTTPException(401, '请先登录')
    return user


def _start_session(db, request, response, user_id):
    stamp = int(time.time())
    db.execute('DELETE FROM sessions WHERE expires_at<=?', (stamp,))
    old_token = request.cookies.get(COOKIE_NAME, '')
    if old_token and len(old_token) <= 100:
        db.execute('DELETE FROM sessions WHERE token_hash=?', (_token_hash(old_token),))
    token = secrets.token_urlsafe(32)
    db.execute('INSERT INTO sessions VALUES (?,?,?)', (_token_hash(token), user_id, stamp + SESSION_SECONDS))
    response.set_cookie(COOKIE_NAME, token, max_age=SESSION_SECONDS, httponly=True,
                        secure=enabled('COOKIE_SECURE'), samesite='lax', path='/')


def _may_claim_legacy(request):
    if not enabled('CLAIM_LEGACY_DATA'):
        return False
    try:
        loopback = ipaddress.ip_address(request.client.host).is_loopback
    except (ValueError, AttributeError):
        loopback = False
    return loopback and request.url.hostname in ('127.0.0.1', 'localhost', '::1')


@router.get('/api/auth/status')
def auth_status(request: Request):
    return {'user': current_user(request), 'registration_open': enabled('ALLOW_REGISTRATION', True)}


@router.post('/api/auth/register')
def register(values: dict, request: Request, response: Response):
    if not enabled('ALLOW_REGISTRATION', True):
        raise HTTPException(403, '注册已关闭，请联系服务维护者')
    email = _email(values.get('email'))
    password = _password(values.get('password'), registering=True)
    name = _name(values.get('name', ''))
    _rate_limit(request, email, registration=True)
    encoded = password_hash(password)
    with LOCK, connection(write=True) as db:
        if db.execute('SELECT 1 FROM users WHERE email=?', (email,)).fetchone():
            raise HTTPException(409, '该邮箱已注册，请登录')
        first = db.execute('SELECT 1 FROM users LIMIT 1').fetchone() is None
        user = {'id': uuid.uuid4().hex, 'email': email, 'name': name}
        db.execute('INSERT INTO users VALUES (?,?,?,?,?)', (user['id'], email, user['name'], encoded, datetime.now(timezone.utc).isoformat()))
        if first and _may_claim_legacy(request):
            for table in ('invoices', 'projects', 'quotas', 'batches'):
                db.execute(f'UPDATE {table} SET owner_id=? WHERE owner_id IS NULL', (user['id'],))
        _start_session(db, request, response, user['id'])
    return {'user': user}


@router.post('/api/auth/login')
def login(values: dict, request: Request, response: Response):
    email = _email(values.get('email'))
    password = _password(values.get('password'))
    _rate_limit(request, email)
    with LOCK, connection(write=True) as db:
        row = db.execute('SELECT * FROM users WHERE email=?', (email,)).fetchone()
        if not verify_password(password, row['password_hash'] if row else ''):
            raise HTTPException(401, '邮箱或密码不正确')
        db.execute('DELETE FROM auth_attempts WHERE bucket=?', (_token_hash('login-identity:' + email),))
        _start_session(db, request, response, row['id'])
    return {'user': _public_user(row)}


def _account_password(values, request, user_id):
    _rate_limit(request, user_id, purpose='account')
    password = values.get('current_password')
    if not isinstance(password, str) or not 1 <= len(password) <= 128:
        raise HTTPException(400, '请填写当前密码（最多 128 个字符）')
    return password


def _checked_account(db, request, user_id, password):
    # Recheck the session inside the write transaction so a revoked request cannot
    # change credentials after a concurrent password/email update.
    row = db.execute("""SELECT u.* FROM users u JOIN sessions s ON s.user_id=u.id
        WHERE u.id=? AND s.token_hash=? AND s.expires_at>?""",
        (user_id, _token_hash(request.cookies.get(COOKIE_NAME, '')), int(time.time()))).fetchone()
    if row is None:
        raise HTTPException(401, '请先登录')
    if not verify_password(password, row['password_hash']):
        raise HTTPException(403, '当前密码不正确')
    return row


def _finish_account_update(db, request, response, user_id, renew=False):
    db.execute('DELETE FROM auth_attempts WHERE bucket=?', (_token_hash('account-identity:' + user_id),))
    if renew:
        db.execute('DELETE FROM sessions WHERE user_id=?', (user_id,))
        _start_session(db, request, response, user_id)
    return {'user': _public_user(db.execute('SELECT * FROM users WHERE id=?', (user_id,)).fetchone())}


@router.patch('/api/auth/profile')
def update_profile(values: dict, request: Request, response: Response, user=Depends(require_user)):
    if not any(key in values for key in ('name', 'email')):
        raise HTTPException(400, '请提供要修改的用户名或邮箱')
    name = _name(values['name']) if 'name' in values else None
    email = _email(values['email']) if 'email' in values else None
    password = _account_password(values, request, user['id'])
    with LOCK, connection(write=True) as db:
        row = _checked_account(db, request, user['id'], password)
        if email and db.execute('SELECT 1 FROM users WHERE email=? AND id!=?', (email, user['id'])).fetchone():
            raise HTTPException(409, '该邮箱已被其他账号使用')
        db.execute('UPDATE users SET name=?,email=? WHERE id=?',
                   (name if name is not None else row['name'], email or row['email'], user['id']))
        return _finish_account_update(db, request, response, user['id'], renew=email is not None and email != row['email'])


@router.post('/api/auth/password')
def change_password(values: dict, request: Request, response: Response, user=Depends(require_user)):
    new_password = _password(values.get('new_password'), registering=True)
    password = _account_password(values, request, user['id'])
    with LOCK, connection(write=True) as db:
        _checked_account(db, request, user['id'], password)
        db.execute('UPDATE users SET password_hash=? WHERE id=?', (password_hash(new_password), user['id']))
        return _finish_account_update(db, request, response, user['id'], renew=True)


@router.post('/api/auth/logout')
def logout(request: Request, response: Response):
    token = request.cookies.get(COOKIE_NAME, '')
    if token and len(token) <= 100:
        with LOCK, connection(write=True) as db:
            db.execute('DELETE FROM sessions WHERE token_hash=?', (_token_hash(token),))
    response.delete_cookie(COOKIE_NAME, httponly=True, secure=enabled('COOKIE_SECURE'), samesite='lax', path='/')
    return {'ok': True}
