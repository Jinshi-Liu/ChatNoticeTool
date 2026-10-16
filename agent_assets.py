"""On-demand attachment preparation. Cache hit = no rescan or conversion."""
from __future__ import annotations

import json
from pathlib import Path
import time
import uuid

from agent_config import AgentError, digest, encode, private_path
from agent_processing import content_hash, has_media
from wechat_content import describe

CONVERTER_VERSION = 2
IMAGES = {'.jpg', '.jpeg', '.png', '.gif', '.bmp', '.webp', '.heic', '.heif'}
DOCUMENTS = {'.pdf', '.doc', '.docx', '.xlsx', '.txt', '.md', '.csv', '.html', '.htm', '.zip'}
SUCCESS = {'image_decoded', 'copied_and_hash_verified', 'copied_local_file', 'thumbnail_available'}


def required_grants(message):
    if message['platform'] == 'wechat':
        info = describe(message)
        if info and not info['media']:
            return set()
        if info and info.get('media_kind') == 'image':
            return {'images_to_agent'}
        if message.get('type') == 'image':
            return {'images_to_agent'}
        if message.get('type') == 'file_link_or_card':
            import xml.etree.ElementTree as ET
            value = message.get('text', '')
            start = value.find('<?xml')
            if start < 0:
                start = value.find('<msg>')
            try:
                xml = ET.fromstring(value[start:]) if start >= 0 else None
                title = xml.findtext('appmsg/title') if xml is not None else ''
                suffix = Path(title or '').suffix.lower()
            except ET.ParseError:
                suffix = ''
            if suffix in IMAGES:
                return {'images_to_agent'}
            if suffix in DOCUMENTS and suffix != '.zip':
                return {'documents_to_agent'}
            return {'documents_to_agent', 'images_to_agent'}
        return set() if not has_media(message) else {'unsupported'}
    grants = set()
    for item in message.get('attachments', []):
        suffix = Path(item.get('filename') or item.get('filepath') or '').suffix.lower()
        extension = str(item.get('file_ext', '')).lower().lstrip('.')
        suffix = suffix or ('.' + extension if extension else '')
        if suffix in IMAGES or (not suffix and (item.get('width') or item.get('height'))):
            grants.add('images_to_agent')
        elif suffix in DOCUMENTS:
            grants.add('documents_to_agent')
            if suffix == '.zip':
                grants.add('images_to_agent')
        else:
            # Unknown bytes could be an image or a document; require both grants.
            grants.update(('images_to_agent', 'documents_to_agent'))
    return grants


def file_stamp(path):
    stat = path.stat()
    return [stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns]


def fingerprints(items, root, config, backend):
    output = {}
    for item in items:
        for key in ('source_path', 'local_path', 'text_path'):
            if not item.get(key):
                continue
            path = Path(item[key])
            if key == 'source_path':
                if not backend.asset_allowed(config, path):
                    raise AgentError('scope_denied', '附件源文件不在当前来源媒体目录内')
            else:
                try:
                    relative = path.relative_to(root)
                except ValueError:
                    raise AgentError('unsafe_path', '准备后的附件必须位于本项目内') from None
                private_path(root, str(relative))
            output[str(path)] = file_stamp(path)
        for member in item.get('archive_members', []):
            for key in ('path', 'text_path'):
                if member.get(key):
                    path = Path(member[key])
                    private_path(root, str(path.relative_to(root)))
                    output[str(path)] = file_stamp(path)
    return output


def prepare(store, payload, *, pid=0, authorize_memory_read=False, timings=False, images_only=False):
    started = time.monotonic()
    if not isinstance(payload, dict):
        raise AgentError('invalid_input', '附件准备输入必须是对象')
    batch_id, keys = payload.get('batch_id'), payload.get('message_keys')
    with store.connection() as db:
        batch, records = store.records(db, batch_id, keys)
        cached_assets = {}
        for record in records:
            resource = store.resource_message(db, batch, record)
            record['resource_message'] = resource
            # A later same-group quote can reuse its original image cache.
            # Bind to resource content + account/group, not to the quoting row;
            # authorization and file fingerprints are still checked on each use.
            resource_version = content_hash({k: v for k, v in resource.items() if k != 'source_id'})
            record['cache_key'] = digest([record['group_key'], resource_version, CONVERTER_VERSION])
            cached_assets[record['cache_key']] = db.execute('SELECT * FROM assets WHERE cache_key=? AND group_key=?',
                                                         (record['cache_key'], record['group_key'])).fetchone()
    if pid and not authorize_memory_read:
        raise AgentError('authorization_required', '图片取钥需要单独明确授权')
    if authorize_memory_read and (type(pid) is not int or pid <= 0):
        raise AgentError('authorization_required', '图片取钥必须指定本人微信客户端PID')
    results, misses, initial_updates = [], {}, []
    for record in records:
        rule = batch['rules'][record['group_key']]
        source = batch['sources'][rule['source_id']]
        from agent_sources import get_source
        backend = get_source(source['platform'])
        if authorize_memory_read and not backend.capabilities.get('memory_read'):
            raise AgentError('unsupported_operation', '该来源不支持也不需要进程内存读取')
        needed = backend.attachment_grants(record['message'])
        if 'unsupported' in needed or not has_media(record['message']):
            results.append({'message_key': record['message_key'], 'status': 'unsupported_or_no_attachment', 'items': []})
            continue
        if images_only and needed != {'images_to_agent'}:
            results.append({'message_key': record['message_key'], 'status': 'not_requested', 'items': [],
                            'notice': '只自动准备纯图片；文档或混合附件请显式attachments prepare'})
            continue
        if any(not rule['grant'][grant] for grant in needed):
            results.append({'message_key': record['message_key'], 'status': 'scope_denied', 'items': [],
                            'notice': '该消息附件所需的图片/文档交付权限尚未授权'})
            continue
        config = store.config.client_config(source)
        cache_key = record['cache_key']
        cached = cached_assets[cache_key]
        hit = False
        if cached:
            items = json.loads(cached['manifest'])
            try:
                hit = fingerprints(items, store.root, config, backend) == json.loads(cached['fingerprint'])
            except (OSError, ValueError):
                hit = False
            if hit:
                initial_updates.append((record['message_key'], items, True))
                results.append({'message_key': record['message_key'], 'status': 'prepared', 'cached': True, 'items': public_items(items)})
        if not hit:
            initial_updates.append((record['message_key'], [], False))
            misses.setdefault(source['id'], []).append((record, cache_key))
    # Invalidate old preparation before attempting conversion. A failed retry
    # must not retain an earlier success flag. One transaction, not one per file.
    store.mark_prepared_many(batch_id, initial_updates)
    final_updates, cache_rows, source_timings = [], [], []
    for source_id, requests in misses.items():
        source = batch['sources'][source_id]
        config = store.config.client_config(source)
        config['policy']['allow_process_memory_read'] = bool(authorize_memory_read)
        output = private_path(store.root, 'exports/agent-assets/' + uuid.uuid4().hex)
        output.mkdir(parents=True, exist_ok=False)
        backend = get_source(source['platform'])
        messages, aliases, unique = [], {}, {}
        for record, _ in requests:
            resource = record['resource_message']
            identity = digest({k: v for k, v in resource.items() if k != 'source_id'})
            if identity not in unique:
                unique[identity] = resource['source_id']
                messages.append(resource)
            aliases[record['message_key']] = unique[identity]
        try:
            clock = time.monotonic()
            report = backend.prepare(config, messages, output, pid)
            source_timings.append({'source_id': source_id, 'selected_messages': len(requests), 'unique_resources': len(messages),
                                   'total_seconds': round(time.monotonic() - clock, 4),
                                   **report.get('timings', {})})
        except Exception:
            # Keep source-wide parser/dependency failures explicit, and let other
            # sources/text messages proceed. Never echo untrusted document data.
            results.extend({'message_key': record['message_key'], 'status': 'pending',
                            'cached': False, 'items': [], 'reason': 'attachment_prepare_failed'}
                           for record, _ in requests)
            continue
        # Another process may have revoked or changed authorization meanwhile.
        with store.connection() as db:
            store.batch(db, batch_id)
        for record, cache_key in requests:
            items = [item for item in report['items'] if item['source_id'] == aliases[record['message_key']]]
            message = record['message']
            expected = max(1, len(message.get('attachments', [])))
            ready = len(items) >= max(1, expected) and all(item['status'] in SUCCESS for item in items)
            # Copied opaque media is not understood. Document/image parsing is
            # intentionally left to the Agent; archive members remain untrusted.
            if ready:
                stamps = fingerprints(items, store.root, config, backend)
                cache_rows.append((cache_key, record['group_key'], encode(stamps), encode(items)))
            final_updates.append((record['message_key'], items, ready))
            results.append({'message_key': record['message_key'], 'status': 'prepared' if ready else 'pending',
                            'cached': False, 'items': public_items(items)})
    if cache_rows:
        with store.connection() as db:
            store.batch(db, batch_id)
            db.executemany('INSERT INTO assets VALUES(?,?,?,?) ON CONFLICT(cache_key) DO UPDATE SET fingerprint=excluded.fingerprint,manifest=excluded.manifest', cache_rows)
    store.mark_prepared_many(batch_id, final_updates)
    result = {'batch_id': batch_id, 'results': results, 'remote_downloads': False,
              'notice': 'prepared仅表示本地资源已准备；请使用当前Agent读图/读文档后再提交analyzed，无法读取则保留pending。'}
    if timings:
        result['timings'] = {'prepare_seconds': round(time.monotonic() - started, 4),
                             'cache_hits': sum(item.get('cached') is True for item in results),
                             'preparation_attempts': sum(item.get('cached') is False for item in results),
                             'sources': source_timings}
    return result


def public_items(items):
    # No paths for unselected candidate files, source media roots, or secrets.
    allowed = {'status', 'filename', 'local_path', 'text_path', 'image_size', 'quality', 'archive_members'}
    return [{k: v for k, v in item.items() if k in allowed} for item in items]
