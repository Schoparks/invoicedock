"""Signed domain receipts exercise the real app, parser and tenant boundaries."""
from email.message import EmailMessage
import hashlib
import hmac
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from fastapi import HTTPException
from fastapi.testclient import TestClient
from app import db, main, inbound


class InboundTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.data = Path(temporary.name)
        for module in (db, main):
            p = patch.object(module, 'DATA', self.data)
            p.start(); self.addCleanup(p.stop)
        p = patch.dict(os.environ, {'INVOICE_RECEIPT_DOMAIN':'example.test',
            'INVOICE_INBOUND_SECRET':'test-only-inbound-key-at-least-32-characters',
            'INVOICE_INBOUND_WORKER':'0', 'INVOICE_MAIL_WORKER':'0',
            'COOKIE_SECURE':'false', 'ALLOW_REGISTRATION':'true'})
        p.start(); self.addCleanup(p.stop)
        self.client = TestClient(main.app, headers={'X-Requested-With':'InvoiceDesk'})
        self.client.__enter__(); self.addCleanup(self.client.__exit__, None, None, None)
        self.other = TestClient(main.app, headers={'X-Requested-With':'InvoiceDesk'})
        self.addCleanup(self.other.close)
        for n, client in enumerate((self.client, self.other)):
            self.ok(client.post('/api/auth/register', json={'email':f'inbound{n}@example.test',
                'name':f'收票用户{n}', 'password':'only-test-password-123'}))
        self.service = TestClient(main.app)
        self.addCleanup(self.service.close)

    def ok(self, response, status=200):
        self.assertEqual(response.status_code, status, response.text)
        return response.json()

    def address(self, prefix='receipts', **scope):
        return self.ok(self.client.post('/api/receipt-addresses', json={
            'prefix':prefix, 'name':'测试收票地址', **scope}))

    def mail(self, *attachments):
        mail = EmailMessage()
        mail['From'] = 'fixture@example.test'
        mail['To'] = 'forged-destination@example.test'
        mail['Subject'] = '联调样例（非真实发票）'
        mail.set_content('附件用于验证发票自动识别。')
        for filename, data in attachments:
            mail.add_attachment(data, maintype='application', subtype='octet-stream', filename=filename)
        return mail.as_bytes()

    def signature(self, raw, recipient, stamp=None):
        stamp = str(stamp or int(time.time()))
        value = stamp+'\n'+recipient+'\n'+hashlib.sha256(raw).hexdigest()
        signature = hmac.new(os.environ['INVOICE_INBOUND_SECRET'].encode(), value.encode(), hashlib.sha256).hexdigest()
        return {'X-Invoice-Timestamp':stamp, 'X-Invoice-Recipient':recipient, 'X-Invoice-Signature':signature}

    def send(self, raw, recipient):
        return self.ok(self.service.post('/api/inbound/email', content=raw,
            headers=self.signature(raw, recipient)), 202)

    def xml(self, number):
        return f'<Invoice><InvoiceNumber>{number}</InvoiceNumber><SellerName>测试餐厅</SellerName><BuyerName>测试买方</BuyerName><InvoiceDate>2026-10-15</InvoiceDate><TotalTaxIncludedAmount>128.50</TotalTaxIncludedAmount></Invoice>'.encode()

    def test_envelope_routing_real_import_dedup_and_private_scope(self):
        address = self.address()
        raw = self.mail(('测试发票.xml', self.xml('INBOUND0001')))
        event = self.send(raw, address['address'])
        self.assertEqual(event['status'], 'queued')
        self.assertEqual(inbound.process_pending(), 1)
        invoices = self.ok(self.client.get('/api/state'))['invoices']
        self.assertEqual(len(invoices), 1)
        item = invoices[0]
        self.assertEqual((item['number'],item['amount_cents'],item['date']), ('INBOUND0001',12850,'2026-10-15'))
        self.assertFalse(item['verified'])
        self.assertEqual(self.client.get(item['file_url']).content, self.xml('INBOUND0001'))
        self.assertEqual(self.ok(self.other.get('/api/state'))['invoices'], [])
        self.assertTrue(self.send(raw,address['address'])['duplicate'])
        self.assertEqual(inbound.process_pending(), 0)
        records = self.ok(self.client.get(f"/api/receipt-addresses/{address['id']}/deliveries"))['deliveries']
        self.assertEqual((records[0]['status'],records[0]['results'][0]['status']), ('completed','imported'))
        self.assertFalse((self.data/'inbound'/f"{event['id']}.eml").exists())

    def test_same_invoice_formats_keep_only_pdf_regardless_of_attachment_order(self):
        import pymupdf
        import zipfile
        from io import BytesIO
        address = self.address()
        for index, pdf_first in enumerate((True, False)):
            with self.subTest(pdf_first=pdf_first):
                number = '2661200000000000001' + str(index)
                xml = self.xml(number)
                with pymupdf.open() as document:
                    page = document.new_page()
                    page.insert_text((40,40), f'Invoice Number: {number}\nIssue Date: 2026-10-15\nGrand Total: 128.50')
                    pdf = document.tobytes()
                files = [('invoice.pdf', pdf), ('invoice.xml', xml)]
                if not pdf_first:
                    files.reverse()
                event = self.send(self.mail(*files), address['address'])
                inbound.process_pending()
                items = self.ok(self.client.get('/api/state'))['invoices']
                self.assertEqual(len(items), 1)
                item = items[0]
                self.assertEqual((item['number'], item['seller'], item['amount_cents']), (number, '测试餐厅', 12850))
                records = self.ok(self.client.get('/api/receipt-addresses/'+address['id']+'/deliveries'))['deliveries']
                record = next(r for r in records if r['id'] == event['id'])
                self.assertEqual([r['status'] for r in record['results']], ['imported', 'duplicate'])
                self.assertEqual({r['invoice_id'] for r in record['results']}, {item['id']})
                self.assertEqual(self.client.get(item['file_url']).content, pdf)
                self.assertEqual(list((self.data/'originals').iterdir()), [self.data/'originals'/f"{item['id']}.pdf"])
                self.assertEqual(main.persist_import(xml, 'resend.xml', item['owner_id'])['status'], 'duplicate')
                with patch('app.main.parse_invoice', side_effect=AssertionError('already stored')):
                    result = main.persist_import(pdf, 'renamed.pdf', item['owner_id'])
                self.assertEqual((result['status'], result['invoice']['id']), ('duplicate', item['id']))
                backup = self.client.get('/api/backup')
                self.assertEqual(backup.status_code, 200)
                with zipfile.ZipFile(BytesIO(backup.content)) as archive:
                    self.assertEqual([archive.read(n) for n in archive.namelist() if n.startswith('原件/')], [pdf])
                self.ok(self.client.delete('/api/invoices/'+item['id']))
                self.assertEqual(list((self.data/'originals').iterdir()), [])

    def test_format_merge_fills_blanks_but_preserves_verified_fields_and_owner_scope(self):
        owner = self.ok(self.client.get('/api/state'))['user']['id']
        other_owner = self.ok(self.other.get('/api/state'))['user']['id']
        number = '26612000000000000002'
        partial = f'<Invoice><InvoiceNumber>{number}</InvoiceNumber></Invoice>'.encode()
        original = main.persist_import(partial, 'partial.xml', owner)['invoice']
        complete = self.xml(number)
        merged = main.persist_import(complete, 'complete.xml', owner)
        self.assertEqual(merged['status'], 'duplicate')
        self.assertEqual((merged['invoice']['seller'], merged['invoice']['date'], merged['invoice']['amount_cents']), ('测试餐厅', '2026-10-15', 12850))
        self.assertFalse(merged['invoice']['verified'])
        self.ok(self.client.patch('/api/invoices/'+original['id'], json={'seller':'用户已核对商家', 'verified':True}))
        third = complete.replace(b'</Invoice>', b'<Extra>third</Extra></Invoice>')
        self.assertEqual(main.persist_import(third, 'third.xml', owner)['invoice']['seller'], '用户已核对商家')
        independent = main.persist_import(third, 'other.xml', other_owner)
        self.assertEqual(independent['status'], 'imported')
        self.assertNotEqual(independent['invoice']['id'], original['id'])
        distinct = main.persist_import(self.xml('26612000000000000003'), 'distinct.xml', owner)
        self.assertEqual(distinct['status'], 'imported')

    def test_signature_and_unknown_address_rejection(self):
        address = self.address()
        raw = self.mail(('test.xml',self.xml('INBOUND0002')))
        signed = self.signature(raw,address['address'])
        for body, headers in ((raw+b'tamper',signed), (raw,{**signed,'X-Invoice-Recipient':'other@example.test'}),
            (raw,self.signature(raw,address['address'],int(time.time())-600))):
            self.assertEqual(self.service.post('/api/inbound/email',content=body,headers=headers).status_code,401)
        self.assertEqual(self.service.post('/api/inbound/email',content=raw,
            headers=self.signature(raw,'missing@example.test')).status_code,404)
        self.assertEqual(self.service.get('/api/inbound/email').status_code,401)
        self.assertEqual(self.service.post('/api/inbound/email',content=raw).status_code,401)
        with db.connection() as connection:
            self.assertEqual(connection.execute('SELECT COUNT(*) FROM inbound_deliveries').fetchone()[0],0)

    def test_dissolve_pauses_receivers_and_delivers_already_queued_mail_to_owner(self):
        group = self.ok(self.client.post('/api/groups', json={'name':'收票团队'}))
        address = self.address('dissolving-team', group_id=group['id'])
        raw = self.mail(('queued.xml', self.xml('QUEUED-RETURN')))
        self.send(raw, address['address'])
        self.ok(self.client.delete('/api/groups/'+group['id']))
        returned = self.ok(self.client.get('/api/receipt-addresses'))['addresses'][0]
        self.assertIsNone(returned['group_id'])
        self.assertFalse(returned['enabled'])
        self.assertEqual(returned['owner_id'], address['owner_id'])
        self.assertEqual(self.other.get('/api/receipt-addresses').json()['addresses'], [])
        self.assertEqual(inbound.process_pending(), 1)
        invoices = self.ok(self.client.get('/api/state'))['invoices']
        self.assertEqual((invoices[0]['number'],invoices[0]['owner_id'],invoices[0]['shared_group_ids']), ('QUEUED-RETURN',address['owner_id'],[]))
        self.assertEqual(self.client.get(invoices[0]['file_url']).status_code, 200)
        records = self.ok(self.client.get(f"/api/receipt-addresses/{address['id']}/deliveries"))['deliveries']
        self.assertEqual(records[0]['status'], 'completed')
        self.assertEqual(self.service.post('/api/inbound/email', content=raw, headers=self.signature(raw,address['address'])).status_code, 404)

    def test_inflight_mail_import_after_departure_uses_personal_scope(self):
        group = self.ok(self.client.post('/api/groups', json={'name':'转让团队'}))
        owner = self.ok(self.client.get('/api/state'))['user']['id']
        next_owner = self.ok(self.other.get('/api/state'))['user']['id']
        invite = self.ok(self.client.post(f"/api/groups/{group['id']}/invites"))['token']
        self.ok(self.other.post('/api/groups/join', json={'token':invite}))
        self.ok(self.client.post(f"/api/groups/{group['id']}/transfer", json={'member_id':next_owner}))
        parse = main.parse_invoice
        def depart_during_parse(data, filename):
            self.ok(self.client.delete(f"/api/groups/{group['id']}/members/{owner}"))
            return parse(data, filename)
        with patch.object(main, 'parse_invoice', side_effect=depart_during_parse):
            item = main.persist_mail_import(self.xml('INFLIGHT-RETURN'), 'inflight.xml', owner, group['id'])['invoice']
        self.assertEqual((item['owner_id'],item['shared_group_ids']), (owner,[]))
        self.assertEqual(self.ok(self.other.get('/api/state', params={'group_id':group['id']}))['invoices'], [])
        self.assertEqual(self.other.get(item['file_url']).status_code, 403)
        with self.assertRaises(HTTPException) as caught:
            main.persist_import(self.xml('HTTP-REJECT'), 'manual.xml', owner, group['id'])
        self.assertEqual(caught.exception.status_code, 403)

    def test_group_permission_and_stopped_address_keeps_ownership(self):
        group = self.ok(self.client.post('/api/groups',json={'name':'收票组'}))
        token = self.ok(self.client.post(f"/api/groups/{group['id']}/invites"))['token']
        self.ok(self.other.post('/api/groups/join',json={'token':token}))
        address = self.address('team',group_id=group['id'])
        visible = self.ok(self.other.get('/api/receipt-addresses'))['addresses'][0]
        self.assertFalse(visible['can_manage'])
        self.assertEqual(self.other.patch('/api/receipt-addresses/'+address['id'],json={'enabled':False}).status_code,404)
        self.assertEqual(self.other.get('/api/receipt-addresses/'+address['id']+'/deliveries').status_code,404)
        raw = self.mail(('group.xml',self.xml('INBOUND0003')))
        self.send(raw,address['address']); inbound.process_pending()
        self.assertEqual(len(self.ok(self.other.get('/api/state',params={'group_id':group['id']}))['invoices']),1)
        self.ok(self.client.patch('/api/receipt-addresses/'+address['id'],json={'enabled':False}))
        self.assertEqual(self.service.post('/api/inbound/email',content=raw,
            headers=self.signature(raw,address['address'])).status_code,404)
        self.assertEqual(self.other.post('/api/receipt-addresses',json={'prefix':'team','name':'抢占'}).status_code,409)

    def test_failed_attachment_retry_and_empty_mail_record(self):
        address = self.address()
        raw = self.mail(('good.xml',self.xml('INBOUND0004')),('retry.xml',self.xml('INBOUND0005')))
        def importer(data, filename, owner_id, group_id):
            if filename == 'retry.xml': raise ValueError('测试暂时失败')
            return main.persist_import(data,filename,owner_id,group_id)
        inbound.configure_importer(importer)
        event = self.send(raw,address['address']); inbound.process_pending()
        self.assertEqual(len(self.ok(self.client.get('/api/state'))['invoices']),1)
        inbound.configure_importer(main.persist_import)
        self.ok(self.client.post('/api/receipt-addresses/'+address['id']+'/retry',json={'delivery_id':event['id']}))
        inbound.process_pending()
        self.assertEqual(len(self.ok(self.client.get('/api/state'))['invoices']),2)
        empty = self.send(self.mail(),address['address']); inbound.process_pending()
        records = self.ok(self.client.get('/api/receipt-addresses/'+address['id']+'/deliveries'))['deliveries']
        ignored = next(row for row in records if row['id']==empty['id'])
        self.assertEqual((ignored['status'],ignored['results'][0]['status']),('completed','ignored'))

    def test_pdf_text_and_scanned_pdf_are_automatically_recognized(self):
        import pymupdf
        address = self.address()
        files = []
        for number, scanned in (('26000000000000000011',False),('26000000000000000012',True)):
            doc = pymupdf.open(); page = doc.new_page(width=595,height=350)
            lines = ['联调样例（非真实发票）','发票号码：'+number,'开票日期：2026年10月15日',
                '销售方名称：测试餐厅','购买方名称：测试买方','价税合计（小写）：￥128.50']
            for index, line in enumerate(lines):
                page.insert_text((40,45+index*40),line,fontname='china-s',fontsize=16)
            if scanned:
                image=page.get_pixmap(dpi=200).tobytes('png')
                doc.close(); doc=pymupdf.open(); page=doc.new_page(width=595,height=350)
                page.insert_image(page.rect,stream=image)
            files.append((('scan' if scanned else 'text')+'.pdf',doc.tobytes())); doc.close()
        self.send(self.mail(*files),address['address']); inbound.process_pending()
        items=self.ok(self.client.get('/api/state'))['invoices']
        self.assertEqual(len(items),2)
        self.assertEqual({i['number'] for i in items},{'26000000000000000011','26000000000000000012'})
        for item in items:
            self.assertEqual((item['date'],item['amount_cents'],item['verified']),('2026-10-15',12850,False))
        self.assertTrue(any('OCR' in i['source'] for i in items))


if __name__ == '__main__': unittest.main()
