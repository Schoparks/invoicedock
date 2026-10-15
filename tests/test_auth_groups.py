"""Authentication, group authorization, and migration checks on disposable databases."""
from contextlib import asynccontextmanager
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import time
import unittest
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import auth, db, groups
from app.permissions import invoice_access


@asynccontextmanager
async def lifespan(app):
    db.initialize()
    yield


class AuthGroupTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.data = Path(temporary.name)
        patcher = patch.object(db, 'DATA', self.data)
        patcher.start()
        self.addCleanup(patcher.stop)
        environment = patch.dict(os.environ, {'ALLOW_REGISTRATION': 'true', 'COOKIE_SECURE': 'false', 'CLAIM_LEGACY_DATA': 'false'})
        environment.start()
        self.addCleanup(environment.stop)
        self.app = FastAPI(lifespan=lifespan)
        self.app.include_router(auth.router)
        self.app.include_router(groups.router)
        self.serial = 0
        self.owner_client, self.owner = self.account()

    def account(self, email=None):
        self.serial += 1
        client = TestClient(self.app)
        client.__enter__()
        self.addCleanup(client.__exit__, None, None, None)
        response = client.post('/api/auth/register', json={'email': email or f'user{self.serial}@example.test', 'name': f'用户{self.serial}', 'password': 'a-long-secret-passphrase'})
        self.assertEqual(response.status_code, 200, response.text)
        return client, response.json()['user']

    def group(self, client=None):
        response = (client or self.owner_client).post('/api/groups', json={'name': '共享项目组'})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def add_member(self, group, client):
        response = self.owner_client.post('/api/groups/' + group['id'] + '/invites')
        self.assertEqual(response.status_code, 200, response.text)
        token = response.json()['token']
        response = client.post('/api/groups/join', json={'token': token})
        self.assertEqual(response.status_code, 200, response.text)
        return token

    def invoice(self, user, number='12345678', digest=None):
        invoice_id = f'invoice-{user["id"]}-{number}'
        with db.connection(write=True) as connection:
            connection.execute('''INSERT INTO invoices(id,number,seller,date,amount_cents,verified,file_hash,owner_id,created_at)
                VALUES (?,?,?,'2026-10-01',1000,1,?,?,?)''', (invoice_id, number, '测试商家', digest, user['id'], '2026-10-01'))
        return invoice_id

    def test_password_sessions_expiry_logout_and_registration_switch(self):
        token = self.owner_client.cookies.get(auth.COOKIE_NAME)
        with db.connection() as connection:
            row = connection.execute('SELECT * FROM sessions').fetchone()
            self.assertNotEqual(row['token_hash'], token)
            self.assertEqual(row['token_hash'], hashlib.sha256(token.encode()).hexdigest())
            encoded = connection.execute('SELECT password_hash FROM users').fetchone()[0]
            self.assertTrue(encoded.startswith('scrypt-v1$'))
            self.assertNotIn('a-long-secret-passphrase', encoded)
        self.owner_client.post('/api/auth/logout')
        self.assertIsNone(self.owner_client.get('/api/auth/status').json()['user'])
        with db.connection() as connection:
            self.assertEqual(connection.execute('SELECT COUNT(*) FROM sessions').fetchone()[0], 0)
        response = self.owner_client.post('/api/auth/login', json={'email': self.owner['email'].upper(), 'password': 'a-long-secret-passphrase'})
        self.assertEqual(response.status_code, 200)
        self.assertIn('HttpOnly', response.headers['set-cookie'])
        self.assertIn('SameSite=lax', response.headers['set-cookie'])
        with db.connection(write=True) as connection:
            connection.execute('UPDATE sessions SET expires_at=?', (int(time.time()) - 1,))
        self.assertEqual(self.owner_client.get('/api/groups').status_code, 401)
        with patch.dict(os.environ, {'ALLOW_REGISTRATION': 'false'}):
            self.assertFalse(self.owner_client.get('/api/auth/status').json()['registration_open'])
            self.assertEqual(self.owner_client.post('/api/auth/register', json={}).status_code, 403)
        with patch.dict(os.environ, {'COOKIE_SECURE': 'true'}):
            response = self.owner_client.post('/api/auth/login', json={'email': self.owner['email'], 'password': 'a-long-secret-passphrase'})
            self.assertIn('Secure', response.headers['set-cookie'])

    def test_login_failures_are_rate_limited(self):
        for _ in range(10):
            response = self.owner_client.post('/api/auth/login', json={'email': self.owner['email'], 'password': 'wrong-password'})
            self.assertEqual(response.status_code, 401)
        response = self.owner_client.post('/api/auth/login', json={'email': self.owner['email'], 'password': 'a-long-secret-passphrase'})
        self.assertEqual(response.status_code, 429)
        self.assertEqual(response.headers['Retry-After'], '900')

    def login_client(self, email=None, password='a-long-secret-passphrase'):
        client = TestClient(self.app)
        self.addCleanup(client.close)
        response = client.post('/api/auth/login', json={'email': email or self.owner['email'], 'password': password})
        self.assertEqual(response.status_code, 200, response.text)
        return client

    def test_chinese_display_name_can_change_and_repeat_but_cannot_login(self):
        other, member = self.account()
        second_session = self.login_client()
        original_token = self.owner_client.cookies.get(auth.COOKIE_NAME)
        response = self.owner_client.patch('/api/auth/profile', json={
            'name': '  中文用户名  ', 'current_password': 'a-long-secret-passphrase'})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()['user'], {**self.owner, 'name': '中文用户名'})
        self.assertEqual(self.owner_client.cookies.get(auth.COOKIE_NAME), original_token)
        self.assertEqual(second_session.get('/api/auth/status').json()['user']['name'], '中文用户名')
        response = other.patch('/api/auth/profile', json={'name': '中文用户名', 'current_password': 'a-long-secret-passphrase'})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()['user']['id'], member['id'])
        for payload in ({'email': '中文用户名'}, {'username': '中文用户名'}):
            response = second_session.post('/api/auth/login', json={**payload, 'password': 'a-long-secret-passphrase'})
            self.assertEqual(response.status_code, 400, response.text)
        self.owner_client.post('/api/auth/logout')
        self.assertEqual(self.owner_client.post('/api/auth/login', json={
            'email': self.owner['email'], 'password': 'a-long-secret-passphrase'}).status_code, 200)

    def test_profile_validation_requires_password_and_rejects_taken_email_atomically(self):
        _, member = self.account()
        for payload, status in [
            ({'name': '新名字'}, 400),
            ({'name': '新名字', 'current_password': 'wrong-password'}, 403),
            ({'email': 'invalid', 'current_password': 'a-long-secret-passphrase'}, 400),
            ({'name': '不能保存', 'email': member['email'].upper(), 'current_password': 'a-long-secret-passphrase'}, 409),
        ]:
            response = self.owner_client.patch('/api/auth/profile', json=payload)
            self.assertEqual(response.status_code, status, response.text)
            self.assertEqual(self.owner_client.get('/api/auth/status').json()['user'], self.owner)
        for name in ('', '字' * 81, '换\n行', None):
            response = self.owner_client.patch('/api/auth/profile', json={'name': name, 'current_password': 'a-long-secret-passphrase'})
            self.assertEqual(response.status_code, 400, response.text)

    def test_email_change_rotates_sessions_and_preserves_ownership(self):
        group = self.group()
        invoice = self.invoice(self.owner)
        other, _ = self.account()
        second_session = self.login_client()
        original_token = self.owner_client.cookies.get(auth.COOKIE_NAME)
        response = self.owner_client.patch('/api/auth/profile', json={
            'email': ' New-Email@Example.Test ', 'current_password': 'a-long-secret-passphrase'})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()['user'], {**self.owner, 'email': 'new-email@example.test'})
        self.assertNotEqual(self.owner_client.cookies.get(auth.COOKIE_NAME), original_token)
        self.assertIsNone(second_session.get('/api/auth/status').json()['user'])
        self.assertIsNotNone(other.get('/api/auth/status').json()['user'])
        self.assertEqual(self.owner_client.get('/api/groups').json()[0]['id'], group['id'])
        with db.connection() as connection:
            self.assertEqual(connection.execute('SELECT owner_id FROM invoices WHERE id=?', (invoice,)).fetchone()[0], self.owner['id'])
        self.assertEqual(second_session.post('/api/auth/login', json={
            'email': self.owner['email'], 'password': 'a-long-secret-passphrase'}).status_code, 401)
        self.assertEqual(second_session.post('/api/auth/login', json={
            'email': 'new-email@example.test', 'password': 'a-long-secret-passphrase'}).status_code, 200)

    def test_password_change_rejects_invalid_passwords_and_revokes_other_sessions(self):
        second_session = self.login_client()
        original_token = self.owner_client.cookies.get(auth.COOKIE_NAME)
        for payload, status in [
            ({'new_password': 'another-long-secret'}, 400),
            ({'current_password': 'wrong-password', 'new_password': 'another-long-secret'}, 403),
            ({'current_password': 'a-long-secret-passphrase', 'new_password': 'short'}, 400),
        ]:
            response = self.owner_client.post('/api/auth/password', json=payload)
            self.assertEqual(response.status_code, status, response.text)
            self.assertIsNotNone(second_session.get('/api/auth/status').json()['user'])
        new_password = ' 新的中文密码 12345 '
        response = self.owner_client.post('/api/auth/password', json={
            'current_password': 'a-long-secret-passphrase', 'new_password': new_password})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()['user'], self.owner)
        self.assertNotEqual(self.owner_client.cookies.get(auth.COOKIE_NAME), original_token)
        self.assertIsNone(second_session.get('/api/auth/status').json()['user'])
        self.assertEqual(second_session.post('/api/auth/login', json={
            'email': self.owner['email'], 'password': 'a-long-secret-passphrase'}).status_code, 401)
        self.assertEqual(second_session.post('/api/auth/login', json={
            'email': self.owner['email'], 'password': new_password}).status_code, 200)
        with db.connection() as connection:
            encoded = connection.execute('SELECT password_hash FROM users WHERE id=?', (self.owner['id'],)).fetchone()[0]
            self.assertNotIn(new_password, encoded)
            self.assertTrue(auth.verify_password(new_password, encoded))

    def test_account_changes_require_login_and_share_password_attempt_limit(self):
        anonymous = TestClient(self.app)
        self.addCleanup(anonymous.close)
        self.assertEqual(anonymous.patch('/api/auth/profile', json={'name': '中文'}).status_code, 401)
        self.assertEqual(anonymous.post('/api/auth/password', json={'new_password': 'another-long-secret'}).status_code, 401)
        for n in range(10):
            if n % 2:
                response = self.owner_client.patch('/api/auth/profile', json={'name': '中文', 'current_password': 'wrong-password'})
            else:
                response = self.owner_client.post('/api/auth/password', json={'new_password': 'another-long-secret', 'current_password': 'wrong-password'})
            self.assertEqual(response.status_code, 403, response.text)
        response = self.owner_client.patch('/api/auth/profile', json={'name': '中文', 'current_password': 'a-long-secret-passphrase'})
        self.assertEqual(response.status_code, 429)
        self.assertEqual(response.headers['Retry-After'], '900')
        self.assertEqual(self.owner_client.get('/api/auth/status').json()['user'], self.owner)

    def test_invites_are_single_use_and_only_owner_can_manage_admins(self):
        group = self.group()
        member_client, member = self.account()
        outsider_client, outsider = self.account()
        token = self.add_member(group, member_client)
        self.assertEqual(outsider_client.post('/api/groups/join', json={'token': token}).status_code, 400)
        self.assertEqual(outsider_client.get(f'/api/groups/{group["id"]}/members').status_code, 403)
        self.assertEqual(member_client.post(f'/api/groups/{group["id"]}/invites').status_code, 403)
        path = f'/api/groups/{group["id"]}/members/{member["id"]}'
        self.assertEqual(member_client.patch(path, json={'role': 'admin'}).status_code, 403)
        self.assertEqual(self.owner_client.patch(path, json={'role': 'admin'}).status_code, 200)
        invite = member_client.post(f'/api/groups/{group["id"]}/invites').json()['token']
        self.assertEqual(member_client.delete(f'/api/groups/{group["id"]}/members/{self.owner["id"]}').status_code, 409)
        self.assertEqual(self.owner_client.patch(path, json={'role': 'member'}).status_code, 200)
        self.assertEqual(outsider_client.post('/api/groups/join', json={'token': invite}).status_code, 400)
        invite = self.owner_client.post(f'/api/groups/{group["id"]}/invites').json()['token']
        with db.connection(write=True) as connection:
            connection.execute('UPDATE group_invites SET expires_at=0')
        self.assertEqual(outsider_client.post('/api/groups/join', json={'token': invite}).status_code, 400)
        self.assertEqual(len(member_client.get('/api/groups').json()), 1)

    def test_invoice_visibility_shares_and_group_duplicate_protection(self):
        group, another_group = self.group(), self.group()
        member_client, member = self.account()
        outsider_client, outsider = self.account()
        self.add_member(group, member_client)
        self.add_member(another_group, member_client)
        first = self.invoice(self.owner, digest='same-original')
        second = self.invoice(member, digest='same-original')
        self.assertEqual(self.owner_client.put(f'/api/invoices/{first}/shares', json={'group_ids': [group['id']]}).status_code, 200)
        self.assertEqual(member_client.put(f'/api/invoices/{first}/shares', json={'group_ids': []}).status_code, 403)
        self.assertEqual(outsider_client.get(f'/api/invoices/{first}/shares').status_code, 403)
        self.assertEqual(member_client.put(f'/api/invoices/{second}/shares', json={'group_ids': [group['id']]}).status_code, 409)
        self.assertEqual(member_client.put(f'/api/invoices/{second}/shares', json={'group_ids': [another_group['id']]}).status_code, 200)
        stranger_group = self.group(outsider_client)
        self.assertEqual(self.owner_client.put(f'/api/invoices/{first}/shares', json={'group_ids': [stranger_group['id']]}).status_code, 403)
        with db.connection() as connection:
            invoice = connection.execute('SELECT * FROM invoices WHERE id=?', (first,)).fetchone()
            self.assertTrue(invoice_access(connection, invoice, self.owner['id']))
            self.assertFalse(invoice_access(connection, invoice, member['id']))
            self.assertTrue(invoice_access(connection, invoice, member['id'], group['id']))
            self.assertFalse(invoice_access(connection, invoice, member['id'], group['id'], edit=True))
            self.assertFalse(invoice_access(connection, invoice, outsider['id'], group['id']))
        self.assertEqual(self.owner_client.get(f'/api/invoices/{first}/shares').json()['group_ids'], [group['id']])

    def test_leaving_and_unsharing_block_active_claims_and_preserve_paid_history(self):
        group = self.group()
        member_client, member = self.account()
        self.add_member(group, member_client)
        invoice = self.invoice(member)
        member_client.put(f'/api/invoices/{invoice}/shares', json={'group_ids': [group['id']]})
        with db.connection(write=True) as connection:
            connection.execute('''INSERT INTO batches(id,name,status,month,total_cents,created_at,updated_at,owner_id,group_id)
                VALUES ('batch','报销单','draft','2026-10',1000,'today','today',?,?)''', (self.owner['id'], group['id']))
            connection.execute('INSERT INTO batch_items VALUES (?,?,?)', ('batch', invoice, json.dumps({'id': invoice, 'seller': '原始商家'})))
            connection.execute("UPDATE invoices SET status='draft',batch_id='batch' WHERE id=?", (invoice,))
        member_path = f'/api/groups/{group["id"]}/members/{member["id"]}'
        self.assertEqual(member_client.delete(member_path).status_code, 409)
        self.assertEqual(self.owner_client.delete(member_path).status_code, 409)
        self.assertEqual(member_client.put(f'/api/invoices/{invoice}/shares', json={'group_ids': []}).status_code, 409)
        with db.connection(write=True) as connection:
            connection.execute("UPDATE batches SET status='paid'")
            connection.execute("UPDATE invoices SET status='paid'")
        self.assertEqual(member_client.delete(member_path).status_code, 200)
        with db.connection() as connection:
            self.assertEqual(connection.execute('SELECT COUNT(*) FROM invoice_shares').fetchone()[0], 0)
            snapshot = json.loads(connection.execute('SELECT snapshot FROM batch_items').fetchone()[0])
            self.assertEqual(snapshot['seller'], '原始商家')
            self.assertEqual(connection.execute('SELECT status FROM invoices').fetchone()[0], 'paid')
        self.assertEqual(member_client.get(f'/api/groups/{group["id"]}/members').status_code, 403)

    def test_member_with_group_mailbox_must_remove_mailbox_before_leaving(self):
        group = self.group()
        member_client, member = self.account()
        self.add_member(group, member_client)
        with db.connection(write=True) as connection:
            connection.execute('CREATE TABLE mailboxes(id TEXT,owner_id TEXT,group_id TEXT,enabled INTEGER)')
            connection.execute('INSERT INTO mailboxes VALUES (?,?,?,0)', ('mailbox', member['id'], group['id']))
        path = f'/api/groups/{group["id"]}/members/{member["id"]}'
        self.assertEqual(member_client.delete(path).status_code, 409)
        with db.connection(write=True) as connection:
            connection.execute('DELETE FROM mailboxes')
        self.assertEqual(member_client.delete(path).status_code, 200)

    def test_owner_scopes_allow_independent_personal_quotas_and_project_names(self):
        _, member = self.account()
        with db.connection(write=True) as connection:
            for user in (self.owner, member):
                connection.execute('INSERT INTO quotas VALUES (?,?,?,?,?,NULL)', (user['id'], '2026-10', '餐饮', 1000, user['id']))
                connection.execute('INSERT INTO projects VALUES (?,?,?,?,?,NULL)', (user['id'], '同名项目', '', 'today', user['id']))
        with self.assertRaises(sqlite3.IntegrityError), db.connection(write=True) as connection:
            connection.execute('INSERT INTO quotas VALUES (?,?,?,?,?,NULL)', ('duplicate', '2026-10', '餐饮', 2000, self.owner['id']))


class LegacyMigrationTests(unittest.TestCase):
    def test_migration_preserves_original_ownership_and_batch_snapshots(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(db, 'DATA', Path(directory)):
            path = Path(directory) / 'invoices.sqlite3'
            with sqlite3.connect(path) as connection:
                connection.executescript('''
                    CREATE TABLE projects(id TEXT PRIMARY KEY,name TEXT UNIQUE,note TEXT,created_at TEXT);
                    CREATE TABLE quotas(id TEXT PRIMARY KEY,month TEXT,category TEXT,amount_cents INTEGER,UNIQUE(month,category));
                    CREATE TABLE batches(id TEXT PRIMARY KEY,name TEXT,status TEXT,project_id TEXT REFERENCES projects(id),quota_id TEXT REFERENCES quotas(id),month TEXT,total_cents INTEGER,created_at TEXT,updated_at TEXT);
                    CREATE TABLE invoices(id TEXT PRIMARY KEY,number TEXT,seller TEXT,buyer TEXT,date TEXT,category TEXT,amount_cents INTEGER,project_id TEXT REFERENCES projects(id),note TEXT,verified INTEGER,file_name TEXT,file_path TEXT,file_hash TEXT UNIQUE,source TEXT,review_note TEXT,raw_text TEXT,status TEXT,batch_id TEXT REFERENCES batches(id),created_at TEXT);
                    CREATE UNIQUE INDEX invoices_number ON invoices(number) WHERE number != '';
                    CREATE TABLE batch_items(batch_id TEXT REFERENCES batches(id),invoice_id TEXT REFERENCES invoices(id),snapshot TEXT,PRIMARY KEY(batch_id,invoice_id));
                    INSERT INTO projects VALUES ('p','旧项目','','2026-10-01');
                    INSERT INTO quotas VALUES ('q','2026-10','餐饮',1000);
                    INSERT INTO batches VALUES ('b','旧报销','draft','p','q','2026-10',1000,'2026-10-01','2026-10-01');
                    INSERT INTO invoices VALUES ('i','123','旧商家','','2026-10-01','餐饮',1000,'p','',1,'old.xml','originals/old.xml','old-hash','XML','','raw text','draft','b','2026-10-01');
                    INSERT INTO batch_items VALUES ('b','i','{"seller":"历史商家"}');
                    PRAGMA user_version=1;
                ''')
            db.initialize()
            db.initialize()
            with db.connection() as connection:
                self.assertEqual(connection.execute('PRAGMA foreign_key_check').fetchall(), [])
                self.assertEqual(connection.execute('PRAGMA user_version').fetchone()[0], 2)
                invoice = dict(connection.execute('SELECT * FROM invoices').fetchone())
                self.assertIsNone(invoice['owner_id'])
                self.assertEqual(invoice['file_path'], 'originals/old.xml')
                self.assertEqual(invoice['raw_text'], 'raw text')
                self.assertEqual(json.loads(connection.execute('SELECT snapshot FROM batch_items').fetchone()[0])['seller'], '历史商家')
                self.assertIsNone(connection.execute('SELECT group_id FROM batches').fetchone()[0])


if __name__ == '__main__':
    unittest.main()
