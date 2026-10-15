"""Multi-account boundaries and a single reservation across overlapping invoice pools."""
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from fastapi.testclient import TestClient
from app import db, main


class ScopedApiTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        data = Path(temporary.name)
        for module in (db, main):
            p = patch.object(module, 'DATA', data)
            p.start()
            self.addCleanup(p.stop)
        p = patch.dict(os.environ, {'INVOICE_MAIL_WORKER':'0', 'ALLOW_REGISTRATION':'true', 'COOKIE_SECURE':'false'})
        p.start()
        self.addCleanup(p.stop)
        self.a = TestClient(main.app, headers={'X-Requested-With':'InvoiceDesk'})
        self.a.__enter__()
        self.addCleanup(self.a.__exit__, None, None, None)
        self.b = TestClient(main.app, headers={'X-Requested-With':'InvoiceDesk'})
        self.c = TestClient(main.app, headers={'X-Requested-With':'InvoiceDesk'})
        self.addCleanup(self.b.close)
        self.addCleanup(self.c.close)
        for n, client in enumerate((self.a, self.b, self.c)):
            self.ok(client.post('/api/auth/register', json={'email':f'user{n}@example.test','name':f'用户{n}','password':'test-password-123'}))
        self.group = self.ok(self.a.post('/api/groups', json={'name':'出差组'}))
        self.group2 = self.ok(self.a.post('/api/groups', json={'name':'项目组'}))
        token = self.ok(self.a.post(f"/api/groups/{self.group['id']}/invites"))['token']
        self.ok(self.b.post('/api/groups/join', json={'token':token}))

    def ok(self, response):
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def upload(self):
        raw = b'<Invoice><InvoiceNumber>SHARED001</InvoiceNumber><SellerName>Vendor</SellerName><InvoiceDate>2026-10-15</InvoiceDate><TotalTaxIncludedAmount>12.34</TotalTaxIncludedAmount></Invoice>'
        item = self.ok(self.a.post('/api/import', files={'files':('private.xml',raw,'application/xml')}))['imported'][0]
        self.ok(self.a.patch('/api/invoices/'+item['id'],json={'verified':True}))
        return item

    def test_selective_sharing_access_and_global_reservation(self):
        invoice = self.upload()
        gid, other_gid = self.group['id'], self.group2['id']
        self.assertEqual(self.b.get(invoice['file_url']).status_code,403)
        self.assertEqual(self.ok(self.b.get('/api/state'))['invoices'],[])
        self.ok(self.a.put('/api/invoices/'+invoice['id']+'/shares',json={'group_ids':[gid,other_gid]}))
        shared = self.ok(self.b.get('/api/state',params={'group_id':gid}))['invoices'][0]
        self.assertFalse(shared['can_edit'])
        self.assertEqual(shared['shared_group_ids'],[])
        self.assertEqual(self.b.get(invoice['file_url']).status_code,200)
        self.assertEqual(self.b.patch('/api/invoices/'+invoice['id'],json={'seller':'forbidden'}).status_code,403)
        batch = self.ok(self.b.post('/api/batches',json={'name':'组报销','group_id':gid,'invoice_ids':[invoice['id']]}))
        self.assertEqual(self.ok(self.a.post('/api/match',json={'target_cents':2000,'group_id':other_gid}))['candidate_count'],0)
        self.assertEqual(self.a.post('/api/batches',json={'name':'重复使用','invoice_ids':[invoice['id']]}).status_code,409)
        self.assertEqual(self.a.put('/api/invoices/'+invoice['id']+'/shares',json={'group_ids':[other_gid]}).status_code,409)
        self.assertEqual(self.c.get('/api/state',params={'group_id':gid}).status_code,403)
        self.assertEqual(self.c.get('/api/batches/'+batch['id']+'/export').status_code,403)
        batch_file = batch['items'][0]['file_url']
        self.assertEqual(self.c.get(batch_file, params={'download': True}).status_code,403)
        self.assertEqual(self.c.get(batch_file, params={'preview_page': 1}).status_code,403)
        self.assertEqual(self.c.post('/api/batches/'+batch['id']+'/export', json={'originals_only': True}).status_code,403)
        other = self.ok(self.a.post('/api/invoices', json={'seller':'Other','date':'2026-10-15','amount_cents':100,'verified':True}))
        self.assertEqual(self.b.get('/api/batches/'+batch['id']+'/invoices/'+other['id']+'/file').status_code,404)
        self.ok(self.b.patch('/api/batches/'+batch['id'],json={'status':'cancelled'}))
        self.ok(self.a.put('/api/invoices/'+invoice['id']+'/shares',json={'group_ids':[other_gid]}))
        self.assertEqual(self.ok(self.b.get('/api/state',params={'group_id':gid}))['invoices'],[])
        self.assertEqual(self.b.get(invoice['file_url']).status_code,403)
        self.assertEqual(self.b.get(batch_file).status_code,200)
        exported = self.b.post('/api/batches/'+batch['id']+'/export', json={'originals_only': True, 'rename': True})
        self.assertEqual(exported.status_code,200)
        with zipfile.ZipFile(io.BytesIO(exported.content)) as archive:
            self.assertEqual(archive.namelist(), ['原件/001-其他-12.34.xml'])
        self.assertEqual(self.ok(self.a.post('/api/match',json={'target_cents':1234,'group_id':other_gid}))['combinations'][0]['total_cents'],1234)

    def test_personal_export_and_scoped_quotas_do_not_leak(self):
        invoice = self.upload()
        gid = self.group['id']
        quota = {'month':'2026-10','category':'餐饮','amount_cents':10000}
        self.ok(self.a.post('/api/quotas',json=quota))
        self.ok(self.b.post('/api/quotas',json=quota))
        self.ok(self.a.post('/api/quotas',json={**quota,'group_id':gid}))
        self.assertEqual(self.b.post('/api/quotas',json={**quota,'category':'交通','group_id':gid}).status_code,403)
        self.ok(self.a.put('/api/invoices/'+invoice['id']+'/shares',json={'group_ids':[gid]}))
        backup=self.b.get('/api/backup')
        self.assertEqual(backup.status_code,200)
        with zipfile.ZipFile(io.BytesIO(backup.content)) as archive:
            content=json.loads(archive.read('个人数据.json'))
            self.assertEqual(content['invoices'],[])
            self.assertFalse(any(n.startswith('原件/') for n in archive.namelist()))
        self.ok(self.b.post('/api/auth/logout'))
        self.assertEqual(self.b.get('/api/state').status_code,401)
        self.assertEqual(self.b.get(invoice['file_url']).status_code,401)


if __name__ == '__main__':
    unittest.main()
