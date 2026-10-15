#!/usr/bin/env python3
"""Submit trusted MTA envelope recipient + MIME stdin; temporary errors exit 75."""
import argparse
import hashlib
import hmac
import os
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

MAX_BYTES = 20 * 1024 * 1024


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--recipient', required=True, help='Trusted SMTP envelope RCPT TO')
    args = parser.parse_args()
    recipient = args.recipient.strip().lower()
    endpoint = os.environ.get('INVOICE_INGRESS_URL', '')
    secret = os.environ.get('INVOICE_INBOUND_SECRET', '')
    target = urlsplit(endpoint)
    if (len(secret) < 32 or not target.hostname or target.username or target.password or target.fragment
        or target.scheme not in ('https','http')
        or (target.scheme == 'http' and target.hostname not in ('localhost','127.0.0.1','::1'))):
        print('收信投递入口或服务密钥配置无效', file=sys.stderr)
        return 75
    if recipient.count('@') != 1 or any(ord(c) < 33 for c in recipient) or len(recipient) > 320:
        print('信封收件地址无效', file=sys.stderr)
        return 67
    raw = sys.stdin.buffer.read(MAX_BYTES+1)
    if not raw or len(raw) > MAX_BYTES:
        print('原始邮件为空或超过20MB', file=sys.stderr)
        return 65
    stamp = str(int(time.time()))
    signing_input = stamp+'\n'+recipient+'\n'+hashlib.sha256(raw).hexdigest()
    signature = hmac.new(secret.encode(), signing_input.encode(), hashlib.sha256).hexdigest()
    request = Request(endpoint, data=raw, method='POST', headers={
        'Content-Type':'message/rfc822', 'X-Invoice-Timestamp':stamp,
        'X-Invoice-Recipient':recipient, 'X-Invoice-Signature':signature})
    try:
        with urlopen(request, timeout=30) as response:
            if response.status in (200,202): return 0
    except HTTPError as error:
        if error.code == 404:
            print('收票地址不存在或已停用', file=sys.stderr); return 67
        if error.code in (400,413):
            print('原始邮件被拒绝', file=sys.stderr); return 65
        print('收信服务暂时不可用，请保留邮件并重试', file=sys.stderr); return 75
    except (OSError, URLError, ValueError):
        print('收信服务连接失败，请保留邮件并重试', file=sys.stderr); return 75
    print('收信服务未确认持久化，请保留邮件并重试', file=sys.stderr)
    return 75


if __name__ == '__main__': sys.exit(main())
