"""Small local-source adapter boundary. Import client/crypto code only on use."""
from __future__ import annotations

from itertools import islice
import os
from pathlib import Path
import sys

from agent_config import AgentError, PLATFORMS, digest, read_json, text
from chatnotice import ReadScope


class LocalSource:
    def __init__(self, platform):
        if platform not in PLATFORMS:
            raise AgentError('unsupported_source', '未实现此消息来源')
        self.platform = platform

    @property
    def capabilities(self):
        return {'platform': self.platform, 'operating_systems': ['windows'],
                'groups': True, 'local_history': True, 'local_images': True,
                'live_receiving': False, 'remote_downloads': False,
                'client_version_dependent': True, 'file_import': False, 'memory_read': True}

    def configure(self, payload):
        supplied = payload.get('paths')
        allowed = {'database_dir', 'media_dir'} if self.platform == 'qq' else {'database_dir', 'account_dir'}
        if not isinstance(supplied, dict) or set(supplied) != allowed:
            raise AgentError('invalid_input', '必须明确提供路径字段：' + ', '.join(sorted(allowed)))
        paths = {}
        for field, value in supplied.items():
            path = Path(text(value, field, 2000))
            if not path.is_absolute() or not path.is_dir():
                raise AgentError('path_not_found', f'{field}必须是已存在的绝对目录')
            paths[field] = str(path.resolve())
        return {'account_id': digest([self.platform, os.path.normcase(paths['database_dir'])])[:20], 'paths': paths}

    def validate_scope(self, group_id, since, until):
        ReadScope(self.platform, (group_id,), since, until)

    def context(self, root, source):
        return {self.platform: dict(source['paths']), 'attachments': {'extra_allowed_roots': []},
                'policy': {'allow_process_memory_read': False}}

    def attachment_grants(self, message):
        from agent_assets import required_grants
        return required_grants(message)

    def asset_allowed(self, config, path):
        from attachments import allowed_local_source
        return allowed_local_source(path, config)

    def prepare(self, config, messages, output, pid=0):
        from attachments import collect
        return collect({'messages': messages}, config, output, pid, persist=False)

    def require_windows(self):
        if sys.platform != 'win32':
            raise AgentError('unsupported_os', '现有QQ/微信本地读取器只支持Windows；配置和待办管理可独立使用')

    def groups(self, config):
        self.require_windows()
        from groups import qq_groups, wechat_groups
        return (qq_groups if self.platform == 'qq' else wechat_groups)(config)

    def read(self, config, request):
        self.require_windows()
        from messages import read_selected
        return read_selected(config, request)

    def check(self, config):
        """Validate cached first-page keys, not message bodies or entire snapshots."""
        self.require_windows()
        import cipher
        from memory_reader import first_pages, load_secrets
        pages = first_pages(config, self.platform)
        secrets = load_secrets().get(self.platform, {})
        verified, missing = [], []
        for path, page in pages.items():
            try:
                raw = bytes.fromhex(secrets[path])
                if self.platform == 'wechat':
                    keys = cipher.derive_wechat_keys(raw, page)
                elif len(raw) == 32:
                    keys = cipher.derive_qq_raw_keys(raw, page)
                else:
                    keys = cipher.derive_qq_keys(raw, page)
                valid = cipher.verify_page(page, 1, keys)
            except (KeyError, ValueError):
                valid = False
            (verified if valid else missing).append(path)
        if not pages:
            raise AgentError('databases_not_found', '未找到可验证的数据库；请核对账号目录')
        return {'status': 'ready' if not missing else 'authorization_required',
                'verified_database_count': len(verified), 'missing_database_count': len(missing),
                'read_chat_bodies': False,
                'next_actions': [] if not missing else ['source connect --authorize-memory-read --pid <客户端PID>']}

    def connect(self, config, pids, authorized):
        if authorized is not True or not pids or any(type(p) is not int or p <= 0 for p in pids):
            raise AgentError('authorization_required', '取钥需要明确的单次内存读取授权和本人客户端PID')
        self.require_windows()
        from memory_reader import acquire
        config['policy']['allow_process_memory_read'] = True
        report = acquire(config, self.platform, pids)
        if not report['verified_databases']:
            raise AgentError('key_acquisition_failed', '未验证出数据库密钥，请检查登录状态、账号路径、PID与客户端版本')
        return report


def _import_adapter():
    from agent_imports import ImportSource
    return ImportSource()


def _dingtalk_adapter():
    from dingtalk_export import DingTalkSource
    return DingTalkSource()


# Explicit, trusted-code registration only: never load module paths from chat/config.
SOURCE_FACTORIES = {'qq': lambda: LocalSource('qq'), 'wechat': lambda: LocalSource('wechat'),
                    'import': _import_adapter, 'dingtalk': _dingtalk_adapter}


def register_source(platform, factory):
    import re
    if not isinstance(platform, str) or not re.fullmatch(r'[a-z][a-z0-9_]{0,39}', platform) or not callable(factory):
        raise AgentError('invalid_adapter', '适配器名称或工厂无效')
    if platform in SOURCE_FACTORIES:
        raise AgentError('adapter_exists', '适配器已注册，拒绝覆盖')
    SOURCE_FACTORIES[platform] = factory


def get_source(platform):
    factory = SOURCE_FACTORIES.get(platform)
    if factory is None:
        raise AgentError('unsupported_source', '来源未注册，请使用已有适配器或明确添加实现')
    adapter = factory()
    if getattr(adapter, 'platform', None) != platform:
        raise AgentError('invalid_adapter', '适配器platform与注册名称不一致')
    return adapter


def candidates(platform, base=None):
    """Bounded metadata-only probes in familiar locations, never a disk search."""
    if platform == 'dingtalk':
        if base is not None and (not Path(base).is_absolute() or not Path(base).is_dir()):
            raise AgentError('path_not_found', '--base须是已存在的绝对目录')
        roots = [Path(base)] if base else [Path(os.environ[k]) / 'DingTalk' for k in ('APPDATA', 'LOCALAPPDATA') if os.environ.get(k)]
        found = {}
        for root in roots:
            if not root.is_dir():
                continue
            entries = [root] + [p for p in islice(root.iterdir(), 100) if p.name.lower().endswith('_v3')]
            for path in entries:
                if (path / 'DBFiles/dingtalk.db').is_file() and not path.is_symlink():
                    found[str(path.resolve())] = {'paths': {'account_dir': str(path.resolve())}, 'validated': False}
        return {'candidates': list(found.values()), 'bounded_search': True, 'read_chat_bodies': False,
                'notice': '仅目录元数据；请用户明确本人UID，不读取账号日志或密钥'}
    if platform not in PLATFORMS:
        raise AgentError('unsupported_source', '没有该来源的目录候选工具')
    if base is not None:
        base = Path(base)
        if not base.is_absolute() or not base.is_dir():
            raise AgentError('path_not_found', '--base必须是现有绝对目录')
        bases = [base]
    else:
        bases = [Path.home() / 'Documents']
        if os.environ.get('USERPROFILE'):
            bases.append(Path(os.environ['USERPROFILE']) / 'Documents')
        if os.environ.get('APPDATA') and platform == 'qq':
            bases.append(Path(os.environ['APPDATA']) / 'Tencent' / 'QQ')
    patterns = ('Tencent Files/*/nt_qq/nt_db', '*/nt_qq/nt_db', 'nt_qq/nt_db') if platform == 'qq' else (
        'xwechat_files/*/db_storage', 'WeChat Files/*/db_storage', '*/db_storage', 'db_storage')
    found = {}
    for root in dict.fromkeys(bases):
        if not root.is_dir():
            continue
        direct = [root] if root.name in ('nt_db', 'db_storage') else []
        for path in direct:
            found[str(path.resolve())] = path.resolve()
        for pattern in patterns:
            for path in islice(root.glob(pattern), 100):
                if path.is_dir():
                    found[str(path.resolve())] = path.resolve()
    rows = []
    for path in list(found.values())[:100]:
        paths = {'database_dir': str(path)}
        if platform == 'qq':
            paths['media_dir'] = str(path.parent / 'nt_data')
        else:
            paths['account_dir'] = str(path.parent)
        rows.append({'paths': paths, 'validated': False})
    return {'candidates': rows, 'read_chat_bodies': False, 'bounded_search': True,
            'notice': '候选目录不是账号或版本兼容性保证；无结果时请用户提供实际保存位置。'}


def cached_report(export_path, config, request, root):
    """Explicit historical reuse; bind snapshot provenance to this account."""
    from agent_config import private_path
    candidate = Path(export_path)
    if candidate.is_absolute():
        try:
            candidate = candidate.relative_to(root)
        except ValueError:
            raise AgentError('unsafe_path', '历史导出必须位于本项目exports中') from None
    if not candidate.parts or candidate.parts[0] != 'exports':
        raise AgentError('unsafe_path', '历史导出必须位于本项目exports中')
    report = read_json(private_path(root, str(candidate / 'messages.json')), 128 * 1024 * 1024)
    platform = request['scopes'][0]['platform']
    database_root = Path(config[platform]['database_dir']).resolve()
    snapshots = report.get('snapshots', [])
    if not snapshots or any(not Path(s.get('source', '')).is_absolute() or
                            not Path(s['source']).resolve().is_relative_to(database_root) for s in snapshots):
        raise AgentError('cache_account_unverified', '无法确认导出的数据库属于当前来源账号，拒绝复用')
    original = report.get('request', {})
    if original.get('confirmed') is not True:
        raise AgentError('cache_scope_invalid', '历史导出缺少原始授权范围')
    available = [ReadScope(s['platform'], tuple(s['group_ids']), s['since'], s['until']) for s in original.get('scopes', [])]
    wanted = [ReadScope(s['platform'], tuple(s['group_ids']), s['since'], s['until']) for s in request['scopes']]
    for scope in wanted:
        if not any(a.platform == scope.platform and set(scope.group_ids) <= set(a.group_ids)
                   and a.since <= scope.since and scope.until <= a.until for a in available):
            raise AgentError('cache_incomplete', '历史导出不完整覆盖本次范围；不会自动回退读取客户端')
    selected = []
    for message in report['messages']:
        args = message['platform'], message['group_id'], message['timestamp']
        if not any(a.accepts(*args) for a in available):
            raise AgentError('cache_scope_invalid', '历史导出包含超出原授权范围的消息')
        if any(s.accepts(*args) for s in wanted):
            selected.append(message)
    return {'messages': selected, 'snapshots': [], 'parse_errors': [],
            'used_cached_export': True, 'cache_snapshot_until': original.get('until'),
            'cache_notice': '显式复用历史快照，未检查客户端最新消息'}
