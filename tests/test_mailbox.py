"""Mock IMAP tests: no external mailbox, network, or actual messages are used."""
from email.message import EmailMessage
import os
from pathlib import Path
import socket
import tempfile
import unittest
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import db, mailbox
from app.auth import require_user


def message(*filenames):
    mail = EmailMessage()
    mail['From'] = 'sender@example.com'
    mail['To'] = 'receipts@example.com'
    mail['Subject'] = '发票'
    mail.set_content('Please see attached invoices.')
    for filename in filenames:
        mail.add_attachment(b'<Invoice><InvoiceNumber>12345678</InvoiceNumber></Invoice>',
                            maintype='application', subtype='xml', filename=filename)
    return mail.as_bytes()


class FakeInbox:
    def __init__(self):
        self.messages = {}
        self.validity = '7'
        self.sizes = {}
        self.calls = []

    @property
    def next_uid(self):
        return max(self.messages, default=0) + 1


class FakeIMAP:
    def __init__(self, inbox):
        self.inbox = inbox

    def login(self, username, password):
        return 'OK', []

    def select(self, folder, readonly=False):
        self.inbox.calls.append(('select', folder, readonly))
        return 'OK', [str(len(self.inbox.messages)).encode()]

    def status(self, folder, fields):
        return 'OK', [f'"INBOX" (UIDVALIDITY {self.inbox.validity} UIDNEXT {self.inbox.next_uid})'.encode()]

    def uid(self, command, *args):
        self.inbox.calls.append((command, *args))
        if command == 'SEARCH':
            return 'OK', [b' '.join(str(uid).encode() for uid in sorted(self.inbox.messages))]
        uid = int(args[0])
        if args[1] == '(RFC822.SIZE)':
            size = self.inbox.sizes.get(uid, len(self.inbox.messages[uid]))
            return 'OK', [f'{uid} (UID {uid} RFC822.SIZE {size})'.encode()]
        if args[1] == '(BODY.PEEK[])':
            return 'OK', [(f'{uid} (BODY[]'.encode(), self.inbox.messages[uid]), b')']
        raise AssertionError('Unexpected IMAP command')

    def logout(self):
        return 'BYE', []


def dns(address):
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, '', (address, 993))]


class MailboxTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.data = Path(temporary.name)
        self.patch(patch.object(db, 'DATA', self.data))
        self.patch(patch.dict(os.environ, {'INVOICE_SECRET_KEY': '', 'INVOICE_MAIL_WORKER': '0'}))
        db.initialize()
        mailbox.initialize_mailboxes()
        with db.connection(write=True) as connection:
            for number in range(1, 5):
                connection.execute('INSERT INTO users VALUES (?,?,?,?,?)', (f'u{number}', f'user{number}@example.com', f'User {number}', 'unused', '2026-10-15'))
            connection.execute('INSERT INTO groups VALUES (?,?,?,?)', ('g1', 'Team', 'u1', '2026-10-15'))
            for user_id, role in (('u1', 'owner'), ('u2', 'admin'), ('u3', 'member')):
                connection.execute('INSERT INTO group_members VALUES (?,?,?,?)', ('g1', user_id, role, '2026-10-15'))
        self.inbox = FakeInbox()
        self.network = self.patch(patch.object(mailbox, '_PinnedIMAP4SSL', side_effect=lambda *args: FakeIMAP(self.inbox)))
        self.patch(patch.object(mailbox.socket, 'getaddrinfo', return_value=dns('8.8.8.8')))
        self.user = {'id': 'u1', 'email': 'user1@example.com', 'name': 'User 1'}
        app = FastAPI()
        app.include_router(mailbox.router)
        app.dependency_overrides[require_user] = lambda: self.user
        self.client = TestClient(app)
        self.imports = []
        mailbox.configure_importer(self.import_invoice)
        self.addCleanup(mailbox.configure_importer, None)

    def patch(self, patcher):
        result = patcher.start()
        self.addCleanup(patcher.stop)
        return result

    def import_invoice(self, data, filename, owner_id, group_id):
        self.imports.append((filename, owner_id, group_id))
        return {'status': 'imported', 'invoice': {'id': filename}}

    def bind(self, **fields):
        response = self.client.post('/api/mailboxes', json={
            'name': '收票邮箱', 'address': 'receipts@example.com', 'host': 'imap.example.com',
            'username': 'receipts@example.com', 'password': 'mail-app-secret', **fields,
        })
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def sync(self, inbox, retry=False):
        response = self.client.post(f"/api/mailboxes/{inbox['id']}/{'retry' if retry else 'sync'}")
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def test_binding_skips_history_and_keeps_secrets_and_unread_state(self):
        self.inbox.messages[4] = message('old.xml')
        inbox = self.bind()
        self.assertTrue(inbox['has_password'])
        self.assertNotIn('password_encrypted', inbox)
        self.assertNotIn('mail-app-secret', str(inbox))
        with db.connection() as connection:
            row = connection.execute('SELECT * FROM mailboxes WHERE id=?', (inbox['id'],)).fetchone()
            self.assertEqual(row['last_uid'], 4)
            self.assertNotIn('mail-app-secret', row['password_encrypted'])
            encrypted = row['password_encrypted']
        self.assertEqual((self.data / 'secret.key').stat().st_mode & 0o777, 0o600)
        response = self.client.patch('/api/mailboxes/' + inbox['id'], json={'name': '修改名称', 'password': ''})
        self.assertEqual(response.status_code, 200)
        with db.connection() as connection:
            self.assertEqual(connection.execute('SELECT password_encrypted FROM mailboxes').fetchone()[0], encrypted)
        self.inbox.messages[5] = message('new.xml')
        self.assertEqual(self.sync(inbox)['imported'], 1)
        self.assertEqual(self.sync(inbox)['imported'], 0)
        self.assertEqual(self.imports, [('new.xml', 'u1', None)])
        self.assertTrue(all(call[2] for call in self.inbox.calls if call[0] == 'select'))
        self.assertTrue(all(call[2] in ('(RFC822.SIZE)', '(BODY.PEEK[])') for call in self.inbox.calls if call[0] == 'FETCH'))
        self.user['id'] = 'u2'
        self.assertEqual(self.client.get('/api/mailboxes').json()['mailboxes'], [])
        self.assertEqual(self.client.post('/api/mailboxes/' + inbox['id'] + '/sync').status_code, 404)
        (self.data / 'secret.key').unlink()
        with self.assertRaisesRegex(RuntimeError, '密钥缺失'):
            mailbox.initialize_mailboxes()

    def test_failed_attachment_does_not_block_later_mail_and_retry_is_idempotent(self):
        inbox = self.bind()
        self.inbox.messages[1] = message('good.xml', 'broken.xml')
        self.inbox.messages[2] = message('later.xml')
        fixed = False
        def importer(data, filename, owner_id, group_id):
            if filename == 'broken.xml' and not fixed:
                raise ValueError('测试解析失败')
            return self.import_invoice(data, filename, owner_id, group_id)
        mailbox.configure_importer(importer)
        result = self.sync(inbox)
        self.assertEqual((result['imported'], result['error_count']), (2, 1))
        with db.connection() as connection:
            self.assertEqual(connection.execute('SELECT last_uid FROM mailboxes').fetchone()[0], 2)
        self.assertEqual(self.sync(inbox)['imported'], 0)
        fixed = True
        self.assertEqual(self.sync(inbox, retry=True)['imported'], 1)
        self.assertEqual(self.imports, [('good.xml', 'u1', None), ('later.xml', 'u1', None), ('broken.xml', 'u1', None)])
        self.assertEqual(self.sync(inbox, retry=True)['imported'], 0)
        results = self.client.get('/api/mailboxes/' + inbox['id'] + '/results').json()['results']
        self.assertEqual({row['status'] for row in results}, {'imported'})

    def test_group_mailboxes_require_admin_and_preserve_original_owner(self):
        inbox = self.bind(group_id='g1')
        self.user['id'] = 'u2'
        self.assertEqual(len(self.client.get('/api/mailboxes').json()['mailboxes']), 1)
        self.inbox.messages[1] = message('team.xml')
        self.assertEqual(self.sync(inbox)['imported'], 1)
        self.assertEqual(self.imports, [('team.xml', 'u1', 'g1')])
        for user_id in ('u3', 'u4'):
            self.user['id'] = user_id
            self.assertEqual(self.client.get('/api/mailboxes').json()['mailboxes'], [])
            self.assertEqual(self.client.delete('/api/mailboxes/' + inbox['id']).status_code, 404)
        self.user['id'] = 'u2'
        self.assertEqual(self.client.delete('/api/mailboxes/' + inbox['id']).status_code, 200)
        with db.connection() as connection:
            self.assertEqual(connection.execute('SELECT COUNT(*) FROM mail_results').fetchone()[0], 0)

    def test_ssrf_rejects_private_and_mixed_dns_before_connecting(self):
        for address in ('127.0.0.1', '10.1.2.3', '169.254.169.254', '192.168.1.1', '::1', '::ffff:127.0.0.1', '224.0.0.1'):
            with self.subTest(address=address), patch.object(mailbox.socket, 'getaddrinfo', return_value=dns(address)):
                with self.assertRaises(mailbox.MailboxError):
                    mailbox._public_addresses('imap.example.com')
        with patch.object(mailbox.socket, 'getaddrinfo', return_value=dns('8.8.8.8') + dns('127.0.0.1')):
            response = self.client.post('/api/mailboxes', json={
                'name': 'unsafe', 'address': 'a@example.com', 'host': 'imap.example.com',
                'username': 'a', 'password': 'secret',
            })
            self.assertEqual(response.status_code, 400)
        self.network.assert_not_called()

    def test_mail_without_supported_attachments_leaves_ignored_receipt(self):
        inbox = self.bind()
        mail = EmailMessage()
        mail['Subject'] = '发票下载通知'
        mail.set_content('请点击正文链接下载发票。')
        self.inbox.messages[1] = mail.as_bytes()
        result = self.sync(inbox)
        self.assertEqual((result['imported'], result['error_count']), (0, 0))
        rows = self.client.get('/api/mailboxes/' + inbox['id'] + '/results').json()['results']
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['status'], 'ignored')
        self.assertIn('未找到受支持的附件', rows[0]['error'])
        self.assertIn('正文链接', rows[0]['error'])
        with db.connection() as connection:
            saved = connection.execute('SELECT last_uid,last_error FROM mailboxes').fetchone()
            self.assertEqual((saved['last_uid'], saved['last_error']), (1, ''))
        self.assertEqual(self.sync(inbox)['error_count'], 0)
        self.assertEqual(self.imports, [])

    def test_uid_reset_keeps_old_failures_without_counting_them_as_retryable(self):
        inbox = self.bind()
        self.inbox.messages[1] = message('too-large.xml')
        self.inbox.sizes[1] = mailbox.MAX_MAIL_BYTES + 1
        self.assertEqual(self.sync(inbox)['error_count'], 1)
        self.inbox.validity = '99'
        self.assertIn('notice', self.sync(inbox))
        self.assertEqual(self.sync(inbox, retry=True)['error_count'], 0)
        with db.connection() as connection:
            self.assertEqual(connection.execute('SELECT last_error FROM mailboxes').fetchone()[0], '')
            row = connection.execute('SELECT uid_validity,status FROM mail_results').fetchone()
            self.assertEqual((row['uid_validity'], row['status']), ('7', 'failed'))
        rows = self.client.get('/api/mailboxes/' + inbox['id'] + '/results').json()['results']
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['status'], 'failed')

    def test_large_email_advances_cursor_without_body_and_uid_reset_skips_history(self):
        inbox = self.bind()
        self.inbox.messages[1] = message('too-large.xml')
        self.inbox.sizes[1] = mailbox.MAX_MAIL_BYTES + 1
        self.inbox.messages[2] = message('valid.xml')
        result = self.sync(inbox)
        self.assertEqual((result['imported'], result['error_count']), (1, 1))
        self.assertNotIn(('FETCH', '1', '(BODY.PEEK[])'), self.inbox.calls)
        with db.connection() as connection:
            self.assertEqual(connection.execute('SELECT last_uid FROM mailboxes').fetchone()[0], 2)
        self.inbox.validity = '99'
        result = self.sync(inbox)
        self.assertIn('UID 已重置', result['notice'])
        self.assertEqual(len(self.imports), 1)


if __name__ == '__main__':
    unittest.main()
