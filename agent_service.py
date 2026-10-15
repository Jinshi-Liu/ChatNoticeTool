"""Application facade for the external Agent. No model, network or scheduler."""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from pathlib import Path
import sys
import time

from agent_config import AgentConfig, AgentError, bound, private_path
from agent_processing import ProcessingStore
from agent_sources import SOURCE_FACTORIES, cached_report, get_source
from chatnotice import ROOT, LOCAL_TZ


class AgentService:
    def __init__(self, root=ROOT):
        self.root = Path(root).absolute()
        self.config = AgentConfig(self.root)

    def inspect(self):
        value = self.config.load()
        return {'root': str(self.root), 'python': str(Path(sys.executable).absolute()),
                'initialized': private_path(self.root, 'config.json').is_file(),
                'revision': value['revision'], 'sources': list(value['sources'].values()),
                'groups': list(value['groups'].values()),
                'capabilities': [get_source(p).capabilities for p in SOURCE_FACTORIES],
                'key_cache_present': private_path(self.root, 'state/secrets.dpapi').is_file(),
                'key_cache_verified': False, 'read_chat_bodies': False,
                'next_actions': ['read'] if any(g['enabled'] for g in value['groups'].values()) else ['source configure', 'groups list', 'groups plan']}

    def setup(self):
        from local_setup import initialize
        created = initialize(self.root)
        return {'created': created, **self.inspect()}

    def source_action(self, source_id, action, **kwargs):
        source = self.config.source(source_id)
        backend = get_source(source['platform'])
        config = self.config.client_config(source)
        if action == 'check':
            return backend.check(config)
        if action == 'connect':
            return backend.connect(config, kwargs.get('pids'), kwargs.get('authorized'))
        if action == 'groups':
            report = backend.groups(config)
            search = kwargs.get('contains', '')
            rows = [g for g in report['groups'] if search in g['name'] or search in g.get('remark', '')]
            offset, limit = kwargs.get('offset', 0), kwargs.get('limit', 50)
            if offset < 0 or not 1 <= limit <= 200:
                raise AgentError('invalid_input', 'offset须非负，limit须在1到200之间')
            from agent_config import encode
            page, size = [], 0
            for row in rows[offset:offset + limit]:
                length = len(encode(row).encode('utf-8'))
                if page and size + length > 32 * 1024:
                    break
                page.append(row)
                size += length
            end = offset + len(page)
            return {'source_id': source_id, 'groups': page, 'total': len(rows),
                    'next_offset': end if end < len(rows) else None, 'read_chat_bodies': False}
        raise AgentError('invalid_input', '未知来源操作')

    def read(self, *, group_keys=None, since=None, until=None, batch_id=None, cursor=0, limit=50,
             message=None, offset=0, chars=8000, from_export=None, timings=False, reprocess=False):
        started = time.monotonic()
        if batch_id:
            if group_keys or since is not None or until not in (None, 'now') or from_export or reprocess:
                raise AgentError('invalid_input', '--batch只继续已有快照，不能同时请求新范围或历史重分析')
            store = ProcessingStore(self.root)
            return store.chunk(batch_id, message, offset, chars) if message else store.page(batch_id, cursor, limit)
        if message or cursor or offset:
            raise AgentError('invalid_input', '分页或内容分块必须指定--batch')
        value = self.config.load()
        groups = self.config.authorized(group_keys, value)
        store = ProcessingStore(self.root)
        lower = bound(since, 'since') if since is not None else None
        upper = bound(until, 'until') if until is not None and until != 'now' else None
        if lower is not None and upper is not None and lower >= upper:
            raise AgentError('invalid_input', '开始必须早于结束')
        windows = store.windows(groups, int(time.time()), lower, upper)
        sources = {g['source_id']: self.config.source(g['source_id'], value) for g in groups}
        if lower is None:
            # Successful file imports already enqueue every message durably.
            # Daily reads consume that queue, not years of empty client-style
            # history windows. Explicit --since still supports inbox rescan.
            for window in windows:
                if get_source(sources[window['source_id']]['platform']).capabilities.get('file_import'):
                    group = next(g for g in groups if g['key'] == window['group_key'])
                    target = window['target_until']
                    if upper is None and (group['until'] is None or target < group['until']):
                        target += 1
                    window.update(since=target, until=target, target_until=target,
                                  more_history=False, queue_only=True)
        if from_export and (len(sources) != 1 or next(iter(sources.values()))['platform'] not in ('qq', 'wechat')):
            raise AgentError('invalid_input', '--from-export只用于单个QQ/微信来源；其他文件请用import')
        reports, stats = {}, []
        for source_id, source in sources.items():
            selected = [w for w in windows if w['source_id'] == source_id and w['since'] < w['until']]
            if not selected:
                continue
            # Merge equal windows into one SQL scope; differing windows still
            # share each database snapshot in messages.read_selected.
            by_window = defaultdict(list)
            for window in selected:
                by_window[(window['since'], window['until'])].append(window['group_id'])
            scopes = [{'platform': source['platform'], 'group_ids': ids, 'since': a, 'until': b}
                      for (a, b), ids in by_window.items()]
            request = {'confirmed': True, 'scopes': scopes,
                       'since': datetime.fromtimestamp(min(w['since'] for w in selected), LOCAL_TZ).isoformat(),
                       'until': datetime.fromtimestamp(max(w['until'] for w in selected), LOCAL_TZ).isoformat(),
                       'groups': [{'platform': source['platform'], 'group_id': g['group_id'], 'name': g['name']}
                                  for g in groups if g['source_id'] == source_id and any(w['group_id'] == g['group_id'] for w in selected)]}
            config = self.config.client_config(source)
            clock = time.monotonic()
            reports[source_id] = cached_report(from_export, config, request, self.root) if from_export else get_source(source['platform']).read(config, request)
            stats.append({'source_id': source_id, 'read_seconds': round(time.monotonic() - clock, 4),
                          'snapshot_count': len(reports[source_id].get('snapshots', [])),
                          'selected_rows': len(reports[source_id]['messages']),
                          **reports[source_id].get('timings', {})})
        # Avoid persisting or delivering results under a changed authorization.
        if self.config.load() != value:
            raise AgentError('config_conflict', '读取期间配置发生变化，未交付消息，请重新调用')
        ledger_started = time.monotonic()
        batch_id = store.record_read(groups, sources, windows, reports, historical=bool(from_export), reprocess=reprocess)
        ledger_seconds = time.monotonic() - ledger_started
        result = store.page(batch_id, 0, limit)
        if timings:
            result['timings'] = {'sources': stats, 'dedup_persist_seconds': round(ledger_seconds, 4),
                                 'total_seconds': round(time.monotonic() - started, 4)}
        return result

    def import_file(self, *, group_key, file, format='auto', timestamp=None, confirmed=False, limit=50, timings=False):
        if confirmed is not True:
            raise AgentError('confirmation_required', '请确认允许处理所选文件并添加--confirm；未读取正文')
        if type(limit) is not int or not 1 <= limit <= 200:
            raise AgentError('invalid_input', 'limit须在1到200之间')
        started = time.monotonic()
        value = self.config.load()
        group = self.config.authorized([group_key], value)[0]
        source = self.config.source(group['source_id'], value)
        backend = get_source(source['platform'])
        if not backend.capabilities.get('file_import'):
            raise AgentError('unsupported_operation', '该来源不支持文件导入，请配置import收件箱来源')
        report = backend.ingest(self.config.client_config(source), group, file, format, timestamp)
        if self.config.load() != value:
            raise AgentError('config_conflict', '导入期间授权发生变化，未交付内容；请按当前授权重试')
        messages = report['messages']
        if not messages:
            raise AgentError('invalid_input', '适配器未返回消息')
        now = int(time.time())
        for message in messages:
            stamp = message['timestamp']
            if (message['platform'] != source['platform'] or message['group_id'] != group['group_id']
                    or type(stamp) is not int or not group['since'] <= stamp <= now
                    or (group['until'] is not None and stamp >= group['until'])):
                raise AgentError('out_of_scope', '适配器返回了超出所选分组授权范围的消息')
        lower, upper = min(m['timestamp'] for m in messages), max(m['timestamp'] for m in messages) + 1
        if upper - lower > group['max_days'] * 86400:
            raise AgentError('scope_denied', '导入跨度超出单次授权，请拆分文件')
        window = {'group_key': group['key'], 'source_id': source['id'], 'group_id': group['group_id'],
                  'since': lower, 'until': upper, 'target_until': upper, 'more_history': False}
        ledger = ProcessingStore(self.root)
        # Persist pending analysis immediately, including old timestamps. Do not
        # advance the normal acquisition cursor over unscanned historical gaps.
        batch_id = ledger.record_read([group], {source['id']: source}, [window], {source['id']: report}, advance_progress=False)
        result = ledger.page(batch_id, limit=limit)
        result.update(imported=True, counts=report['counts'], import_notice=report.get('notice', ''), read_client_database=False)
        if timings:
            result['timings'] = {'import_seconds': round(time.monotonic() - started, 4)}
        return result

    def prepare(self, payload, **kwargs):
        from agent_assets import prepare
        return prepare(ProcessingStore(self.root), payload, **kwargs)

    def todo_store(self):
        from todo_store import TodoStore
        return TodoStore(private_path(self.root, 'state/todos.sqlite3'))

    def todos_apply(self, payload, *, timings=False):
        started = time.monotonic()
        if not isinstance(payload, dict):
            raise AgentError('invalid_input', '待办输入必须是对象')
        operations = payload.get('operations')
        if not isinstance(operations, list) or not 1 <= len(operations) <= 200 or any(not isinstance(o, dict) for o in operations):
            raise AgentError('invalid_input', 'operations必须包含1到200个对象')
        batch_id = payload.get('batch_id')
        # Standalone user-requested todo management needs no chat authorization.
        # With a batch, every cited message must have actually been delivered.
        if batch_id:
            if any(not isinstance(o.get('message_keys'), list) or not o['message_keys']
                   or any(not isinstance(k, str) for k in o['message_keys']) for o in operations):
                raise AgentError('invalid_input', '批次内每个待办操作都须引用已交付的message_keys')
            keys = list(dict.fromkeys(k for o in operations for k in o['message_keys']))
            ledger = ProcessingStore(self.root)
            with ledger.connection() as db:
                ledger.records(db, batch_id, keys)
        elif any(o.get('message_keys') for o in operations):
            raise AgentError('batch_required', '引用聊天来源的待办操作必须指定batch_id')
        result = {'results': self.todo_store().apply_batch(operations)}
        if timings:
            result['timings'] = {'todo_write_seconds': round(time.monotonic() - started, 4)}
        return result

    def commit(self, payload, *, timings=False):
        started = time.monotonic()
        store = self.todo_store()
        def exists(task_id):
            try:
                store.get(task_id)
                return True
            except ValueError:
                return False
        result = ProcessingStore(self.root).commit(payload, exists)
        if timings:
            result['timings'] = {'processing_commit_seconds': round(time.monotonic() - started, 4)}
        return result
