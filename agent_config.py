"""Private Agent configuration. No client reads or persistent memory-read grants."""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import time
import uuid

from chatnotice import ROOT, parse_bound

SCHEMA_VERSION = 1
MAX_INPUT = 1024 * 1024
PLATFORMS = ('qq', 'wechat')


class AgentError(ValueError):
    def __init__(self, code, message, next_actions=()):
        super().__init__(message)
        self.code = code
        self.next_actions = list(next_actions)


def encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def digest(value):
    return hashlib.sha256(encode(value).encode('utf-8')).hexdigest()


def private_path(root: Path, relative: str) -> Path:
    """Do not cross symlinks, junctions or an untrusted project-root redirect."""
    root = Path(root).absolute()
    if root.resolve() != root:
        raise AgentError('unsafe_path', '项目目录不能通过链接或目录联接访问')
    name = Path(relative)
    if name.is_absolute() or '..' in name.parts:
        raise AgentError('unsafe_path', '路径必须位于本项目内')
    path = root / name
    for candidate in (root, *[root.joinpath(*name.parts[:i]) for i in range(1, len(name.parts) + 1)]):
        if candidate.is_symlink() or (candidate.exists() and getattr(candidate.lstat(), 'st_file_attributes', 0) & 0x400):
            raise AgentError('unsafe_path', '拒绝链接或目录联接')
    if not path.resolve().is_relative_to(root):
        raise AgentError('unsafe_path', '路径必须位于本项目内')
    return path


def read_json(path: Path, max_size=MAX_INPUT):
    with path.open('rb') as stream:
        data = stream.read(max_size + 1)
    if len(data) > max_size:
        raise AgentError('input_too_large', 'JSON超过大小限制，请分批提交')
    try:
        return json.loads(data.decode('utf-8-sig'))
    except (ValueError, UnicodeError):
        raise AgentError('invalid_json', '输入必须是有效的UTF-8 JSON') from None


def atomic_json(path: Path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    try:
        with temp.open('x', encoding='utf-8') as stream:
            stream.write(encode(value) + '\n')
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def text(value, field, limit=500, *, empty=False):
    if not isinstance(value, str) or (not empty and not value.strip()) or len(value) > limit:
        raise AgentError('invalid_input', f'{field}必须是长度不超过{limit}的字符串')
    return value


def bound(value, field):
    if type(value) is int:
        return value
    try:
        return parse_bound(text(value, field, 80), end=(field == 'until'))
    except (ValueError, TypeError, OverflowError):
        raise AgentError('invalid_input', f'{field}需要日期或ISO时间') from None


class AgentConfig:
    def __init__(self, root=ROOT):
        self.root = Path(root).absolute()
        self.path = private_path(self.root, 'state/agent-config.json')

    def load(self):
        if not self.path.exists():
            return {'schema_version': SCHEMA_VERSION, 'revision': 0, 'sources': {}, 'groups': {}}
        value = read_json(self.path)
        if not isinstance(value, dict) or value.get('schema_version') != SCHEMA_VERSION:
            raise AgentError('config_version', '配置版本不兼容，拒绝读取或降级写入')
        if type(value.get('revision')) is not int or not isinstance(value.get('sources'), dict) or not isinstance(value.get('groups'), dict):
            raise AgentError('invalid_config', 'Agent配置结构损坏，请恢复备份')
        return value

    @contextmanager
    def locked(self):
        lock = private_path(self.root, 'state/agent-config.lock')
        lock.parent.mkdir(parents=True, exist_ok=True)
        try:
            descriptor = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            raise AgentError('config_busy', '配置正在修改；如进程曾异常退出，请确认无写入者后移除锁文件') from None
        try:
            os.close(descriptor)
            yield
        finally:
            lock.unlink(missing_ok=True)

    def change(self, payload, update):
        if not isinstance(payload, dict) or payload.get('confirmed') is not True:
            raise AgentError('confirmation_required', '请先向用户展示变更并取得确认')
        with self.locked():
            value = self.load()
            if type(payload.get('expected_revision')) is not int or payload['expected_revision'] != value['revision']:
                raise AgentError('config_conflict', '配置已变化，请重新预览', ['inspect'])
            result = update(value)
            value['revision'] += 1
            value['updated_at'] = int(time.time())
            value['installation'] = {'root': str(self.root), 'python': os.path.abspath(os.sys.executable)}
            if len(encode(value).encode('utf-8')) > MAX_INPUT:
                raise AgentError('config_too_large', '配置超过1MiB限制，请缩短群说明或减少配置项')
            atomic_json(self.path, value)
        return {'revision': value['revision'], **result}

    def source(self, source_id, value=None):
        value = value or self.load()
        source = value['sources'].get(source_id)
        if source is None:
            raise AgentError('source_not_found', '来源不存在', ['source configure'])
        return source

    def configure_source(self, payload):
        def update(value):
            platform = payload.get('platform')
            from agent_sources import get_source
            platform = text(platform, 'platform', 40)
            settings = get_source(platform).configure(payload)
            account_id = settings['account_id']
            source_id = platform + ':' + account_id
            previous = value['sources'].get(source_id)
            source = {**settings, 'id': source_id, 'platform': platform,
                      'name': text(payload.get('name', platform), 'name', 200)}
            if previous and {k: v for k, v in previous.items() if k != 'name'} != {k: v for k, v in source.items() if k != 'name'} and any(g['source_id'] == source_id for g in value['groups'].values()):
                raise AgentError('source_in_use', '请先移除该来源的群授权，再修改媒体/账号目录')
            value['sources'][source_id] = source
            return {'source': source}
        return self.change(payload, update)

    def normalize_groups(self, payload, value):
        rows = payload.get('groups')
        if not isinstance(rows, list) or not 1 <= len(rows) <= 100:
            raise AgentError('invalid_input', 'groups必须包含1到100个群')
        result = []
        for row in rows:
            if not isinstance(row, dict):
                raise AgentError('invalid_input', '群配置必须是对象')
            source = self.source(row.get('source_id'), value)
            group_id = text(row.get('group_id'), 'group_id', 200)
            since = bound(row.get('since'), 'since')
            until = bound(row['until'], 'until') if row.get('until') is not None else None
            from agent_sources import get_source
            get_source(source['platform']).validate_scope(group_id, since, until if until is not None else since + 1)
            days = row.get('max_days', 7)
            if type(days) is not int or not 1 <= days <= 31:
                raise AgentError('invalid_input', 'max_days必须是1到31的整数')
            grant = row.get('grant')
            if (not isinstance(grant, dict) or set(grant) != {'text_to_agent', 'images_to_agent', 'documents_to_agent'}
                    or any(type(v) is not bool for v in grant.values()) or not grant['text_to_agent']):
                raise AgentError('invalid_input', 'grant必须明确确认正文、图片和文档交付权限；正文须允许')
            recipient = text(row.get('recipient'), 'recipient', 300)
            enabled = row.get('enabled', True)
            if type(enabled) is not bool:
                raise AgentError('invalid_input', 'enabled必须是布尔值')
            key = source['id'] + '/' + group_id
            result.append({'key': key, 'source_id': source['id'], 'group_id': group_id,
                           'name': text(row.get('name', group_id), 'name', 300),
                           'instructions': text(row.get('instructions', ''), 'instructions', 8000, empty=True),
                           'since': since, 'until': until, 'max_days': days, 'enabled': enabled,
                           'grant': grant, 'recipient': recipient})
        if len({g['key'] for g in result}) != len(result):
            raise AgentError('invalid_input', '群配置重复')
        return result

    def plan_groups(self, payload):
        value = self.load()
        if payload.get('expected_revision') != value['revision']:
            raise AgentError('config_conflict', '配置已变化，请重新inspect')
        rows = self.normalize_groups(payload, value)
        return {'expected_revision': value['revision'], 'changes': [
            {'before': value['groups'].get(g['key']), 'after': g} for g in rows],
            'notice': '本操作只配置授权和规则，不读取正文。内容交给当前Agent可能离开本机。'}

    def apply_groups(self, payload):
        def update(value):
            rows = self.normalize_groups(payload, value)
            for row in rows:
                value['groups'][row['key']] = row
            return {'groups': rows}
        return self.change(payload, update)

    def remove_groups(self, payload):
        def update(value):
            keys = payload.get('group_keys')
            if not isinstance(keys, list) or not keys or any(k not in value['groups'] for k in keys):
                raise AgentError('group_not_found', '请提供准确的已配置group_keys')
            for key in keys:
                del value['groups'][key]
            return {'removed': keys}
        return self.change(payload, update)

    def authorized(self, keys=None, value=None):
        value = value or self.load()
        if keys is None:
            keys = [k for k, g in value['groups'].items() if g['enabled']]
        if not keys or len(keys) != len(set(keys)):
            raise AgentError('groups_required', '请选择已授权群', ['groups plan'])
        result = []
        for key in keys:
            row = value['groups'].get(key)
            if not row or not row['enabled'] or not row['grant']['text_to_agent']:
                raise AgentError('scope_denied', '群未授权、已停用或已撤销')
            self.source(row['source_id'], value)
            result.append(row)
        return result

    def client_config(self, source, value=None):
        # Context belongs to this source only. Never borrow another account's roots.
        from agent_sources import get_source
        return get_source(source['platform']).context(self.root, source)
