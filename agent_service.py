"""Application facade for the external Agent. No model, network or scheduler."""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from pathlib import Path
import sys
import time

from agent_config import AgentConfig, AgentError, bound, private_path
from agent_processing import MAX_TASK_LINKS, ProcessingStore, has_media
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
             message=None, offset=0, chars=8000, from_export=None, timings=False, reprocess=False,
             prepare_images=True, raw=False, todos=False, ids=None, include_removed=False,
             file=None, format='auto', timestamp=None, confirmed=False):
        started = time.monotonic()
        if file is not None:
            if (not isinstance(group_keys, list) or len(group_keys) != 1 or todos or ids is not None or include_removed
                    or batch_id or since is not None or until not in (None, 'now') or cursor or message or offset or from_export or reprocess or raw):
                raise AgentError('invalid_input', '文件读取须选择一个已授权收件箱分组，不能混用聊天范围、批次或待办参数')
            return self.import_file(group_key=group_keys[0], file=file, format=format, timestamp=timestamp,
                                    confirmed=confirmed, limit=limit, timings=timings, prepare_images=prepare_images)
        if format != 'auto' or timestamp is not None or confirmed:
            raise AgentError('invalid_input', '--format/--timestamp/--confirm只用于明确选择的--file')
        if todos or ids is not None:
            if group_keys or since is not None or until not in (None, 'now') or batch_id or message or offset or from_export or reprocess or raw:
                raise AgentError('invalid_input', '待办读取不能混用聊天范围、批次或原文参数')
            result = self.todo_store().page(cursor, limit, include_removed, ids)
            result['next_cursor'] = result.pop('next_offset')
            if timings:
                result['timings'] = {'total_seconds': round(time.monotonic() - started, 4)}
            return result
        if include_removed:
            raise AgentError('invalid_input', '--all仅用于待办读取')
        if raw and not (batch_id and message):
            raise AgentError('invalid_input', '--raw仅用于已交付消息的按需原文分块读取')
        if batch_id:
            if group_keys or since is not None or until not in (None, 'now') or from_export or reprocess:
                raise AgentError('invalid_input', '--batch只继续已有快照，不能同时请求新范围或历史重分析')
            store = ProcessingStore(self.root)
            if message:
                return store.chunk(batch_id, message, offset, chars, raw=raw)
            return self._finish_page(store.page(batch_id, cursor, limit), prepare_images, timings, started, store)
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
                    window.pop('history_available_before', None)
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
        return self._finish_page(result, prepare_images, timings, started, store)

    def _finish_page(self, result, prepare_images, timings, started, store):
        if prepare_images:
            from agent_assets import prepare
            keys = [item['message_key'] for item in result['messages']
                    if 'content_ref' in item or item.get('message', {}).get('has_media') or has_media(item.get('message', {}))]
            report = prepare(store, {'batch_id': result['batch_id'], 'message_keys': keys},
                             images_only=True, timings=timings) if keys else {'results': []}
            result['attachments'] = report['results']
            for item in result['messages']:
                prepared = next((r for r in report['results'] if r['message_key'] == item['message_key']), None)
                if prepared and prepared['status'] == 'prepared':
                    item['attachment_state'] = 'prepared'
            result['attachment_notice'] = '仅准备本页已授权图片；Agent看图后write，未准备/无法理解的内容保留pending'
            if timings:
                result.setdefault('timings', {})['attachment_prepare'] = report.get('timings', {'prepare_seconds': 0, 'cache_hits': 0, 'preparation_attempts': 0})
        result['next_actions'] = ['write --input -'] if result['messages'] else []
        if timings:
            result.setdefault('timings', {})['total_seconds'] = round(time.monotonic() - started, 4)
        return result

    def import_file(self, *, group_key, file, format='auto', timestamp=None, confirmed=False, limit=50, timings=False, prepare_images=False):
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
        return self._finish_page(result, prepare_images, timings, started, ledger)

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

    def write(self, payload, *, timings=False):
        """One Agent call, two durable databases; replay, never fake atomicity."""
        started = time.monotonic()
        if not isinstance(payload, dict) or set(payload) - {'batch_id', 'operations', 'results'}:
            raise AgentError('invalid_input', 'write只接受batch_id、operations和results')
        operations, results = payload.get('operations', []), payload.get('results')
        if not payload.get('batch_id') and 'results' not in payload:
            applied = self.todos_apply({'operations': operations}, timings=timings)
            return {'operations': applied['results'], 'has_failures': any(not r['ok'] for r in applied['results']),
                    **({'timings': applied['timings']} if timings else {})}
        if (not isinstance(operations, list) or len(operations) > 200 or any(not isinstance(o, dict) for o in operations)
                or not isinstance(results, list) or not 1 <= len(results) <= 200 or any(not isinstance(r, dict) for r in results)):
            raise AgentError('invalid_input', 'operations最多200项，results须有1到200个对象')
        batch_id = payload.get('batch_id')
        keys = [r.get('message_key') for r in results]
        ids = [o.get('operation_id') for o in operations]
        if any(not isinstance(i, str) or not i or len(i) > 200 for i in ids) or len(set(ids)) != len(ids):
            raise AgentError('invalid_input', 'operation_id须为不重复的稳定字符串')
        for op in operations:
            refs = op.get('message_keys')
            if not isinstance(refs, list) or not refs or any(not isinstance(k, str) or k not in keys for k in refs):
                raise AgentError('invalid_input', '每个待办操作须关联本次results中的已交付消息')
        for row in results:
            if set(row) - {'message_key', 'content_hash', 'status', 'reason', 'text_state', 'attachment_state', 'task_ids', 'operation_ids'}:
                raise AgentError('invalid_input', 'results含未知字段，未写入数据')
            dependencies = row.get('operation_ids', [])
            if (not isinstance(dependencies, list) or any(not isinstance(i, str) or i not in ids for i in dependencies)
                    or any(row.get('message_key') not in operations[ids.index(i)]['message_keys'] for i in dependencies)):
                raise AgentError('invalid_input', 'operation_ids只能引用与此消息关联的本次操作；不填也会推导所有依赖')
        todos, ledger = self.todo_store(), ProcessingStore(self.root)
        def exists(task_id):
            try:
                todos.get(task_id)
                return True
            except ValueError:
                return False
        with ledger.connection() as db:
            _, records = ledger.records(db, batch_id, keys)
        normalized = []
        for row, record in zip(results, records):
            status = row.get('status')
            state = 'analyzed' if status == 'done' else 'skipped' if status == 'skipped' else 'pending'
            normalized.append({'text_state': state,
                               'attachment_state': state if has_media(record['message']) else 'not_applicable', **row})
        results = normalized
        commit_payload = {'batch_id': batch_id, 'results': results}
        # Reject invalid/stale content, fake analysis, missing existing task IDs
        # before any todo write. New task IDs are linked from successful ops.
        ledger.commit(commit_payload, exists, validate_only=True)
        # Automatic links must fit the same bound as explicit task_ids, before
        # any todo write; otherwise a successful bulk create cannot be committed.
        from todo_store import operation_task_id
        targets = defaultdict(set)
        for op in operations:
            try:
                target = operation_task_id(op)
            except ValueError:
                continue  # Keep invalid operations as ordinary per-item failures.
            for key in op['message_keys']:
                targets[key].add(target)
        if any(len(set(row.get('task_ids', [])) | targets[row['message_key']]) > MAX_TASK_LINKS for row in results):
            raise AgentError('too_many_task_links', f'单条消息本次提交最多关联{MAX_TASK_LINKS}个候选待办ID；请拆分operations后重试，尚未写入待办')
        clock = time.monotonic()
        if operations:
            # Recheck authorization immediately before the todo transaction,
            # without reopening/reinitializing both stores through todos_apply.
            with ledger.connection() as db:
                ledger.records(db, batch_id, list(dict.fromkeys(k for op in operations for k in op['message_keys'])))
            applied = todos.apply_batch(operations)
        else:
            applied = []
        write_seconds = time.monotonic() - clock
        prepared, summaries = [], []
        for row in results:
            linked = [(op, result) for op, result in zip(operations, applied) if row['message_key'] in op['message_keys']]
            failed = [op['operation_id'] for op, result in linked if not result['ok']]
            task_ids = list(dict.fromkeys(row.get('task_ids', []) + [result['id'] for _, result in linked if result['ok']]))
            item = {**row, 'task_ids': task_ids}
            if failed:
                item.update(status='pending', reason='todo_operations_failed')
            prepared.append(item)
            summaries.append({'message_key': row['message_key'], 'status': item['status'],
                              'task_ids': task_ids, 'failed_operation_ids': failed})
        clock = time.monotonic()
        committed = ledger.commit({'batch_id': batch_id, 'results': prepared}, exists)
        result = {'batch_id': batch_id, 'operations': applied, 'results': summaries, 'processing': committed,
                  'has_failures': any(not r['ok'] for r in applied),
                  'notice': '失败操作对应消息保持pending；中断重试须复用相同operation_id和参数，不是跨库原子事务'}
        if timings:
            result['timings'] = {'todo_write_seconds': round(write_seconds, 4),
                                 'processing_commit_seconds': round(time.monotonic() - clock, 4),
                                 'total_seconds': round(time.monotonic() - started, 4)}
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
