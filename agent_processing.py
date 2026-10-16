"""Scoped incremental ledger: acquisition is durable before analysis, not success."""
from __future__ import annotations

from contextlib import contextmanager
import json
from pathlib import Path
import sqlite3
import time
import uuid

from agent_config import AgentConfig, AgentError, digest, encode, private_path, text
from chatnotice import ROOT
from wechat_content import compact_message, describe

APP_ID = 0x434E4131
OVERLAP = 24 * 60 * 60
PAGE_BYTES = 32 * 1024
INLINE_BYTES = 12000
MAX_TASK_LINKS = 100


def has_media(message):
    info = describe(message)
    if info is not None:
        return info['media']
    return bool(message.get('attachments') or message.get('local_media_hashes') or
                message.get('attachment_content_status', 'not_applicable') != 'not_applicable')


def content_hash(message):
    # Names and formatting/diagnostic timestamps are not message-content versions.
    return digest({k: v for k, v in message.items() if k not in {
        'group_name', 'time', 'batch', 'output_dir', 'elapsed_seconds', 'parse_errors'}})


def message_key(source, message):
    server = message.get('server_id')
    identity = ['server', str(server)] if server and str(server) != '0' else [
        'local', message.get('shard', 'main'), str(message['message_id'])]
    return digest([source['platform'], source['account_id'], message['group_id'], identity])


class ProcessingStore:
    def __init__(self, root=ROOT):
        self.root = Path(root).absolute()
        self.config = AgentConfig(self.root)
        self.path = private_path(self.root, 'state/processing.sqlite3')
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as db:
            app = db.execute('PRAGMA application_id').fetchone()[0]
            objects = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type IN ('table','index')")}
            if app not in (0, APP_ID) or (app == 0 and objects):
                raise AgentError('unknown_database', '该数据库不属于Agent处理状态，拒绝修改')
            version = db.execute('PRAGMA user_version').fetchone()[0]
            if version > 1:
                raise AgentError('state_version', '处理状态版本较新，拒绝降级写入')
            expected = {'messages', 'messages_group_time', 'progress', 'batches', 'batch_messages',
                        'batch_context', 'links', 'preparations', 'assets'}
            if app == APP_ID and version == 1 and expected <= objects:
                return  # Verify identity/version/schema without rewriting headers.
            db.execute(f'PRAGMA application_id={APP_ID}')
            db.execute('PRAGMA user_version=1')
            db.executescript('''
                CREATE TABLE IF NOT EXISTS messages (
                    message_key TEXT PRIMARY KEY, group_key TEXT NOT NULL, timestamp INTEGER NOT NULL,
                    content_hash TEXT NOT NULL, payload TEXT NOT NULL, processed_hash TEXT,
                    text_state TEXT NOT NULL DEFAULT 'pending', attachment_state TEXT NOT NULL,
                    analysis_state TEXT NOT NULL DEFAULT 'pending', reason TEXT NOT NULL DEFAULT '');
                CREATE INDEX IF NOT EXISTS messages_group_time ON messages(group_key,timestamp);
                CREATE TABLE IF NOT EXISTS progress (group_key TEXT PRIMARY KEY, observed_until INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS batches (
                    batch_id TEXT PRIMARY KEY, created INTEGER NOT NULL, rules TEXT NOT NULL,
                    sources TEXT NOT NULL, windows TEXT NOT NULL, historical INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS batch_messages (
                    batch_id TEXT NOT NULL, ordinal INTEGER NOT NULL, message_key TEXT NOT NULL,
                    content_hash TEXT NOT NULL, payload TEXT NOT NULL, delivered INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY(batch_id,message_key), UNIQUE(batch_id,ordinal));
                CREATE TABLE IF NOT EXISTS batch_context (
                    batch_id TEXT NOT NULL, message_key TEXT NOT NULL, content_hash TEXT NOT NULL,
                    payload TEXT NOT NULL, PRIMARY KEY(batch_id,message_key));
                CREATE TABLE IF NOT EXISTS links (
                    message_key TEXT NOT NULL, task_id TEXT NOT NULL, PRIMARY KEY(message_key,task_id));
                CREATE TABLE IF NOT EXISTS preparations (
                    batch_id TEXT NOT NULL, message_key TEXT NOT NULL, prepared INTEGER NOT NULL,
                    manifest TEXT NOT NULL, PRIMARY KEY(batch_id,message_key));
                CREATE TABLE IF NOT EXISTS assets (
                    cache_key TEXT PRIMARY KEY, group_key TEXT NOT NULL, fingerprint TEXT NOT NULL,
                    manifest TEXT NOT NULL);
            ''')

    @contextmanager
    def connection(self):
        path = private_path(self.root, 'state/processing.sqlite3')
        db = sqlite3.connect(path, timeout=5)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA foreign_keys=ON')
        try:
            with db:
                yield db
        finally:
            db.close()

    def windows(self, groups, now, since=None, until=None):
        result = []
        now = int(now)
        end = now if until is None else min(until, now)
        with self.connection() as db:
            for group in groups:
                observed = db.execute('SELECT observed_until FROM progress WHERE group_key=?', (group['key'],)).fetchone()
                start = max(group['since'], observed[0] - OVERLAP) if observed else group['since']
                if since is not None:
                    if since < group['since']:
                        raise AgentError('scope_denied', '回补起点早于已授权历史起点')
                    start = since
                upper = min(end, group['until']) if group['until'] is not None else end
                if until is not None and group['until'] is not None and until > group['until']:
                    raise AgentError('scope_denied', '结束时刻超出授权范围')
                initial_recent = observed is None and since is None and start < upper - group['max_days'] * 86400
                if initial_recent:
                    # An authorization floor is not a request to scan decades.
                    # Explicit --since retains oldest-first historical backfill.
                    start = upper - group['max_days'] * 86400
                # When catching up a long gap, do not spend the entire allowed
                # window rereading overlap: reserve room for forward progress.
                if observed and since is None:
                    overlap = min(OVERLAP, group['max_days'] * 86400 // 2)
                    start = max(group['since'], observed[0] - overlap)
                stop = min(upper, start + group['max_days'] * 86400)
                result.append({'group_key': group['key'], 'source_id': group['source_id'],
                               'group_id': group['group_id'], 'since': start, 'until': stop,
                               'target_until': upper, 'more_history': stop < upper,
                               **({'history_available_before': start} if initial_recent else {})})
        return result

    def record_read(self, groups, sources, windows, reports, historical=False, reprocess=False, advance_progress=True):
        """All reports must succeed before acquisition progress is committed."""
        batch_id = uuid.uuid4().hex
        rules = {g['key']: g for g in groups}
        with self.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            for source_id, report in reports.items():
                source = sources[source_id]
                choices = [w for w in windows if w['source_id'] == source_id and w['until'] > w['since']]
                for message in report['messages']:
                    window = next((w for w in choices if w['group_id'] == message['group_id'] and
                                   w['since'] <= message['timestamp'] < w['until']), None)
                    if window is None or message['platform'] != source['platform']:
                        raise AgentError('out_of_scope', '来源返回了超出授权范围的消息')
                    key = message_key(source, message)
                    version = content_hash(message)
                    if reprocess:
                        db.execute("UPDATE messages SET processed_hash=NULL,analysis_state='pending',reason='' WHERE message_key=?", (key,))
                    db.execute('''INSERT INTO messages(message_key,group_key,timestamp,content_hash,payload,attachment_state)
                                  VALUES(?,?,?,?,?,?) ON CONFLICT(message_key) DO UPDATE SET
                                  timestamp=excluded.timestamp,payload=excluded.payload,
                                  attachment_state=CASE WHEN messages.content_hash!=excluded.content_hash THEN excluded.attachment_state ELSE messages.attachment_state END,
                                  text_state=CASE WHEN messages.content_hash!=excluded.content_hash THEN 'pending' ELSE messages.text_state END,
                                  analysis_state=CASE WHEN messages.content_hash!=excluded.content_hash THEN 'pending' ELSE messages.analysis_state END,
                                  content_hash=excluded.content_hash''',
                               (key, window['group_key'], message['timestamp'], version, encode(message),
                                'pending' if has_media(message) else 'not_applicable'))
            if not historical and advance_progress:
                for window in windows:
                    if window['until'] <= window['since']:
                        continue
                    previous = db.execute('SELECT observed_until FROM progress WHERE group_key=?', (window['group_key'],)).fetchone()
                    covered = previous[0] if previous else rules[window['group_key']]['since']
                    # Explicit backfill must not jump an unacquired gap.
                    if (window['since'] <= covered < window['until'] or
                            (previous is None and 'history_available_before' in window)):
                        db.execute('INSERT INTO progress VALUES(?,?) ON CONFLICT(group_key) DO UPDATE SET observed_until=excluded.observed_until',
                                   (window['group_key'], window['until']))
            db.execute('INSERT INTO batches VALUES(?,?,?,?,?,?)',
                       (batch_id, int(time.time()), encode(rules), encode(sources), encode(windows), int(historical)))
            ordinal = 0
            for group in groups:
                upper = next(w['target_until'] for w in windows if w['group_key'] == group['key'])
                rows = db.execute('''SELECT * FROM messages WHERE group_key=? AND timestamp>=? AND timestamp<?
                                     AND (processed_hash IS NULL OR processed_hash!=content_hash)
                                     ORDER BY timestamp,message_key''', (group['key'], group['since'], upper))
                for row in rows:
                    db.execute('INSERT INTO batch_messages(batch_id,ordinal,message_key,content_hash,payload) VALUES(?,?,?,?,?)',
                               (batch_id, ordinal, row['message_key'], row['content_hash'], row['payload']))
                    ordinal += 1
        return batch_id

    def batch(self, db, batch_id):
        row = db.execute('SELECT * FROM batches WHERE batch_id=?', (batch_id,)).fetchone()
        if row is None:
            raise AgentError('batch_not_found', '批次不存在')
        row = dict(row)
        row['rules'] = json.loads(row['rules'])
        row['sources'] = json.loads(row['sources'])
        row['windows'] = json.loads(row['windows'])
        current = self.config.load()
        self.config.authorized(list(row['rules']), current)
        for key, old in row['rules'].items():
            if current['groups'][key] != old:
                raise AgentError('stale_batch', '群配置已变化，请重新read；旧批次不能继续访问')
        for key, old in row['sources'].items():
            if self.config.source(key, current) != old:
                raise AgentError('stale_batch', '来源配置已变化，请重新read')
        return row

    def records(self, db, batch_id, keys, *, delivered=True):
        if (not isinstance(keys, list) or not 1 <= len(keys) <= 200
                or any(not isinstance(k, str) or len(k) > 100 for k in keys) or len(set(keys)) != len(keys)):
            raise AgentError('invalid_input', 'message_keys必须包含1到200个不重复ID')
        batch = self.batch(db, batch_id)
        records = []
        for key in keys:
            row = db.execute('''SELECT b.*,m.group_key,m.content_hash AS current_hash,m.attachment_state,m.text_state,m.processed_hash
                                FROM batch_messages b JOIN messages m USING(message_key)
                                WHERE b.batch_id=? AND b.message_key=?''', (batch_id, key)).fetchone()
            if row is None or (delivered and not row['delivered']):
                raise AgentError('message_not_delivered', '消息未通过本批次分页返回，拒绝访问或提交')
            if row['content_hash'] != row['current_hash']:
                raise AgentError('message_changed', '消息内容已变化，请重新读取')
            record = dict(row)
            record['message'] = json.loads(record.pop('payload'))
            records.append(record)
        return batch, records

    def resource_message(self, db, batch, record):
        """Resolve image quotes only from already acquired, same-group scope."""
        message = record['message']
        info = describe(message)
        if not info or info.get('kind') != 'quote' or info.get('media_kind') != 'image':
            return message
        rule = batch['rules'][record['group_key']]
        source = batch['sources'][rule['source_id']]
        identity = info['reference']['server_id']
        if identity and identity != '0':
            key = message_key(source, {'server_id': identity, 'group_id': message['group_id']})
            upper = min(message['timestamp'], (rule['until'] - 1) if rule['until'] is not None else message['timestamp'])
            parent = db.execute('SELECT payload FROM messages WHERE message_key=? AND group_key=? AND timestamp>=? AND timestamp<=?',
                                (key, record['group_key'], rule['since'], upper)).fetchone()
            if parent:
                image = json.loads(parent['payload'])
                if image.get('type') == 'image':
                    return {**image, 'source_id': message['source_id']}
        if info.get('resource_text'):
            return {**message, 'type': 'image', 'text': info['resource_text']}
        return message

    @staticmethod
    def view(message, batch_id, key):
        value = compact_message(message)
        if value is not message:
            value = {**value, 'raw_ref': {'batch_id': batch_id, 'message_key': key,
                                         'use': 'read --batch B --message KEY --raw'}}
        if has_media(message):
            value = {**value, 'attachment_ref': {'batch_id': batch_id, 'message_key': key}}
        return value

    def page(self, batch_id, cursor=0, limit=50):
        if type(cursor) is not int or cursor < 0 or type(limit) is not int or not 1 <= limit <= 200:
            raise AgentError('invalid_input', 'cursor须非负，limit须在1到200之间')
        with self.connection() as db:
            batch = self.batch(db, batch_id)
            total = db.execute('SELECT COUNT(*) FROM batch_messages WHERE batch_id=?', (batch_id,)).fetchone()[0]
            rows = db.execute('''SELECT b.*,m.group_key,m.content_hash AS current_hash,m.processed_hash,m.reason,
                                 m.text_state,m.attachment_state,m.analysis_state
                                 FROM batch_messages b JOIN messages m USING(message_key)
                                 WHERE batch_id=? AND ordinal>=? ORDER BY ordinal LIMIT ?''', (batch_id, cursor, limit))
            items, size, next_cursor, page_rules = [], 0, cursor, {}
            for row in rows:
                if row['processed_hash'] == row['content_hash']:
                    next_cursor = row['ordinal'] + 1
                    continue
                message = json.loads(row['payload'])
                item = {'message_key': row['message_key'], 'content_hash': row['content_hash'],
                        'group_key': row['group_key'], 'text_state': row['text_state'],
                        'attachment_state': row['attachment_state'] if has_media(message) else 'not_applicable', 'analysis_state': row['analysis_state'],
                        'reason': row['reason'], 'changed_since_batch': row['current_hash'] != row['content_hash'],
                        'message': self.view(message, batch_id, row['message_key'])}
                linked = db.execute('SELECT task_id FROM links WHERE message_key=?', (row['message_key'],)).fetchall()
                item['task_ids'] = [r[0] for r in linked]
                length = len(encode(item).encode('utf-8'))
                if length > INLINE_BYTES:
                    item.pop('message')
                    item['content_ref'] = {'batch_id': batch_id, 'message_key': row['message_key'],
                                           'format': 'json', 'use': 'read --batch B --message KEY --offset 0'}
                    item['timestamp'] = message['timestamp']
                    length = len(encode(item).encode('utf-8'))
                rule = batch['rules'][row['group_key']]
                rule_size = len(encode(rule).encode('utf-8')) if row['group_key'] not in page_rules else 0
                if items and size + length + rule_size > PAGE_BYTES:
                    break
                items.append(item)
                page_rules[row['group_key']] = rule
                size += length + rule_size
                next_cursor = row['ordinal'] + 1
                db.execute('UPDATE batch_messages SET delivered=1 WHERE batch_id=? AND message_key=?', (batch_id, row['message_key']))
            # A little already-processed text can explain a newly arrived image.
            # It is read-only context, not a request to redo successful work.
            context = []
            page_keys = {item['message_key'] for item in items}
            for group_key in page_rules:
                first = next(item for item in items if item['group_key'] == group_key)
                stamp = first.get('timestamp', first.get('message', {}).get('timestamp'))
                rule = batch['rules'][group_key]
                neighbors = db.execute('''SELECT * FROM messages WHERE group_key=? AND timestamp>=? AND timestamp<=?
                                          AND processed_hash=content_hash ORDER BY timestamp DESC,message_key LIMIT 2''',
                                       (group_key, max(rule['since'], stamp - OVERLAP), stamp))
                for neighbor in neighbors:
                    if neighbor['message_key'] in page_keys:
                        continue
                    value = json.loads(neighbor['payload'])
                    context_item = {'message_key': neighbor['message_key'], 'group_key': group_key,
                                    'read_only_context': True, 'message': self.view(value, batch_id, neighbor['message_key'])}
                    length = len(encode(context_item).encode('utf-8'))
                    if length > 4000:
                        context_item.pop('message')
                        context_item['content_ref'] = {'batch_id': batch_id, 'message_key': neighbor['message_key'], 'format': 'json'}
                        length = len(encode(context_item).encode('utf-8'))
                    if size + length > PAGE_BYTES:
                        break
                    db.execute('INSERT OR IGNORE INTO batch_context VALUES(?,?,?,?)',
                               (batch_id, neighbor['message_key'], neighbor['content_hash'], neighbor['payload']))
                    context.append(context_item)
                    size += length
            result = {'batch_id': batch_id, 'messages': items, 'context': context, 'total': total,
                      'next_cursor': next_cursor if next_cursor < total else None,
                      'remaining': max(0, total - next_cursor), 'rules': list(page_rules.values()),
                      'read_windows': batch['windows'], 'used_cached_export': bool(batch['historical']),
                      'more_history': any(w['more_history'] for w in batch['windows']),
                      'all_attachment_contents_processed': False,
                      'next_actions': ['processing commit'] if items else []}
            if any('history_available_before' in w for w in batch['windows']):
                result['history_notice'] = '首次默认读取最近一个max_days窗口；较早授权历史尚未扫描，可用显式--since回补，不代表不存在旧消息'
            if batch['historical']:
                result['cache_notice'] = '历史快照，不是最新同步；本批次未推进客户端获取游标'
            return result

    def chunk(self, batch_id, key, offset=0, chars=8000, *, raw=False):
        if type(offset) is not int or offset < 0 or type(chars) is not int or not 1 <= chars <= 10000:
            raise AgentError('invalid_input', 'offset须非负，chars须在1到10000之间')
        with self.connection() as db:
            self.batch(db, batch_id)
            context = db.execute('''SELECT c.*,m.content_hash AS current_hash FROM batch_context c
                                    JOIN messages m USING(message_key) WHERE c.batch_id=? AND c.message_key=?''', (batch_id, key)).fetchone()
            if context:
                if context['content_hash'] != context['current_hash']:
                    raise AgentError('message_changed', '上下文已变化，请重新读取')
                message = json.loads(context['payload'])
            else:
                _, records = self.records(db, batch_id, [key])
                message = records[0]['message']
            value = encode(message if raw else self.view(message, batch_id, key))
            chunk = value[offset:offset + chars]
            return {'message_key': key, 'format': 'json', 'content': chunk, 'total_chars': len(value),
                    'next_offset': offset + len(chunk) if offset + len(chunk) < len(value) else None}

    def mark_prepared(self, batch_id, key, manifest, prepared):
        self.mark_prepared_many(batch_id, [(key, manifest, prepared)])

    def mark_prepared_many(self, batch_id, rows):
        if not rows:
            return
        with self.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            self.records(db, batch_id, [key for key, _, _ in rows])
            db.executemany('INSERT INTO preparations VALUES(?,?,?,?) ON CONFLICT(batch_id,message_key) DO UPDATE SET prepared=excluded.prepared,manifest=excluded.manifest',
                           [(batch_id, key, int(prepared), encode(manifest)) for key, manifest, prepared in rows])
            db.executemany('UPDATE messages SET attachment_state=? WHERE message_key=?',
                           [('prepared' if prepared else 'pending', key) for key, _, prepared in rows])

    def commit(self, payload, task_exists, *, validate_only=False):
        if not isinstance(payload, dict):
            raise AgentError('invalid_input', '提交必须是对象')
        results = payload.get('results')
        if not isinstance(results, list) or not 1 <= len(results) <= 200 or any(not isinstance(r, dict) for r in results):
            raise AgentError('invalid_input', 'results必须包含1到200项')
        batch_id = text(payload.get('batch_id'), 'batch_id', 100)
        # Scope this memo to one validation/commit, not to the service lifetime:
        # final commit must check existence again after the todo transaction.
        checked_tasks = {}
        def exists(task_id):
            if task_id not in checked_tasks:
                checked_tasks[task_id] = task_exists(task_id)
            return checked_tasks[task_id]
        with self.connection() as db:
            db.execute('BEGIN' if validate_only else 'BEGIN IMMEDIATE')
            _, records = self.records(db, batch_id, [r.get('message_key') for r in results])
            for result, row in zip(results, records):
                if result.get('content_hash') != row['content_hash']:
                    raise AgentError('message_changed', '请提交本批次返回的content_hash')
                status = result.get('status')
                ts, ats = result.get('text_state'), result.get('attachment_state')
                reason = text(result.get('reason', ''), 'reason', 2000, empty=True)
                if status not in ('done', 'pending', 'skipped') or ts not in ('analyzed', 'pending', 'failed', 'skipped') or ats not in ('analyzed', 'not_applicable', 'pending', 'failed', 'skipped'):
                    raise AgentError('invalid_input', '无效的处理阶段状态')
                media = has_media(row['message'])
                if media and ats == 'not_applicable':
                    raise AgentError('attachment_pending', '消息含媒体，不能当作不含附件')
                if ats == 'analyzed':
                    prepared = db.execute('SELECT prepared FROM preparations WHERE batch_id=? AND message_key=?', (batch_id, row['message_key'])).fetchone()
                    if media and (not prepared or not prepared[0]):
                        raise AgentError('attachment_pending', '本地附件尚未成功准备，不能标记已分析')
                body = row['message'].get('body_status', '')
                if ts == 'analyzed' and (body.endswith('pending') or body.endswith('missing')
                                         or (body == 'nontext_or_unknown' and not media)):
                    raise AgentError('body_pending', '正文解析未完成，不能标记正文已分析')
                if (status == 'skipped' or ts == 'skipped' or ats == 'skipped') and not reason.strip():
                    raise AgentError('reason_required', '跳过必须明确记录原因')
                if status == 'done' and (ts not in ('analyzed', 'skipped') or ats not in ('analyzed', 'not_applicable', 'skipped')):
                    raise AgentError('processing_incomplete', '未完成项目必须保留pending，不能推进成功状态')
                # An old failed attempt must not undo a successful concurrent
                # analysis of the identical content version.
                if row['processed_hash'] == row['content_hash'] and status == 'pending':
                    continue
                task_ids = result.get('task_ids', [])
                if not isinstance(task_ids, list) or len(task_ids) > MAX_TASK_LINKS or any(not isinstance(t, str) or not exists(t) for t in task_ids):
                    raise AgentError('task_not_found', '关联待办必须已成功写入')
                if validate_only:
                    continue
                for task_id in task_ids:
                    db.execute('INSERT OR IGNORE INTO links VALUES(?,?)', (row['message_key'], task_id))
                db.execute('''UPDATE messages SET processed_hash=?,text_state=?,attachment_state=?,analysis_state=?,reason=?
                              WHERE message_key=?''',
                           (row['content_hash'] if status in ('done', 'skipped') else None, ts, ats, status, reason, row['message_key']))
        return {'committed': len(results), 'batch_id': batch_id,
                'notice': '已登记逐条状态；pending会继续返回，skipped是明确跳过而不是已读懂附件'}

    def pending(self, keys=None, limit=50):
        groups = self.config.authorized(keys)
        result = []
        with self.connection() as db:
            for group in groups:
                upper = group['until'] if group['until'] is not None else int(time.time())
                row = db.execute('''SELECT COUNT(*) AS total,MIN(timestamp) AS oldest FROM messages WHERE group_key=?
                                    AND timestamp>=? AND timestamp<? AND (processed_hash IS NULL OR processed_hash!=content_hash)''',
                                 (group['key'], group['since'], upper)).fetchone()
                observed = db.execute('SELECT observed_until FROM progress WHERE group_key=?', (group['key'],)).fetchone()
                result.append({'group_key': group['key'], 'pending_count': row['total'],
                               'oldest_pending_timestamp': row['oldest'],
                               'acquired_until': observed[0] if observed else None})
        return {'groups': result, 'notice': 'acquired_until只是已持久接收，不代表已分析完成；用read获取待处理内容'}
