"""Account-isolated invoice pools, selective group sharing and reimbursement."""
import hashlib
import io
import os
import json
import re
import shutil
import sqlite3
import tempfile
import uuid
import zipfile
from contextlib import asynccontextmanager
from datetime import date, datetime
from pathlib import Path
from urllib.parse import quote, urlsplit

from fastapi import Depends, FastAPI, File, HTTPException, Request, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.background import BackgroundTask

from .db import DATA, ROOT, LOCK, connection, initialize
from .matching import best_combinations
from .parsing import REVIEW_NOTE, expand_upload, parse_invoice
from .auth import current_user, require_user, router as auth_router
from .groups import router as groups_router
from .permissions import require_group, invoice_access
from . import mailbox, inbound

CATEGORIES = ['餐饮', '交通', '住宿', '办公', '通讯', '其他']
INVOICE_FIELDS = ['number', 'seller', 'buyer', 'date', 'category', 'amount_cents', 'project_id', 'note', 'verified']
STATUS_NAMES = {'available': '未使用', 'draft': '草稿占用', 'submitted': '已提交', 'paid': '已报销', 'cancelled': '已取消', 'rejected': '已驳回'}


def now():
    return datetime.now().astimezone().isoformat(timespec='seconds')


def uid():
    return uuid.uuid4().hex


def fail(message, status=400):
    raise HTTPException(status, message)


def text_value(value, label, max_length=200, required=False):
    if not isinstance(value, str):
        fail(f'{label}格式不正确')
    value = value.strip()
    if len(value) > max_length:
        fail(f'{label}最多 {max_length} 个字符')
    if required and not value:
        fail(f'请填写{label}')
    return value


def cents(value, zero=False, maximum=100_000_000):
    if type(value) is not int or not (0 if zero else 1) <= value <= maximum:
        fail('金额须为有效的整数分，最多 1,000,000 元')
    return value


def valid_date(value, empty=False):
    if empty and not value:
        return ''
    if not isinstance(value, str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}', value):
        fail('日期格式应为 YYYY-MM-DD')
    try:
        date.fromisoformat(value)
    except ValueError:
        fail('日期不存在')
    return value


def valid_month(value):
    if not isinstance(value, str) or not re.fullmatch(r'\d{4}-\d{2}', value):
        fail('月份格式应为 YYYY-MM')
    valid_date(value + '-01')
    return value


def fetch(db, table, object_id):
    row = db.execute(f'SELECT * FROM {table} WHERE id=?', (object_id,)).fetchone()
    if not row:
        fail('记录不存在', 404)
    return dict(row)


def invoice_dict(row, raw=False, user_id=None, db=None):
    item = dict(row)
    item['verified'] = bool(item['verified'])
    item['file_url'] = f"/api/invoices/{item['id']}/file" if item['file_path'] else ''
    item.pop('file_path', None)
    item.pop('file_hash', None)
    item['can_edit'] = item.get('owner_id') == user_id
    item['shared_group_ids'] = [r[0] for r in db.execute('SELECT group_id FROM invoice_shares WHERE invoice_id=?', (item['id'],))] if db is not None and item['can_edit'] else []
    if not raw:
        item.pop('raw_text', None)
    return item


def quota_dict(db, row):
    item = dict(row)
    used = db.execute("SELECT COALESCE(SUM(total_cents),0) FROM batches WHERE quota_id=? AND status IN ('draft','submitted','paid')", (item['id'],)).fetchone()[0]
    item.update(used_cents=used, remaining_cents=max(0, item['amount_cents'] - used))
    return item


def batch_dict(db, row):
    item = dict(row)
    item['items'] = []
    for entry in db.execute('SELECT b.snapshot,i.file_path,i.file_name FROM batch_items b JOIN invoices i ON i.id=b.invoice_id WHERE b.batch_id=? ORDER BY b.rowid', (item['id'],)):
        invoice = json.loads(entry['snapshot'])
        invoice['file_url'] = f"/api/batches/{item['id']}/invoices/{invoice['id']}/file" if entry['file_path'] else ''
        invoice['original_file_name'] = entry['file_name']
        item['items'].append(invoice)
    item['invoice_ids'] = [r['id'] for r in item['items']]
    return item


def validate_invoice(db, values, old=None, user_id=None):
    data = {k: (old[k] if old else v) for k, v in dict(number='', seller='', buyer='', date='', category='其他', amount_cents=0, project_id=None, note='', verified=False).items()}
    for field in INVOICE_FIELDS:
        if field in values:
            data[field] = values[field]
    for field, label in [('number', '发票号码'), ('seller', '销售方'), ('buyer', '购买方'), ('note', '备注')]:
        data[field] = text_value(data[field], label, 2000 if field == 'note' else 200)
    data['number'] = re.sub(r'\s+', '', data['number'])
    data['date'] = valid_date(data['date'], empty=True)
    if data['category'] not in CATEGORIES:
        fail('请选择有效的费用类别')
    data['amount_cents'] = cents(data['amount_cents'], zero=True)
    if type(data['verified']) not in (bool, int) or data['verified'] not in (0, 1):
        fail('核对状态不正确')
    data['verified'] = bool(data['verified'])
    data['project_id'] = data['project_id'] or None
    if data['project_id']:
        authorize_scope(db, fetch(db, 'projects', data['project_id']), user_id)
    if data['verified'] and (not data['seller'] or not data['date'] or data['amount_cents'] <= 0):
        fail('确认核对前，请填写销售方、开票日期和大于零的金额')
    if data['number']:
        duplicate = db.execute('SELECT id FROM invoices WHERE number=? AND owner_id=?', (data['number'], user_id)).fetchone()
        if duplicate and (old is None or duplicate['id'] != old['id']):
            fail('该发票号码已在票库中，请检查是否重复', 409)
    return data


@asynccontextmanager
async def lifespan(app):
    if os.environ.get('INVOICE_MODE') == 'production' and os.environ.get('COOKIE_SECURE', '').lower() not in ('1','true','yes','on'):
        raise RuntimeError('线上模式需要 COOKIE_SECURE=true，并通过 HTTPS 反向代理访问')
    initialize()
    mailbox.initialize_mailboxes()
    mailbox.configure_importer(persist_import)
    inbound.initialize_inbound()
    inbound.configure_importer(persist_import)
    mailbox.start_mailbox_worker()
    inbound.start_inbound_worker()
    try:
        yield
    finally:
        mailbox.stop_mailbox_worker()
        inbound.stop_inbound_worker()


app = FastAPI(title='票栈 · 发票与报销管理', lifespan=lifespan, docs_url=None, redoc_url=None)
app.add_middleware(TrustedHostMiddleware, allowed_hosts=[h.strip() for h in os.environ.get('INVOICE_ALLOWED_HOSTS', '127.0.0.1,localhost,[::1],testserver').split(',') if h.strip()])
app.include_router(auth_router)
app.include_router(groups_router)
app.include_router(mailbox.router)
app.include_router(inbound.router)


@app.middleware('http')
async def local_only(request: Request, call_next):
    public = {'/api/auth/status', '/api/auth/login', '/api/auth/register'}
    signed_email = request.method == 'POST' and request.url.path == '/api/inbound/email'
    if request.url.path.startswith('/api/') and request.url.path not in public and not signed_email and current_user(request) is None:
        return JSONResponse({'detail': '请先登录'}, status_code=401)
    if request.headers.get('content-length', '').isdigit() and int(request.headers['content-length']) > 110 * 1024 * 1024:
        return JSONResponse({'detail': '上传请求超过 110 MB，请分批导入'}, status_code=413)
    if request.method not in ('GET', 'HEAD', 'OPTIONS') and not signed_email:
        origin = request.headers.get('origin')
        if origin and urlsplit(origin).netloc != request.headers.get('host'):
            return JSONResponse({'detail': '不允许跨站请求'}, status_code=403)
        if request.headers.get('x-requested-with') != 'InvoiceDesk':
            return JSONResponse({'detail': '请求来源无效，请在应用页面中操作'}, status_code=403)
    response = await call_next(request)
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['Referrer-Policy'] = 'no-referrer'
    response.headers['Content-Security-Policy'] = "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; connect-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
    if request.url.path.startswith('/api/'):
        response.headers['Cache-Control'] = 'no-store'
    return response


@app.exception_handler(RequestValidationError)
async def invalid_request(request, exc):
    return JSONResponse({'detail': '请求字段或上传文件格式不正确'}, status_code=422)


@app.exception_handler(sqlite3.IntegrityError)
async def invalid_database(request, exc):
    return JSONResponse({'detail': '记录重复或关联已变更，请刷新后重试'}, status_code=409)


def authorize_scope(db, row, user_id, admin=False):
    if row.get('group_id'):
        return require_group(db, user_id, row['group_id'], admin=admin)
    if row.get('owner_id') != user_id:
        fail('无权访问此记录', 403)
    return None


def scoped_rows(db, table, user_id, group_id=None):
    if group_id:
        require_group(db, user_id, group_id)
        return db.execute(f'SELECT * FROM {table} WHERE group_id=? ORDER BY rowid DESC', (group_id,)).fetchall()
    return db.execute(f'SELECT * FROM {table} WHERE owner_id=? AND group_id IS NULL ORDER BY rowid DESC', (user_id,)).fetchall()


def visible_invoices(db, user_id, group_id=None):
    if group_id:
        require_group(db, user_id, group_id)
        rows = db.execute('SELECT i.*,u.name AS owner_name FROM invoices i JOIN invoice_shares s ON s.invoice_id=i.id LEFT JOIN users u ON u.id=i.owner_id WHERE s.group_id=? ORDER BY i.created_at DESC,i.id', (group_id,))
    else:
        rows = db.execute('SELECT i.*,u.name AS owner_name FROM invoices i LEFT JOIN users u ON u.id=i.owner_id WHERE i.owner_id=? ORDER BY i.created_at DESC,i.id', (user_id,))
    return [invoice_dict(r, user_id=user_id, db=db) for r in rows]


def share_import(db, invoice_id, owner_id, group_id):
    if not group_id:
        return
    require_group(db, owner_id, group_id)
    invoice = fetch(db, 'invoices', invoice_id)
    duplicate = db.execute("SELECT 1 FROM invoices i JOIN invoice_shares s ON s.invoice_id=i.id WHERE s.group_id=? AND i.id!=? AND ((?<>'' AND i.number=?) OR (? IS NOT NULL AND i.file_hash=?))", (group_id, invoice_id, invoice['number'], invoice['number'], invoice['file_hash'], invoice['file_hash'])).fetchone()
    if duplicate:
        fail('此组票池已有同号码或同内容的发票，请核对后处理', 409)
    db.execute('INSERT OR IGNORE INTO invoice_shares(invoice_id,group_id,shared_by,created_at) VALUES (?,?,?,?)', (invoice_id, group_id, owner_id, now()))


@app.get('/api/state')
def state(group_id: str | None = None, user: dict = Depends(require_user)):
    with connection() as db:
        invoices = visible_invoices(db, user['id'], group_id)
        batches = [batch_dict(db, r) for r in scoped_rows(db, 'batches', user['id'], group_id)]
        for batch in batches:
            role = require_group(db, user['id'], group_id)['role'] if group_id else None
            batch['can_manage'] = batch['owner_id'] == user['id'] or role in ('owner', 'admin')
        groups = [dict(r) for r in db.execute('SELECT g.id,g.name,m.role,(SELECT COUNT(*) FROM group_members m2 WHERE m2.group_id=g.id) AS member_count FROM groups g JOIN group_members m ON m.group_id=g.id WHERE m.user_id=? ORDER BY g.created_at', (user['id'],))]
        return dict(user=user, groups=groups, active_group_id=group_id, invoices=invoices, batches=batches,
            projects=[dict(r) for r in scoped_rows(db, 'projects', user['id'], group_id)],
            quotas=[quota_dict(db, r) for r in scoped_rows(db, 'quotas', user['id'], group_id)],
            summary=dict(total_count=len(invoices), total_cents=sum(i['amount_cents'] for i in invoices),
              available_cents=sum(i['amount_cents'] for i in invoices if i['status'] == 'available' and i['verified']),
              pending_review=sum(not i['verified'] for i in invoices),
              submitted_cents=sum(i['amount_cents'] for i in invoices if i['status'] == 'submitted'),
              paid_cents=sum(i['amount_cents'] for i in invoices if i['status'] == 'paid')),
            capabilities=dict(ocr=bool(shutil.which('tesseract')), local_only=os.environ.get('INVOICE_MODE','local')=='local', categories=CATEGORIES))


@app.post('/api/invoices')
def create_invoice(values: dict, user: dict = Depends(require_user)):
    with LOCK, connection(write=True) as db:
        group_id = values.get('group_id') or None
        if group_id:
            require_group(db, user['id'], group_id)
        data = validate_invoice(db, values, user_id=user['id'])
        invoice_id = uid()
        fields = ['id', *INVOICE_FIELDS, 'created_at', 'owner_id']
        db.execute(f"INSERT INTO invoices ({','.join(fields)}) VALUES ({','.join('?' for _ in fields)})", [invoice_id, *[data[k] for k in INVOICE_FIELDS], now(), user['id']])
        share_import(db, invoice_id, user['id'], group_id)
        return invoice_dict(fetch(db, 'invoices', invoice_id), user_id=user['id'], db=db)


@app.patch('/api/invoices/{invoice_id}')
def update_invoice(invoice_id: str, values: dict, user: dict = Depends(require_user)):
    with LOCK, connection(write=True) as db:
        old = fetch(db, 'invoices', invoice_id)
        if old['owner_id'] != user['id']:
            fail('仅发票所有者可以修改内容', 403)
        if old['status'] != 'available':
            fail('该发票已被报销单占用，不能修改', 409)
        data = validate_invoice(db, values, old, user_id=user['id'])
        if data['number']:
            collision = db.execute('SELECT 1 FROM invoice_shares mine JOIN invoice_shares other ON mine.group_id=other.group_id JOIN invoices i ON i.id=other.invoice_id WHERE mine.invoice_id=? AND i.id!=? AND i.number=?', (invoice_id, invoice_id, data['number'])).fetchone()
            if collision:
                fail('某个已共享的组中已有此发票号码，请先核对重复记录', 409)
        db.execute(f"UPDATE invoices SET {','.join(k+'=?' for k in INVOICE_FIELDS)} WHERE id=?", [*[data[k] for k in INVOICE_FIELDS], invoice_id])
        return invoice_dict(fetch(db, 'invoices', invoice_id), user_id=user['id'], db=db)


@app.delete('/api/invoices/{invoice_id}')
def delete_invoice(invoice_id: str, user: dict = Depends(require_user)):
    with LOCK, connection(write=True) as db:
        old = fetch(db, 'invoices', invoice_id)
        if old['owner_id'] != user['id']:
            fail('仅发票所有者可以删除', 403)
        if old['status'] != 'available' or db.execute('SELECT 1 FROM batch_items WHERE invoice_id=?', (invoice_id,)).fetchone():
            fail('有报销记录的发票需保留，不能删除', 409)
        db.execute('DELETE FROM invoice_shares WHERE invoice_id=?', (invoice_id,))
        db.execute('DELETE FROM invoices WHERE id=?', (invoice_id,))
        if old['file_path']:
            (DATA / old['file_path']).unlink(missing_ok=True)
    return {'ok': True}


def persist_import(data: bytes, filename: str, owner_id: str, group_id: str | None = None):
    digest = hashlib.sha256(data).hexdigest()
    with LOCK, connection(write=True) as db:
        if group_id:
            require_group(db, owner_id, group_id)
        duplicate = db.execute('SELECT * FROM invoices WHERE file_hash=? AND owner_id=?', (digest, owner_id)).fetchone()
        if duplicate:
            share_import(db, duplicate['id'], owner_id, group_id)
            return {'status': 'duplicate', 'invoice': invoice_dict(duplicate, user_id=owner_id, db=db)}
    parsed = parse_invoice(data, filename)
    obsolete_path = None
    with LOCK, connection(write=True) as db:
        if group_id:
            require_group(db, owner_id, group_id)
        duplicate = db.execute('SELECT * FROM invoices WHERE file_hash=? AND owner_id=?', (digest, owner_id)).fetchone()
        if duplicate:
            share_import(db, duplicate['id'], owner_id, group_id)
            return {'status': 'duplicate', 'invoice': invoice_dict(duplicate, user_id=owner_id, db=db)}
        number = re.sub(r'\s+', '', parsed.get('number', ''))
        duplicate = db.execute('SELECT * FROM invoices WHERE number=? AND owner_id=?', (number, owner_id)).fetchone() if number else None
        if duplicate:
            share_import(db, duplicate['id'], owner_id, group_id)
            updates = {}
            # 补全空字段，不覆盖用户已核对的内容或报销中的记录。
            if not duplicate['verified'] and duplicate['status'] == 'available':
                updates = {key: parsed[key] for key in ('seller', 'buyer', 'date', 'amount_cents')
                           if not duplicate[key] and parsed.get(key)}
                if updates:
                    validate_invoice(db, updates, old=duplicate, user_id=owner_id)
                    updates['review_note'] = REVIEW_NOTE + ' 同票多格式附件已合并。'
            # 同票只留一份原件；有 PDF 时优先保留 PDF，与附件处理顺序无关。
            if Path(filename).suffix.lower() == '.pdf' and Path(duplicate['file_name']).suffix.lower() != '.pdf':
                relative = f"originals/{duplicate['id']}.pdf"
                (DATA / relative).write_bytes(data)
                obsolete_path = duplicate['file_path']
                updates.update(file_name=Path(filename).name, file_path=relative, file_hash=digest,
                               source=parsed['source'], raw_text=parsed.get('raw_text', '')[:100000])
            if updates:
                db.execute(f"UPDATE invoices SET {','.join(key+'=?' for key in updates)} WHERE id=?", [*updates.values(), duplicate['id']])
            result = {'status': 'duplicate', 'invoice': invoice_dict(fetch(db, 'invoices', duplicate['id']), user_id=owner_id, db=db)}
        else:
            parsed['verified'] = False
            values = validate_invoice(db, parsed, user_id=owner_id)
            invoice_id = uid()
            relative = f"originals/{invoice_id}{Path(filename).suffix.lower()}"
            fields = ['id', *INVOICE_FIELDS, 'file_name', 'file_path', 'file_hash', 'source', 'review_note', 'raw_text', 'created_at', 'owner_id']
            db.execute(f"INSERT INTO invoices ({','.join(fields)}) VALUES ({','.join('?' for _ in fields)})", [invoice_id, *[values[k] for k in INVOICE_FIELDS], Path(filename).name, relative, digest, parsed.get('source','文件导入'), parsed.get('review_note','请核对识别内容'), parsed.get('raw_text','')[:100000], now(), owner_id])
            share_import(db, invoice_id, owner_id, group_id)
            (DATA / relative).write_bytes(data)
            result = {'status': 'imported', 'invoice': invoice_dict(fetch(db, 'invoices', invoice_id), user_id=owner_id, db=db)}
    if obsolete_path:
        (DATA / obsolete_path).unlink(missing_ok=True)
    return result


@app.post('/api/import')
def import_files(files: list[UploadFile] = File(...), group_id: str | None = None, user: dict = Depends(require_user)):
    with connection() as db:
        if group_id:
            require_group(db, user['id'], group_id)
    if len(files) > 200:
        fail('一次最多上传 200 个文件')
    result = dict(imported=[], duplicates=[], errors=[])
    total_size = total_count = 0
    for upload in files:
        filename = Path(upload.filename or '未命名').name
        try:
            blob = upload.file.read(100 * 1024 * 1024 + 1)
            if len(blob) > 100 * 1024 * 1024:
                raise ValueError('单个上传包不得超过 100 MB')
            entries = expand_upload(blob, filename)
        except ValueError as exc:
            result['errors'].append(dict(filename=filename, error=str(exc)))
            continue
        finally:
            upload.file.close()
        for name, data in entries:
            total_size += len(data)
            total_count += 1
            if total_size > 100 * 1024 * 1024 or total_count > 200:
                result['errors'].append(dict(filename=name, error='本次导入超过 200 张或 100 MB，请分批导入'))
                return result
            try:
                imported = persist_import(data, name, user['id'], group_id)
                if imported['status'] == 'duplicate':
                    result['duplicates'].append(name)
                else:
                    result['imported'].append(imported['invoice'])
            except (ValueError, HTTPException) as exc:
                result['errors'].append(dict(filename=name, error=exc.detail if isinstance(exc, HTTPException) else str(exc)))
    return result


def safe_filename(value, fallback='原件', max_bytes=180):
    value = re.sub(r'[/\\:*?"<>|\x00-\x1f\x7f]', '_', str(value or '')).strip(' .')
    value = value.encode('utf-8')[:max_bytes].decode('utf-8', errors='ignore').rstrip(' .')
    return value or fallback


def original_filename(row, snapshot=None, sequence=None, rename=False, filename=None):
    suffix = Path(row['file_path']).suffix.lower() if rename or filename is not None else Path(row['file_name']).suffix
    if filename is not None:
        if not isinstance(filename, str) or not filename.strip() or len(filename.encode('utf-8')) > 180 or safe_filename(filename) != filename:
            fail('文件名不能为空、不能超过 180 字节或包含 /\\:*?"<>| 等特殊字符及首尾空格、句点')
        stem = filename
    elif rename:
        invoice = snapshot if snapshot is not None else row
        amount = int(invoice.get('amount_cents') or 0)
        stem = (f'{sequence:03}-' if sequence is not None else '') + safe_filename(invoice.get('category'), '其他') + f'-{amount // 100}.{amount % 100:02}'
    else:
        stem = safe_filename(Path(row['file_name']).stem)
    return stem + suffix


def original_response(row, download_file=False, filename=None, preview_page=None):
    if not row['file_path'] or not (DATA / row['file_path']).is_file():
        fail('此记录没有原件', 404)
    if preview_page is not None:
        if preview_page < 1 or Path(row['file_path']).suffix.lower() != '.pdf':
            fail('请选择有效的 PDF 页码')
        import pymupdf
        try:
            with pymupdf.open(DATA / row['file_path']) as document:
                if document.needs_pass:
                    fail('此 PDF 已加密，请下载原件查看')
                if preview_page > document.page_count:
                    fail('PDF 页码超出范围')
                page = document.load_page(preview_page - 1)
                scale = min(2, 2000 / max(page.rect.width, page.rect.height))
                image = page.get_pixmap(matrix=pymupdf.Matrix(scale, scale), alpha=False)
                return Response(image.tobytes('png'), media_type='image/png', headers={'X-Preview-Pages': str(document.page_count)})
        except (RuntimeError, ValueError):
            fail('PDF 无法预览，请下载原件查看')
    types = {'.pdf': 'application/pdf', '.png': 'image/png', '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg', '.webp': 'image/webp', '.xml': 'application/xml'}
    mime = types.get(Path(row['file_path']).suffix.lower())
    name = original_filename(row, filename=filename) if download_file else row['file_name']
    return FileResponse(DATA / row['file_path'], media_type=mime or 'application/octet-stream', filename=name,
                        content_disposition_type='attachment' if download_file or not mime else 'inline')


@app.get('/api/invoices/{invoice_id}/file')
def original_file(invoice_id: str, download: bool = False, preview_page: int | None = None, user: dict = Depends(require_user)):
    with connection() as db:
        row = fetch(db, 'invoices', invoice_id)
        shared = db.execute('SELECT 1 FROM invoice_shares s JOIN group_members m ON m.group_id=s.group_id WHERE s.invoice_id=? AND m.user_id=?', (invoice_id,user['id'])).fetchone()
        if row['owner_id'] != user['id'] and not shared:
            fail('无权查看此原件', 403)
    return original_response(row, download, preview_page=preview_page)


@app.get('/api/batches/{batch_id}/invoices/{invoice_id}/file')
def batch_original_file(batch_id: str, invoice_id: str, download: bool = False, filename: str | None = None, preview_page: int | None = None, user: dict = Depends(require_user)):
    with connection() as db:
        batch = fetch(db, 'batches', batch_id)
        authorize_scope(db, batch, user['id'])
        if not db.execute('SELECT 1 FROM batch_items WHERE batch_id=? AND invoice_id=?', (batch_id, invoice_id)).fetchone():
            fail('此发票不在报销单中', 404)
        row = fetch(db, 'invoices', invoice_id)
    return original_response(row, download, filename, preview_page)


@app.post('/api/projects')
def create_project(values: dict, user: dict = Depends(require_user)):
    name = text_value(values.get('name', ''), '项目名称', required=True)
    note = text_value(values.get('note', ''), '备注', 2000)
    with LOCK, connection(write=True) as db:
        project_id = uid()
        group_id = values.get('group_id') or None
        if group_id:
            require_group(db, user['id'], group_id)
        db.execute('INSERT INTO projects(id,name,note,created_at,owner_id,group_id) VALUES (?,?,?,?,?,?)', (project_id, name, note, now(), user['id'], group_id))
        return fetch(db, 'projects', project_id)


@app.post('/api/quotas')
def create_quota(values: dict, user: dict = Depends(require_user)):
    month = valid_month(values.get('month'))
    category = values.get('category', '全部')
    if category not in ['全部', *CATEGORIES]:
        fail('请选择有效类别')
    amount = cents(values.get('amount_cents'))
    with LOCK, connection(write=True) as db:
        quota_id = uid()
        group_id = values.get('group_id') or None
        if group_id:
            require_group(db, user['id'], group_id, admin=True)
        db.execute('INSERT INTO quotas(id,month,category,amount_cents,owner_id,group_id) VALUES (?,?,?,?,?,?)', (quota_id, month, category, amount, user['id'], group_id))
        return quota_dict(db, fetch(db, 'quotas', quota_id))


@app.delete('/api/quotas/{quota_id}')
def delete_quota(quota_id: str, user: dict = Depends(require_user)):
    with LOCK, connection(write=True) as db:
        authorize_scope(db, fetch(db, 'quotas', quota_id), user['id'], admin=True)
        if db.execute('SELECT 1 FROM batches WHERE quota_id=?', (quota_id,)).fetchone():
            fail('此额度已有关联报销单，需保留记录', 409)
        db.execute('DELETE FROM quotas WHERE id=?', (quota_id,))
    return {'ok': True}


def candidates(db, values, user_id):
    invoices = visible_invoices(db, user_id, values.get('group_id'))
    for key in ('date_from','date_to'):
        if values.get(key):
            valid_date(values[key])
    if values.get('date_from') and values.get('date_to') and values['date_from'] > values['date_to']:
        fail('开始日期不能晚于结束日期')
    return [i for i in invoices if i['status']=='available' and i['verified'] and i['amount_cents']>0
        and all(not values.get(k) or values[k]=='全部' or i[k]==values[k] for k in ('category','project_id','buyer'))
        and (not values.get('date_from') or i['date']>=values['date_from'])
        and (not values.get('date_to') or i['date']<=values['date_to'])]


def quota_for_scope(db, quota_id, user_id, group_id):
    row = fetch(db, 'quotas', quota_id)
    authorize_scope(db, row, user_id)
    if row.get('group_id') != (group_id or None):
        fail('额度与当前个人/群组空间不一致')
    return quota_dict(db, row)


@app.post('/api/match')
def match(values: dict, user: dict = Depends(require_user)):
    target = cents(values.get('target_cents'), maximum=10_000_000)
    direction = values.get('direction', 'above')
    if direction not in ('above', 'below'):
        fail('匹配方式须为稍多或稍少')
    quota = None
    with connection() as db:
        if values.get('quota_id'):
            quota = quota_for_scope(db, values['quota_id'], user['id'], values.get('group_id'))
            if direction == 'above' and target > quota['remaining_cents']:
                fail('额度不足：目标金额超过该月度额度的可用余额')
            category = values.get('category')
            if quota['category'] != '全部':
                if category and category not in ('全部', quota['category']):
                    fail('费用类别与所选月度额度不一致')
                values = {**values, 'category': quota['category']}
        invoices = candidates(db, values, user['id'])
    try:
        search_target = min(target, quota['remaining_cents']) if quota and direction == 'below' else target
        combos = best_combinations(invoices, search_target, direction=direction)
    except ValueError as exc:
        fail(str(exc))
    message = ''
    if not combos:
        if direction == 'below':
            message = '没有不超过目标金额且符合剩余额度的可用发票组合，请调整金额或筛选条件。' if quota else '没有不超过目标金额的可用发票组合，请调整金额或筛选条件。'
        else:
            message = '额度不足：符合条件的可用发票总额低于目标金额，请补充发票或调整筛选条件。'
    elif quota and direction == 'above':
        combos = [combo for combo in combos if combo['total_cents'] <= quota['remaining_cents']]
        if not combos:
            message = '额度不足：达到目标金额的发票组合超出剩余月度额度，请调整额度或筛选条件。'
    index = {i['id']: i for i in invoices}
    for combo in combos:
        combo['gap_cents'] = target - combo['total_cents']
        combo['invoices'] = [index[i] for i in combo['invoice_ids']]
    return dict(combinations=combos, candidate_count=len(invoices), target_cents=target, direction=direction, message=message)


@app.post('/api/batches')
def create_batch(values: dict, user: dict = Depends(require_user)):
    name = text_value(values.get('name', ''), '报销单名称', required=True)
    ids = values.get('invoice_ids')
    if not isinstance(ids, list) or not ids or len(ids) > 1000 or any(not isinstance(i, str) for i in ids) or len(set(ids)) != len(ids):
        fail('请选择 1 至 1000 张不同发票')
    with LOCK, connection(write=True) as db:
        group_id = values.get('group_id') or None
        if group_id:
            require_group(db, user['id'], group_id)
        invoices = [fetch(db, 'invoices', i) for i in ids]
        if any(not invoice_access(db, i, user['id'], group_id) for i in invoices):
            fail('所选发票不在当前可用票池中', 403)
        if any(i['status'] != 'available' or not i['verified'] or i['amount_cents'] <= 0 for i in invoices):
            fail('有发票未核对或已被占用，请刷新后重新选择', 409)
        project_id = values.get('project_id') or None
        quota_id = values.get('quota_id') or None
        month = valid_month(values.get('month') or date.today().isoformat()[:7])
        total = sum(i['amount_cents'] for i in invoices)
        if project_id:
            project = fetch(db, 'projects', project_id)
            authorize_scope(db, project, user['id'])
            if project.get('group_id') != group_id:
                fail('项目与当前个人/群组空间不一致')
            if any(i['project_id'] != project_id for i in invoices):
                fail('所选发票必须全部属于该项目，请先在票库中归类')
        if quota_id:
            quota = quota_for_scope(db, quota_id, user['id'], group_id)
            month = quota['month']
            if quota['category'] != '全部' and any(i['category'] != quota['category'] for i in invoices):
                fail('发票类别与月度额度不一致')
            if total > quota['remaining_cents']:
                fail('发票合计超过月度额度的可用余额', 409)
        batch_id = uid()
        stamp = now()
        db.execute('INSERT INTO batches(id,name,status,project_id,quota_id,month,total_cents,created_at,updated_at,owner_id,group_id) VALUES (?,?,?,?,?,?,?,?,?,?,?)', (batch_id, name, 'draft', project_id, quota_id, month, total, stamp, stamp, user['id'], group_id))
        for invoice in invoices:
            db.execute('INSERT INTO batch_items VALUES (?,?,?)', (batch_id, invoice['id'], json.dumps(invoice_dict(invoice), ensure_ascii=False)))
            db.execute("UPDATE invoices SET status='draft',batch_id=? WHERE id=?", (batch_id, invoice['id']))
        return batch_dict(db, fetch(db, 'batches', batch_id))


@app.patch('/api/batches/{batch_id}')
def update_batch(batch_id: str, values: dict, user: dict = Depends(require_user)):
    status = values.get('status')
    transitions = {'draft': {'submitted', 'cancelled'}, 'submitted': {'paid', 'rejected'}}
    with LOCK, connection(write=True) as db:
        batch = fetch(db, 'batches', batch_id)
        member = authorize_scope(db, batch, user['id'])
        if batch['owner_id'] != user['id'] and (not member or member['role'] not in ('owner','admin')):
            fail('仅报销单创建者或组管理员可以变更状态', 403)
        if not isinstance(status, str) or status not in transitions.get(batch['status'], set()):
            fail('该报销单当前状态不支持此操作', 409)
        db.execute('UPDATE batches SET status=?,updated_at=? WHERE id=?', (status, now(), batch_id))
        if status in ('cancelled', 'rejected'):
            db.execute("UPDATE invoices SET status='available',batch_id=NULL WHERE batch_id=?", (batch_id,))
        else:
            db.execute('UPDATE invoices SET status=? WHERE batch_id=?', (status, batch_id))
        return batch_dict(db, fetch(db, 'batches', batch_id))


def workbook_bytes(invoices, projects=None):
    projects = projects or {}
    book = Workbook()
    sheet = book.active
    sheet.title = '发票明细'
    headers = ['发票号码', '销售方', '购买方', '开票日期', '费用类别', '价税合计（元）', '所属项目', '状态', '已核对', '原件文件名', '备注']
    sheet.append(headers)
    for inv in invoices:
        values = [inv['number'], inv['seller'], inv['buyer'], inv['date'], inv['category'], inv['amount_cents'] / 100, projects.get(inv.get('project_id'), ''), STATUS_NAMES.get(inv.get('status'), ''), '是' if inv['verified'] else '否', inv.get('file_name', ''), inv.get('note', '')]
        sheet.append(values)
        # Strings are always literal text, including invoice fields beginning with '='.
        for col, value in enumerate(values, 1):
            cell = sheet.cell(sheet.max_row, col)
            if isinstance(value, str):
                cell.data_type = 's'
        sheet.cell(sheet.max_row, 6).number_format = '#,##0.00'
    sheet.freeze_panes = 'A2'
    sheet.auto_filter.ref = sheet.dimensions
    for cell in sheet[1]:
        cell.font = Font(color='FFFFFF', bold=True)
        cell.fill = PatternFill('solid', fgColor='365D84')
    for key, width in zip('ABCDEFGHIJK', [25, 32, 32, 15, 13, 20, 24, 15, 12, 35, 40]):
        sheet.column_dimensions[key].width = width
    buffer = io.BytesIO()
    book.save(buffer)
    return buffer.getvalue()


def download(data, name, media_type):
    return Response(data, media_type=media_type, headers={'Content-Disposition': f"attachment; filename*=UTF-8''{quote(name)}"})


@app.get('/api/export')
def export_library(group_id: str | None = None, user: dict = Depends(require_user)):
    with connection() as db:
        invoices = visible_invoices(db, user['id'], group_id)
        projects = {r['id']: r['name'] for r in scoped_rows(db, 'projects', user['id'], group_id)}
    return download(workbook_bytes(invoices, projects), f'发票明细_{date.today()}.xlsx', 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')


@app.get('/api/batches/{batch_id}/export')
def export_batch(batch_id: str, user: dict = Depends(require_user)):
    return build_batch_export(batch_id, user)


@app.post('/api/batches/{batch_id}/export')
def export_batch_options(batch_id: str, values: dict, user: dict = Depends(require_user)):
    rename = values.get('rename', False)
    originals_only = values.get('originals_only', False)
    filenames = values.get('filenames', {})
    if type(rename) is not bool or type(originals_only) is not bool or not isinstance(filenames, dict) or any(not isinstance(value, str) for value in filenames.values()):
        fail('下载选项格式不正确')
    return build_batch_export(batch_id, user, originals_only, rename, filenames if rename else {})


def build_batch_export(batch_id, user, originals_only=False, rename=False, filenames=None):
    filenames = filenames or {}
    with LOCK, connection() as db:
        row = fetch(db, 'batches', batch_id)
        authorize_scope(db, row, user['id'])
        batch = batch_dict(db, row)
        if any(key not in batch['invoice_ids'] for key in filenames):
            fail('文件名包含不属于此报销单的发票')
        projects = {r['id']: r['name'] for r in scoped_rows(db, 'projects', user['id'], row.get('group_id'))}
        temporary = tempfile.TemporaryDirectory(prefix='invoice-export-')
        zip_path = Path(temporary.name) / 'export.zip'
        try:
            with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as archive:
                if not originals_only:
                    export_items = [{**i, 'status': batch['status']} for i in batch['items']]
                    archive.writestr('发票明细.xlsx', workbook_bytes(export_items, projects))
                    archive.writestr('报销单.json', json.dumps(batch, ensure_ascii=False, indent=2))
                missing, used_names = [], set()
                for n, snapshot in enumerate(batch['items'], 1):
                    row = fetch(db, 'invoices', snapshot['id'])
                    if row['file_path'] and (DATA / row['file_path']).is_file():
                        name = original_filename(row, snapshot, n, rename, filenames.get(snapshot['id']))
                        candidate, duplicate = name, 1
                        while candidate.casefold() in used_names:
                            duplicate += 1
                            candidate = f'{Path(name).stem}-{duplicate}{Path(name).suffix}'
                        used_names.add(candidate.casefold())
                        archive.write(DATA / row['file_path'], '原件/' + candidate)
                    else:
                        missing.append(f"{n:03}_" + (snapshot['seller'] or snapshot['number'] or snapshot['id']))
                if missing:
                    archive.writestr('无原件记录.txt', '以下记录为手工录入或无原件：\n' + '\n'.join(missing))
        except BaseException:
            temporary.cleanup()
            raise
    name = safe_filename(batch['name'], '报销单') + ('_原件' if originals_only else '')
    return FileResponse(zip_path, filename=name + '.zip', media_type='application/zip', background=BackgroundTask(temporary.cleanup))


@app.get('/api/backup')
def backup(user: dict = Depends(require_user)):
    # A user may export only their own original files and personal records, never the server database.
    temporary = tempfile.TemporaryDirectory(prefix='invoice-backup-')
    zip_path = Path(temporary.name) / 'backup.zip'
    with LOCK, connection() as db:
        invoices = visible_invoices(db, user['id'])
        payload = dict(format='invoice-desk-personal-v2', exported_at=now(), user=user, invoices=invoices,
            projects=[dict(r) for r in scoped_rows(db, 'projects', user['id'])],
            quotas=[quota_dict(db,r) for r in scoped_rows(db, 'quotas', user['id'])],
            batches=[batch_dict(db,r) for r in scoped_rows(db, 'batches', user['id'])])
        with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as archive:
            archive.writestr('个人数据.json', json.dumps(payload, ensure_ascii=False, indent=2))
            archive.writestr('发票明细.xlsx', workbook_bytes(invoices, {r['id']:r['name'] for r in payload['projects']}))
            for invoice in invoices:
                row = fetch(db, 'invoices', invoice['id'])
                if row['file_path'] and (DATA / row['file_path']).is_file():
                    archive.write(DATA / row['file_path'], f"原件/{invoice['id']}_{Path(row['file_name']).name}")
            archive.writestr('说明.txt', '这是当前账号的个人数据导出，含个人票据原件和个人报销记录，不含其他账号、组报销单、会话或邮箱密码。发票中的组ID用于记录分享关系。服务器全量备份由部署管理员停止服务后复制完整数据目录（含secret.key）完成。当前不支持通过页面自动恢复JSON。')
    return FileResponse(zip_path, filename=f'个人票栈_{date.today()}.zip', media_type='application/zip', background=BackgroundTask(temporary.cleanup))


app.mount('/', StaticFiles(directory=ROOT / 'static', html=True), name='frontend')
