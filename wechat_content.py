"""Technical WeChat body classification/projection, not notification policy.

Full redacted bodies remain in the ledger. XML transport attributes are not
useful model input; unfamiliar/malformed cards remain explicit pending media.
Only standard library imports, also usable by portable processing tests.
"""
from __future__ import annotations

import re
import xml.etree.ElementTree as ET


def xml_root(value):
    starts = [p for p in (value.find('<?xml'), value.find('<msg>'), value.find('<msg ')) if p >= 0]
    if not starts or '<!DOCTYPE' in value.upper() or len(value) > 16 * 1024 * 1024:
        return None
    try:
        return ET.fromstring(value[min(starts):])
    except (ET.ParseError, ValueError):
        return None


def body_prefix(message):
    value = message.get('text', '')
    starts = [p for p in (value.find('<?xml'), value.find('<msg>'), value.find('<msg ')) if p >= 0]
    prefix = value[:min(starts)].strip() if starts else ''
    # Strip only a known sender header, never arbitrary human-written text.
    return '' if prefix == str(message.get('sender', '')) + ':' else prefix


def describe(message):
    if message.get('platform') != 'wechat' or message.get('type') not in ('image', 'file_link_or_card'):
        return None
    root = xml_root(message.get('text', ''))
    if root is None:
        return {'media': True, 'kind': 'unknown', 'valid': False}
    if message['type'] == 'image':
        image = root.find('img')
        if image is None:
            return {'media': True, 'kind': 'image', 'valid': False}
        dimensions = {}
        for key in ('width', 'height'):
            value = image.get(key, '')
            if value.isdigit() and len(value) < 7 and 0 < int(value) <= 100000:
                dimensions[key] = int(value)
        caption = root.findtext('text', '') or image.get('caption', '')
        body = '\n'.join(p for p in (body_prefix(message), caption) if p)
        return {'media': True, 'kind': 'image', 'valid': True, 'text': body, 'dimensions': dimensions}
    app = root.find('appmsg')
    if app is None:
        return {'media': True, 'kind': 'unknown', 'valid': False}
    subtype, title = app.findtext('type', ''), app.findtext('title', '')
    title = '\n'.join(p for p in (body_prefix(message), title) if p)
    if subtype == '6':
        return {'media': True, 'kind': 'file', 'valid': True, 'text': title, 'filename': app.findtext('title', '')}
    if subtype == '5':
        return {'media': False, 'kind': 'link', 'valid': True, 'text': title,
                'description': app.findtext('des', ''), 'url': app.findtext('url', '')}
    if subtype != '57':
        return {'media': True, 'kind': 'unknown', 'valid': False}
    reference = app.find('refermsg')
    if reference is None:
        return {'media': True, 'kind': 'quote', 'valid': False}
    reference_type = reference.findtext('type', '')
    identity = reference.findtext('svrid', '') or reference.findtext('serverid', '')
    # No lookup outside this group is ever performed using these identifiers.
    ref = {'type': reference_type, 'server_id': identity}
    embedded = reference.findtext('content', '')
    resource = None
    if reference_type == '3':
        nested = xml_root(embedded)
        if nested is not None and nested.find('img') is not None:
            image = nested.find('img')
            if re.fullmatch('[0-9a-fA-F]{32}', image.get('md5', '')):
                resource = embedded
    return {'media': reference_type != '1', 'kind': 'quote', 'valid': True,
            'text': title, 'reference': ref, 'resource_text': resource,
            'media_kind': 'image' if reference_type == '3' else 'unknown'}


def compact_message(message):
    info = describe(message)
    if not info or not info.get('valid'):
        return message
    value = {k: message[k] for k in ('platform', 'source_id', 'group_id', 'group_name', 'message_id',
                                    'timestamp', 'time', 'sender', 'server_id', 'body_status') if k in message}
    value.update(type=info['kind'], text=info['text'], has_media=info['media'])
    for key in ('dimensions', 'reference', 'description', 'url', 'filename'):
        if info.get(key):
            value[key] = info[key]
    return value
