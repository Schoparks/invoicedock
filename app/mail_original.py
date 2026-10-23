"""Read-only MIME previews: text and embedded raster images, never active HTML."""
from __future__ import annotations

import base64
from email import policy
from email.parser import BytesParser
from html.parser import HTMLParser
from io import BytesIO
from urllib.parse import urlsplit

from fastapi.responses import Response
from PIL import Image

MAX_BODY = 50_000
MAX_IMAGES = 30
IMAGE_TYPES = {'PNG': 'image/png', 'JPEG': 'image/jpeg', 'GIF': 'image/gif', 'WEBP': 'image/webp'}


def _web_url(value: str) -> str:
    try:
        url = urlsplit(value)
        if url.scheme in ('https', 'http') and url.hostname and not url.username and not url.password:
            return value
    except ValueError:
        pass
    return ''


class _BodyText(HTMLParser):
    """Extract readable text and image references without rendering email HTML."""
    hidden_tags = {'script', 'style', 'head', 'iframe', 'object', 'template'}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.text = []
        self.images = []
        self.hidden = 0

    def handle_starttag(self, tag, attrs):
        if tag in self.hidden_tags:
            self.hidden += 1
        if self.hidden:
            return
        attrs = dict(attrs)
        if tag in ('p', 'div', 'br', 'tr', 'li', 'h1', 'h2', 'h3'):
            self.text.append('\n')
        if tag == 'img' and attrs.get('src'):
            self.images.append((attrs['src'].strip(), attrs.get('alt', '')[:200]))
        if tag == 'a' and (url := _web_url(attrs.get('href', '').strip())):
            self.text.append(f' ({url}) ')

    def handle_endtag(self, tag):
        if tag in self.hidden_tags:
            self.hidden = max(0, self.hidden - 1)
        elif not self.hidden and tag in ('p', 'div', 'tr', 'li'):
            self.text.append('\n')

    def handle_data(self, data):
        if not self.hidden:
            self.text.append(data)


def _image(payload: bytes, name: str) -> dict | None:
    try:
        with Image.open(BytesIO(payload)) as image:
            media_type = IMAGE_TYPES.get(image.format)
            if not media_type or image.width * image.height > 20_000_000:
                return None
            image.verify()
        return {'name': name[:200], 'src': f'data:{media_type};base64,' + base64.b64encode(payload).decode('ascii')}
    except (OSError, ValueError, SyntaxError, Image.DecompressionBombError):
        return None


def original_response(content: bytes, download: bool = False):
    if download:
        return Response(content, media_type='application/octet-stream',
                        headers={'Content-Disposition': 'attachment; filename="original-email.eml"'})
    message = BytesParser(policy=policy.default).parsebytes(content)
    part = message.get_body(preferencelist=('html', 'plain'))
    body, sources = '', []
    if part is not None:
        try:
            body = part.get_content()
        except (LookupError, UnicodeError):
            body = (part.get_payload(decode=True) or b'').decode('utf-8', errors='replace')
        if part.get_content_type() == 'text/html':
            parser = _BodyText()
            parser.feed(body)
            body, sources = ''.join(parser.text).strip(), parser.images
    images, attachments = [], []
    image_count = 0
    for position, part in enumerate(message.walk()):
        if part.is_multipart():
            continue
        name = str(part.get_filename() or '')[:200]
        if name or part.get_content_disposition() == 'attachment':
            attachments.append({'name': name or f'附件 {position}', 'type': part.get_content_type()})
        if part.get_content_type() not in IMAGE_TYPES.values():
            continue
        image_count += 1
        if len(images) < MAX_IMAGES:
            image = _image(part.get_payload(decode=True) or b'', name or '邮件图片')
            if image:
                images.append(image)
    remote_images = []
    for src, name in sources:
        if src.lower().startswith('data:image/') and ';base64,' in src:
            image_count += 1
            if len(images) < MAX_IMAGES:
                try:
                    payload = base64.b64decode(src.split(';base64,', 1)[1], validate=True)
                except ValueError:
                    continue
                image = _image(payload, name or '邮件图片')
                if image:
                    images.append(image)
        elif url := _web_url(src):
            if len(remote_images) < MAX_IMAGES:
                remote_images.append({'name': name or '外部图片', 'url': url})
    return {
        'subject': str(message.get('Subject', ''))[:1000],
        'sender': str(message.get('From', ''))[:1000],
        'recipient': str(message.get('To', ''))[:1000],
        'date': str(message.get('Date', ''))[:200],
        'body': body[:MAX_BODY], 'body_truncated': len(body) > MAX_BODY,
        'images': images, 'images_truncated': image_count > MAX_IMAGES,
        'remote_images': remote_images, 'attachments': attachments[:200],
    }
