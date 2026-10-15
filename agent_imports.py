"""Portable, bounded file import adapter. No client, network or model access.

Imported data is a private inbox, not a claimed connection to a chat service.
The collection/group namespace and message IDs are only processing identities;
real account, sender, original time and origin descriptions are not required.
"""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
from pathlib import Path, PurePosixPath, PureWindowsPath
import sqlite3
import time
import uuid

from agent_config import AgentError, bound, digest, encode, private_path, read_json, text

APP_ID = 0x434E4931
MAX_FILE = 32 * 1024 * 1024
MAX_TOTAL = 128 * 1024 * 1024
MAX_TEXT = 4 * 1024 * 1024
MAX_MESSAGES = 10000
IMAGE_EXTENSIONS = {'.png', '.jpg', '.jpeg', '.gif', '.webp', '.bmp'}
DOCUMENT_EXTENSIONS = {'.pdf', '.doc', '.docx', '.xlsx', '.txt', '.md', '.csv', '.html', '.htm', '.zip'}


def stamp(path):
    s = path.stat()
    return s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns


def file_digest(path):
    hasher, total = hashlib.sha256(), 0
    with path.open('rb') as stream:
        while chunk := stream.read(1024 * 1024):
            total += len(chunk)
            if total > MAX_FILE:
                raise AgentError('input_too_large', '附件读取超过32MiB限制')
            hasher.update(chunk)
    return hasher.hexdigest()


def image_extension(head):
    if head.startswith(b'\x89PNG\r\n\x1a\n'):
        return '.png'
    if head.startswith(b'\xff\xd8\xff'):
        return '.jpg'
    if head.startswith((b'GIF87a', b'GIF89a')):
        return '.gif'
    if head.startswith(b'RIFF') and head[8:12] == b'WEBP':
        return '.webp'
    if head.startswith(b'BM'):
        return '.bmp'
    return None


def selected_file(value):
    path = Path(text(str(value), 'file', 4000))
    if not path.is_absolute():
        raise AgentError('unsafe_path', '导入文件须使用明确的绝对路径')
    if path.is_symlink() or (path.exists() and getattr(path.lstat(), 'st_file_attributes', 0) & 0x400):
        raise AgentError('unsafe_path', '不导入链接或目录联接文件')
    path = path.parent.resolve() / path.name
    if not path.is_file():
        raise AgentError('path_not_found', '导入文件不存在或不是普通文件')
    return path


def attachment_file(base, value):
    value = text(value, 'attachment.path', 2000)
    normalized = value.replace('\\', '/')
    relative = PurePosixPath(normalized)
    if (relative.is_absolute() or PureWindowsPath(value).drive or ':' in normalized
            or '..' in relative.parts or not relative.parts):
        raise AgentError('unsafe_path', '附件路径必须相对于导入文件目录，不能包含URL、绝对路径或..')
    candidate = base.joinpath(*relative.parts)
    for index in range(1, len(relative.parts) + 1):
        node = base.joinpath(*relative.parts[:index])
        if node.is_symlink() or (node.exists() and getattr(node.lstat(), 'st_file_attributes', 0) & 0x400):
            raise AgentError('unsafe_path', '附件路径不能经过链接或目录联接')
    if not candidate.resolve().is_relative_to(base):
        raise AgentError('unsafe_path', '附件不能离开所选导入文件目录')
    return selected_file(candidate)


class ImportStore:
    def __init__(self, config):
        self.root = Path(config['import']['project_root'])
        self.relative = config['import']['storage_relative']
        self.directory = private_path(self.root, self.relative)
        self.path = private_path(self.root, self.relative + '/messages.sqlite3')

    @contextmanager
    def connection(self, create=False):
        for suffix in ('', '-journal', '-wal', '-shm'):
            private_path(self.root, self.relative + '/messages.sqlite3' + suffix)
        if create:
            self.directory.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(self.path, timeout=5) if create else sqlite3.connect(self.path.as_uri() + '?mode=ro', uri=True, timeout=5)
        db.row_factory = sqlite3.Row
        try:
            app = db.execute('PRAGMA application_id').fetchone()[0]
            tables = db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
            if app not in (0, APP_ID) or (app == 0 and tables) or (not create and app != APP_ID):
                raise AgentError('unknown_database', '该数据库不属于通用导入，拒绝访问')
            if db.execute('PRAGMA user_version').fetchone()[0] > 1:
                raise AgentError('state_version', '导入库版本较新，拒绝降级访问')
            if create:
                db.execute(f'PRAGMA application_id={APP_ID}')
                db.execute('PRAGMA user_version=1')
                db.execute('''CREATE TABLE IF NOT EXISTS records (
                    group_id TEXT NOT NULL, message_id TEXT NOT NULL, timestamp INTEGER NOT NULL,
                    payload TEXT NOT NULL, PRIMARY KEY(group_id,message_id))''')
                db.execute('CREATE INDEX IF NOT EXISTS records_group_time ON records(group_id,timestamp)')
            with db:
                yield db
        finally:
            db.close()


class ImportSource:
    """Minimal adapter reference: override load_rows to support another file format."""
    platform = 'import'
    formats = ('auto', 'json', 'text', 'image', 'document')

    @property
    def capabilities(self):
        return {'platform': self.platform, 'operating_systems': ['windows', 'macos', 'linux'],
                'groups': True, 'local_history': True, 'local_images': True,
                'file_import': True, 'import_formats': list(self.formats), 'memory_read': False,
                'live_receiving': False, 'remote_downloads': False, 'client_version_dependent': False}

    def configure(self, payload):
        if payload.get('paths') not in (None, {}):
            raise AgentError('invalid_input', '通用导入不配置客户端路径；文件由每次import显式选择')
        collection = text(payload.get('collection', 'inbox'), 'collection', 200)
        return {'account_id': digest([self.platform, collection])[:20], 'paths': {}, 'collection': collection}

    def validate_scope(self, group_id, since, until):
        text(group_id, 'group_id', 200)
        if (group_id != group_id.strip() or any(ord(c) < 32 or c in '/\\' for c in group_id)
                or type(since) is not int or type(until) is not int or since < 0 or since >= until):
            raise AgentError('invalid_input', '导入分组ID或时间范围无效')

    def context(self, root, source):
        relative = 'state/imports/' + digest(source['id'])[:32]
        private_path(root, relative)
        return {'import': {'project_root': str(root), 'storage_relative': relative},
                'policy': {'allow_process_memory_read': False}}

    def check(self, config):
        store = ImportStore(config)
        count = 0
        if store.path.is_file():
            with store.connection() as db:
                count = db.execute('SELECT COUNT(*) FROM records').fetchone()[0]
        return {'status': 'ready', 'imported_messages': count, 'read_chat_bodies': False,
                'notice': '文件收件箱，无需登录客户端或取钥，不代表已连接任何聊天服务'}

    def connect(self, config, pids, authorized):
        raise AgentError('unsupported_operation', '文件导入无需内存取钥，请直接使用import命令')

    def groups(self, config):
        store = ImportStore(config)
        rows = []
        if store.path.is_file():
            with store.connection() as db:
                rows = [{'platform': self.platform, 'group_id': r[0], 'name': r[0], 'remark': ''}
                        for r in db.execute('SELECT DISTINCT group_id FROM records ORDER BY group_id')]
        return {'groups': rows, 'read_chat_bodies': False,
                'notice': '仅列出已导入的本地分组；首次可自行创建inbox分组，不需要真实群ID'}

    def load_rows(self, path, format):
        if format not in self.formats:
            raise AgentError('unsupported_import_format', '此适配器不支持该导入格式')
        if format == 'auto':
            format = 'json' if path.suffix.lower() == '.json' else 'text' if path.suffix.lower() in ('.txt', '.md') else 'file'
        if format == 'json':
            value = read_json(path, MAX_FILE)
            if isinstance(value, dict):
                if value.get('schema_version', 1) != 1:
                    raise AgentError('import_version', '不支持此导入格式版本')
                value = value.get('messages', [value])
            if not isinstance(value, list):
                raise AgentError('invalid_input', 'JSON必须是消息数组或包含messages的对象')
            return value
        if format == 'text':
            with path.open('rb') as stream:
                raw = stream.read(MAX_TEXT + 1)
            if len(raw) > MAX_TEXT:
                raise AgentError('input_too_large', '文本超过4MiB，请拆分导入')
            try:
                return [{'text': raw.decode('utf-8-sig')}]
            except UnicodeError:
                raise AgentError('invalid_encoding', '文本须为UTF-8，不会静默替换未知编码') from None
        return [{'attachments': [{'path': path.name, **({'kind': format} if format != 'file' else {})}]}]

    def _asset(self, store, source, descriptor, grant, budget):
        before = stamp(source)
        if before[2] > MAX_FILE:
            raise AgentError('input_too_large', '单个附件超过32MiB限制')
        budget[0] += before[2]
        budget[1] += 1
        if budget[0] > MAX_TOTAL or budget[1] > 200:
            raise AgentError('input_too_large', '本次附件超过128MiB或200个，请分批导入')
        with source.open('rb') as stream:
            detected = image_extension(stream.read(16))
        suffix = detected or source.suffix.lower()
        if detected:
            kind = 'image'
        elif suffix in IMAGE_EXTENSIONS:
            raise AgentError('invalid_image', '图片扩展名与文件签名不符')
        elif suffix in DOCUMENT_EXTENSIONS:
            kind = 'document'
        else:
            raise AgentError('unsupported_import_format', '不支持此文件类型，不执行或猜测任意文件内容')
        if descriptor.get('kind', kind) != kind:
            raise AgentError('invalid_input', '附件kind与实际文件类型不符')
        if not grant['images_to_agent' if kind == 'image' else 'documents_to_agent'] or (suffix == '.zip' and not grant['images_to_agent']):
            raise AgentError('scope_denied', '该文件所需的图片/文档交付权限未授权')
        asset_dir = private_path(store.root, store.relative + '/assets')
        asset_dir.mkdir(parents=True, exist_ok=True)
        temporary = asset_dir / (uuid.uuid4().hex + '.tmp')
        hasher = hashlib.sha256()
        try:
            with source.open('rb') as incoming, temporary.open('xb') as outgoing:
                copied = 0
                while chunk := incoming.read(1024 * 1024):
                    copied += len(chunk)
                    if copied > MAX_FILE:
                        raise AgentError('input_too_large', '复制时附件超过大小限制')
                    hasher.update(chunk)
                    outgoing.write(chunk)
            if stamp(source) != before:
                raise AgentError('file_changed', '导入期间文件变化，请重试')
            sha = hasher.hexdigest()
            if descriptor.get('sha256', sha) != sha:
                raise AgentError('hash_mismatch', '附件SHA256不匹配')
            destination = private_path(store.root, store.relative + '/assets/' + sha + suffix)
            if destination.exists():
                # Only explicitly selected resources are hashed, not old inbox files.
                if destination.stat().st_size != copied or file_digest(destination) != sha:
                    raise AgentError('hash_mismatch', '已保存附件被改动，拒绝覆盖')
            else:
                temporary.replace(destination)
            return {'path': destination.name, 'kind': kind, 'filename': source.name,
                    'sha256': sha, 'size': copied}
        finally:
            temporary.unlink(missing_ok=True)

    def ingest(self, config, group, file, format='auto', timestamp=None):
        path = selected_file(file)
        before = stamp(path)
        if before[2] > MAX_FILE:
            raise AgentError('input_too_large', '导入文件超过32MiB，请分批')
        rows = self.load_rows(path, format)
        if stamp(path) != before:
            raise AgentError('file_changed', '导入期间文件变化，请重试')
        if not isinstance(rows, list) or not 1 <= len(rows) <= MAX_MESSAGES:
            raise AgentError('invalid_input', '每次导入须包含1到10000条消息')
        store = ImportStore(config)
        now = int(time.time())
        fallback = bound(timestamp, 'timestamp') if timestamp is not None else now
        prepared, seen, budget = [], set(), [0, 0]
        # Validate shapes, time scope and attachment references before persistence.
        for row in rows:
            if not isinstance(row, dict) or set(row) - {'id', 'timestamp', 'text', 'attachments'}:
                raise AgentError('invalid_input', '消息仅支持id、timestamp、text、attachments字段；其他格式请使用适配器')
            body = text(row.get('text', ''), 'text', MAX_TEXT, empty=True).replace('\r\n', '\n').replace('\r', '\n')
            files = row.get('attachments', [])
            if not isinstance(files, list) or len(files) > 50 or (not body and not files):
                raise AgentError('invalid_input', '消息需有正文或附件，单条附件最多50个')
            stamp_value = bound(row['timestamp'], 'timestamp') if row.get('timestamp') is not None else fallback
            explicit_time = row.get('timestamp') is not None or timestamp is not None
            if explicit_time and (type(stamp_value) is not int or not group['since'] <= stamp_value <= now or (group['until'] is not None and stamp_value >= group['until'])):
                raise AgentError('scope_denied', '消息时间超出授权范围或晚于本次导入时刻')
            descriptors = []
            for item in files:
                if isinstance(item, str):
                    item = {'path': item}
                if not isinstance(item, dict) or set(item) - {'path', 'kind', 'sha256'}:
                    raise AgentError('invalid_input', '附件需提供相对path，可选kind和sha256')
                source = attachment_file(path.parent, item.get('path'))
                descriptors.append((source, item))
            prepared.append((row, body, stamp_value, descriptors))
        messages, counts = [], {'created': 0, 'updated': 0, 'unchanged': 0}
        with store.connection(create=True) as db:
            db.execute('BEGIN IMMEDIATE')
            for row, body, stamp_value, descriptors in prepared:
                assets = [self._asset(store, source, item, group['grant'], budget) for source, item in descriptors]
                explicit_time = row.get('timestamp') is not None or timestamp is not None
                identity = row.get('id')
                if identity is None:
                    identity = 'auto:' + digest([body, assets, stamp_value if explicit_time else None])
                elif type(identity) is int:
                    identity = str(identity)
                identity = text(identity, 'id', 200)
                if identity in seen:
                    raise AgentError('duplicate_message_id', '同一文件内消息ID重复；请去重或提供不同ID')
                seen.add(identity)
                previous = db.execute('SELECT payload,timestamp FROM records WHERE group_id=? AND message_id=?', (group['group_id'], identity)).fetchone()
                if previous and not explicit_time:
                    stamp_value = previous['timestamp']
                if not group['since'] <= stamp_value <= now or (group['until'] is not None and stamp_value >= group['until']):
                    raise AgentError('scope_denied', '首次导入时间不在当前授权范围，请明确消息时间或重新确认范围')
                message = {'platform': self.platform, 'group_id': group['group_id'], 'group_name': group['name'],
                           'message_id': identity, 'source_id': self.platform + ':' + digest([group['group_id'], identity]), 'timestamp': stamp_value,
                           'timestamp_basis': 'provided' if explicit_time else (json.loads(previous['payload']).get('timestamp_basis', 'imported') if previous else 'imported'),
                           'text': body, 'type': 'imported', 'body_status': 'utf8' if body else 'empty',
                           'attachments': assets, 'attachment_content_status': 'not_processed' if assets else 'not_applicable'}
                encoded = encode(message)
                status = 'created' if not previous else 'unchanged' if previous['payload'] == encoded else 'updated'
                counts[status] += 1
                db.execute('INSERT INTO records VALUES(?,?,?,?) ON CONFLICT(group_id,message_id) DO UPDATE SET timestamp=excluded.timestamp,payload=excluded.payload',
                           (group['group_id'], identity, stamp_value, encoded))
                messages.append(message)
            # Missing times may have reused much older persisted timestamps.
            # Validate the resolved range before the import transaction commits.
            if max(m['timestamp'] for m in messages) - min(m['timestamp'] for m in messages) >= group['max_days'] * 86400:
                raise AgentError('scope_denied', '文件内消息跨度超过单次授权，请拆分文件或确认修改max_days')
        return {'messages': messages, 'counts': counts, 'snapshots': [],
                'notice': '仅导入用户提供的文件；未连接聊天服务。无原时间时使用首次导入时间，无ID时按内容去重。'}

    def read(self, config, request):
        if request.get('confirmed') is not True:
            raise AgentError('scope_denied', '读取范围未确认')
        store, rows = ImportStore(config), []
        if store.path.is_file():
            with store.connection() as db:
                for scope in request['scopes']:
                    if scope['platform'] != self.platform:
                        raise AgentError('scope_denied', '适配器平台不匹配')
                    for group in scope['group_ids']:
                        self.validate_scope(group, scope['since'], scope['until'])
                        rows.extend(json.loads(r[0]) for r in db.execute('SELECT payload FROM records WHERE group_id=? AND timestamp>=? AND timestamp<? ORDER BY timestamp,message_id',
                                                                       (group, scope['since'], scope['until'])))
        return {'messages': rows, 'snapshots': [], 'parse_errors': []}

    def attachment_grants(self, message):
        result = set()
        for item in message.get('attachments', []):
            result.add('images_to_agent' if item['kind'] == 'image' else 'documents_to_agent')
            if Path(item['path']).suffix == '.zip':
                result.add('images_to_agent')
        return result

    def asset_allowed(self, config, path):
        store = ImportStore(config)
        root = private_path(store.root, store.relative + '/assets')
        return path.resolve().is_relative_to(root)

    def prepare(self, config, messages, output, pid=0):
        if pid or config['policy'].get('allow_process_memory_read'):
            raise AgentError('unsupported_operation', '通用导入不使用进程内存')
        store, items = ImportStore(config), []
        for message in messages:
            for attachment in message['attachments']:
                filename = attachment['path']
                if Path(filename).name != filename or not filename.startswith(attachment['sha256']):
                    raise AgentError('unsafe_path', '导入附件引用无效')
                local = private_path(store.root, store.relative + '/assets/' + filename)
                item = {'source_id': message['source_id'], 'filename': attachment['filename'], 'status': 'processing_pending'}
                if local.is_file() and local.stat().st_size <= MAX_FILE and file_digest(local) == attachment['sha256']:
                    item.update(status='copied_and_hash_verified', source_path=str(local), local_path=str(local))
                    if local.suffix in ('.txt', '.md', '.csv', '.html', '.htm'):
                        item['text_path'] = str(local)
                items.append(item)
        return {'items': items}
