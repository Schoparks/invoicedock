"""Server-owner maintenance. Run from the source checkout using its Conda environment."""
import argparse
import getpass
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent / '.env')
from app.db import LOCK, connection, initialize
from app.auth import password_hash, _email, _password


def main():
    parser = argparse.ArgumentParser(description='票栈服务器账号维护')
    commands = parser.add_subparsers(dest='command', required=True)
    create = commands.add_parser('create-user', help='在关闭公开注册时创建账号')
    create.add_argument('email')
    create.add_argument('--name', required=True)
    reset = commands.add_parser('reset-password', help='重置密码并撤销所有登录会话')
    reset.add_argument('email')
    args = parser.parse_args()
    email = _email(args.email)
    password = _password(getpass.getpass('新密码（至少10个字符）：'), registering=True)
    if password != getpass.getpass('再次输入密码：'):
        parser.error('两次密码不一致')
    initialize()
    with LOCK, connection(write=True) as db:
        user = db.execute('SELECT id FROM users WHERE email=?', (email,)).fetchone()
        if args.command == 'create-user':
            if user:
                parser.error('该账号已存在')
            name = args.name.strip()
            if not name or len(name) > 80:
                parser.error('姓名须为1至80个字符')
            db.execute('INSERT INTO users VALUES (?,?,?,?,?)', (uuid.uuid4().hex, email, name, password_hash(password), datetime.now(timezone.utc).isoformat()))
        else:
            if not user:
                parser.error('账号不存在')
            db.execute('UPDATE users SET password_hash=? WHERE id=?', (password_hash(password), user['id']))
            db.execute('DELETE FROM sessions WHERE user_id=?', (user['id'],))
    print('账号创建完成。' if args.command == 'create-user' else '密码已重置，原登录会话已全部撤销。')


if __name__ == '__main__':
    main()
