"""Reparse and merge one historical delivery only when every original agrees.

Defaults to a read-only plan. --apply backs up SQLite before merging unused,
unverified records; only the PDF original is retained and inbound result links are reconciled.
Run with the existing project environment and INVOICE_DATA_DIR set.
"""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import shutil
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.db import DATA, initialize
from app.parsing import parse_invoice, REVIEW_NOTE


def repair(delivery_id, apply=False):
    database = DATA / 'invoices.sqlite3'
    with sqlite3.connect(f'file:{database}?mode=ro', uri=True) as db:
        db.row_factory = sqlite3.Row
        rows = [dict(row) for row in db.execute('''SELECT DISTINCT i.* FROM invoices i
            JOIN inbound_results r ON r.invoice_id=i.id WHERE r.delivery_id=?''', (delivery_id,))]
        history = {r['id']: db.execute('SELECT COUNT(*) FROM batch_items WHERE invoice_id=?', (r['id'],)).fetchone()[0] for r in rows}
    if len(rows) < 2:
        raise ValueError('该投递没有多条现存发票记录，无需合并')
    if len({r['owner_id'] for r in rows}) != 1 or not rows[0]['owner_id']:
        raise ValueError('附件所有人不一致，停止合并')
    if any(r['verified'] or r['status'] != 'available' or history[r['id']] or r['batch_id'] or r['note'] or r['project_id'] for r in rows):
        raise ValueError('记录已核对、使用或含用户备注/项目，停止自动修复')
    keys = ('number', 'date', 'amount_cents', 'seller', 'buyer')
    parsed = []
    for row in rows:
        data = (DATA / row['file_path']).read_bytes()
        if hashlib.sha256(data).hexdigest() != row['file_hash']:
            raise ValueError('原件哈希不一致，停止修复')
        fields = parse_invoice(data, row['file_name'])
        if not all(fields.get(key) for key in keys):
            raise ValueError('原件关键字段仍不完整，停止合并')
        parsed.append(fields)
    if len({tuple(item[key] for key in keys) for item in parsed}) != 1:
        raise ValueError('原件重新解析的关键字段不一致，停止合并')
    canonical = next((r for r in rows if Path(r['file_name']).suffix.lower() == '.pdf'), None)
    if canonical is None:
        raise ValueError('该投递没有 PDF 原件，停止合并')
    retired = [r for r in rows if r['id'] != canonical['id']]
    fields = parsed[rows.index(canonical)]
    plan = {'delivery_id': delivery_id, 'keep': canonical['id'], 'merge': [r['id'] for r in retired],
            'fields': {key: fields[key] for key in keys}, 'kept_format': 'PDF', 'discarded_formats': [Path(r['file_name']).suffix for r in retired], 'applied': False}
    if not apply:
        return plan
    backup_dir = DATA / 'repair-backups'
    backup_dir.mkdir(mode=0o700, exist_ok=True)
    backup = backup_dir / ('before-format-merge-' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ') + '.sqlite3')
    # Create private backup before opening SQLite's writer.
    descriptor = os.open(backup, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    os.close(descriptor)
    with sqlite3.connect(database) as source, sqlite3.connect(backup) as target:
        source.backup(target)
    originals_backup = backup.with_suffix('.originals')
    originals_backup.mkdir(mode=0o700)
    for row in retired:
        shutil.copy2(DATA / row['file_path'], originals_backup / Path(row['file_path']).name)
    initialize()
    with sqlite3.connect(database) as db:
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA foreign_keys=ON')
        db.execute('BEGIN IMMEDIATE')
        for row in rows:
            current = db.execute('SELECT * FROM invoices WHERE id=?', (row['id'],)).fetchone()
            if current is None or dict(current) != row or db.execute('SELECT 1 FROM batch_items WHERE invoice_id=?', (row['id'],)).fetchone():
                raise ValueError('记录在检查后发生变化，已停止合并')
        for row in retired:
            db.execute('''INSERT OR IGNORE INTO invoice_shares SELECT ?,group_id,shared_by,created_at
                FROM invoice_shares WHERE invoice_id=?''', (canonical['id'], row['id']))
            db.execute("UPDATE inbound_results SET invoice_id=?,status='duplicate',error='' WHERE invoice_id=?", (canonical['id'], row['id']))
            db.execute('DELETE FROM invoice_shares WHERE invoice_id=?', (row['id'],))
            db.execute('DELETE FROM invoices WHERE id=?', (row['id'],))
        updates = {key: fields[key] for key in (*keys, 'category', 'raw_text', 'source')}
        updates['review_note'] = REVIEW_NOTE + ' 同张发票的多格式附件已合并，仅保留 PDF 原件。'
        db.execute(f"UPDATE invoices SET {','.join(key+'=?' for key in updates)} WHERE id=?", [*updates.values(), canonical['id']])
        if db.execute('PRAGMA foreign_key_check').fetchall():
            raise ValueError('发现关联异常，修复事务已回滚')
    for row in retired:
        (DATA / row['file_path']).unlink()
    plan.update(applied=True, backup=str(backup))
    return plan


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('delivery_id')
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    print(json.dumps(repair(args.delivery_id, args.apply), ensure_ascii=False, indent=2))
