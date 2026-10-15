"""Synthetic Agent workflow/performance tests. Never touch real client data."""
from __future__ import annotations

from contextlib import contextmanager
import io
import importlib.util
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import agent_cli
from agent_config import AgentError, encode, private_path
from agent_processing import ProcessingStore, content_hash, message_key
from agent_service import AgentService
from agent_sources import LocalSource
import messages

NOW = 1893758400  # fixed synthetic time


class AgentFixture(unittest.TestCase):
    def setUp(self):
        (ROOT / 'runtime').mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=ROOT / 'runtime')
        self.root = Path(self.temp.name).resolve()
        self.patches = [patch('chatnotice.ROOT', self.root), patch('todo_store.ROOT', self.root)]
        for p in self.patches:
            p.start()
        self.service = AgentService(self.root)
        self.config = self.service.config
        dbdir, media = self.root / 'client' / 'nt_db', self.root / 'client' / 'nt_data'
        dbdir.mkdir(parents=True)
        media.mkdir()
        self.source = self.config.configure_source({'confirmed': True, 'expected_revision': 0, 'platform': 'qq',
                                                   'name': 'Synthetic account', 'paths': {'database_dir': str(dbdir), 'media_dir': str(media)}})['source']
        self.group = self.add_group('123')
        self.data = []
        self.reader = patch.object(LocalSource, 'read', side_effect=self.fake_read)
        self.read_mock = self.reader.start()
        self.clock = patch('agent_service.time.time', return_value=NOW)
        self.clock.start()

    def tearDown(self):
        self.clock.stop()
        self.reader.stop()
        for p in reversed(self.patches):
            p.stop()
        self.temp.cleanup()

    def add_group(self, group_id, **kwargs):
        row = {'source_id': self.source['id'], 'group_id': group_id, 'name': '合成课程群',
               'since': NOW - 3 * 86400, 'max_days': 7, 'instructions': '结合图片和文字整理通知',
               'recipient': '当前测试Agent',
               'grant': {'text_to_agent': True, 'images_to_agent': True, 'documents_to_agent': False}, **kwargs}
        payload = {'confirmed': True, 'expected_revision': self.config.load()['revision'], 'groups': [row]}
        return self.config.apply_groups(payload)['groups'][0]

    def message(self, mid=1, group='123', **kwargs):
        return {'source_id': f'qq:{group}:main:{mid}', 'platform': 'qq', 'group_id': group,
                'group_name': '合成课程群', 'message_id': mid, 'timestamp': NOW - 100,
                'text': '合成通知：提交练习', 'type': 'qq_type_1', 'body_status': 'decoded_known_fields',
                'attachments': [], 'attachment_content_status': 'not_applicable', **kwargs}

    def fake_read(self, config, request):
        def accepts(m):
            return any(s['platform'] == m['platform'] and m['group_id'] in s['group_ids'] and
                       s['since'] <= m['timestamp'] < s['until'] for s in request['scopes'])
        return {'messages': [m for m in self.data if accepts(m)], 'snapshots': [{}], 'parse_errors': []}

    def read(self, **kwargs):
        return self.service.read(**kwargs)

    def commit_payload(self, page, index=0, **kwargs):
        item = page['messages'][index]
        row = {'message_key': item['message_key'], 'content_hash': item['content_hash'],
               'status': 'done', 'text_state': 'analyzed', 'attachment_state': 'not_applicable', 'task_ids': [], **kwargs}
        return {'batch_id': page['batch_id'], 'results': [row]}

    def revoke(self):
        self.config.remove_groups({'confirmed': True, 'expected_revision': self.config.load()['revision'],
                                   'group_keys': [self.group['key']]})


class ConfigTests(AgentFixture):
    def test_inspect_lightweight_no_reader_or_key_check(self):
        with patch.object(LocalSource, 'check') as check:
            result = self.service.inspect()
        check.assert_not_called()
        self.read_mock.assert_not_called()
        self.assertFalse(result['read_chat_bodies'])
        self.assertEqual(result['root'], str(self.root))

    def test_confirmation_and_revision_required(self):
        for payload, code in [({}, 'confirmation_required'), ({'confirmed': True, 'expected_revision': 0}, 'config_conflict')]:
            with self.assertRaises(AgentError) as cm:
                self.config.remove_groups(payload)
            self.assertEqual(cm.exception.code, code)
        self.assertEqual(len(self.config.load()['groups']), 1)

    def test_group_plan_no_content_read_and_exact_ids(self):
        payload = {'expected_revision': self.config.load()['revision'], 'groups': [{**self.group, 'group_id': '456'}]}
        planned = self.config.plan_groups(payload)
        self.assertEqual(planned['changes'][0]['after']['group_id'], '456')
        self.read_mock.assert_not_called()
        payload['groups'][0]['group_id'] = '课程群'
        with self.assertRaises(ValueError):
            self.config.plan_groups(payload)

    def test_source_account_identity_and_no_grant_path_retargeting(self):
        other_media = self.root / 'other-media'
        other_media.mkdir()
        payload = {'confirmed': True, 'expected_revision': self.config.load()['revision'], 'platform': 'qq',
                   'paths': {**self.source['paths'], 'media_dir': str(other_media)}}
        with self.assertRaises(AgentError) as cm:
            self.config.configure_source(payload)
        self.assertEqual(cm.exception.code, 'source_in_use')
        other_db = self.root / 'other-db'
        other_db.mkdir()
        payload['paths']['database_dir'] = str(other_db)
        second = self.config.configure_source(payload)['source']
        self.assertNotEqual(second['account_id'], self.source['account_id'])

    def test_unknown_version_and_database_refused(self):
        data = self.config.load()
        data['schema_version'] = 999
        self.config.path.write_text(encode(data), encoding='utf-8')
        with self.assertRaises(AgentError):
            self.config.load()
        path = private_path(self.root, 'state/processing.sqlite3')
        db = sqlite3.connect(path)
        try:
            db.execute('CREATE TABLE unrelated(x)')
            db.commit()
        finally:
            db.close()
        before = path.read_bytes()
        with self.assertRaises(AgentError):
            ProcessingStore(self.root)
        self.assertEqual(before, path.read_bytes())

    def test_no_authorization_no_client_read(self):
        self.revoke()
        with self.assertRaises(AgentError):
            self.read()
        self.read_mock.assert_not_called()

    def test_connection_requires_explicit_memory_grant(self):
        backend = LocalSource('qq')
        with patch('memory_reader.acquire') as acquire:
            for authorized, pids in [(False, [1]), (True, []), (True, [-1])]:
                with self.assertRaises(AgentError):
                    backend.connect({}, pids, authorized)
            acquire.assert_not_called()

    def test_setup_preserves_existing_private_files(self):
        (self.root / 'config.example.json').write_bytes((ROOT / 'config.example.json').read_bytes())
        (self.root / 'config.json').write_text('{"user":"synthetic"}', encoding='utf-8')
        before = self.config.path.read_bytes()
        first = self.service.setup()
        second = self.service.setup()
        self.assertIn('.chatnotice-owned.json', first['created'])
        self.assertEqual(second['created'], [])
        self.assertEqual(self.config.path.read_bytes(), before)
        self.assertEqual((self.root / 'config.json').read_text(), '{"user":"synthetic"}')

    def test_private_path_rejects_escape_and_symlinks(self):
        for path in ('../outside', str(self.root / 'absolute')):
            with self.assertRaises(AgentError):
                private_path(self.root, path)
        link = self.root / 'linked'
        try:
            link.symlink_to(self.root / 'client', target_is_directory=True)
        except OSError:
            return
        with self.assertRaises(AgentError):
            private_path(self.root, 'linked/file')


class IncrementalTests(AgentFixture):
    def test_fast_path_and_repeat_done_not_returned(self):
        self.data = [self.message()]
        with patch.object(LocalSource, 'check') as check, patch.object(LocalSource, 'groups') as groups:
            page = self.read(timings=True)
            self.service.commit(self.commit_payload(page))
            again = self.read()
        self.assertEqual(page['total'], 1)
        self.assertEqual(again['messages'], [])
        self.assertEqual(self.read_mock.call_count, 2)  # freshness is still checked
        check.assert_not_called()
        groups.assert_not_called()
        self.assertIn('timings', page)

    def test_resuming_old_batch_does_not_repeat_completed_analysis(self):
        self.data = [self.message()]
        page = self.read()
        self.service.commit(self.commit_payload(page))
        resumed = self.read(batch_id=page['batch_id'])
        self.assertEqual(resumed['messages'], [])
        self.assertIsNone(resumed['next_cursor'])
        self.read_mock.assert_called_once()

    def test_changed_content_returned_but_transient_fields_ignored(self):
        self.data = [self.message()]
        page = self.read()
        self.service.commit(self.commit_payload(page))
        self.data[0] = {**self.data[0], 'group_name': '改名', 'time': 'format only', 'output_dir': 'new-batch'}
        self.assertEqual(self.read()['total'], 0)
        self.data[0]['text'] = '更正：延后提交'
        self.assertEqual(self.read()['total'], 1)

    def test_same_group_ids_in_different_accounts_do_not_collide(self):
        other = {**self.source, 'account_id': 'different'}
        self.assertNotEqual(message_key(self.source, self.message()), message_key(other, self.message()))
        a = self.message(platform='wechat', server_id=987, shard='one')
        b = {**a, 'shard': 'two', 'message_id': 999}
        self.assertEqual(message_key(self.source, a), message_key(self.source, b))

    def test_multiple_groups_one_source_call_and_exact_windows(self):
        second = self.add_group('456', since=NOW - 86400)
        self.data = [self.message(), self.message(2, '456')]
        page = self.read()
        self.assertEqual(page['total'], 2)
        self.read_mock.assert_called_once()
        scopes = self.read_mock.call_args.args[1]['scopes']
        self.assertEqual(len(scopes), 2)
        self.assertEqual({s['group_ids'][0] for s in scopes}, {'123', '456'})
        self.assertEqual(next(s['since'] for s in scopes if s['group_ids'] == ['456']), second['since'])

    def test_out_of_scope_rows_fail_without_advancing(self):
        self.read_mock.side_effect = None
        self.read_mock.return_value = {'messages': [self.message(group='999')]}
        with self.assertRaises(AgentError):
            self.read()
        ledger = ProcessingStore(self.root)
        with ledger.connection() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM progress').fetchone()[0], 0)

    def test_pagination_no_additional_database_reads(self):
        self.data = [self.message(mid=i) for i in range(12)]
        page = self.read(limit=5)
        self.assertEqual(len(page['messages']), 5)
        second = self.read(batch_id=page['batch_id'], cursor=page['next_cursor'], limit=5)
        third = self.read(batch_id=page['batch_id'], cursor=second['next_cursor'], limit=5)
        self.assertEqual([len(p['messages']) for p in (page, second, third)], [5, 5, 2])
        self.assertIsNone(third['next_cursor'])
        self.read_mock.assert_called_once()
        self.assertEqual(len({m['message_key'] for p in (page, second, third) for m in p['messages']}), 12)

    def test_large_message_explicit_chunks_not_truncated(self):
        self.data = [self.message(text='图片旁的合成通知。' * 4000)]
        page = self.read()
        item = page['messages'][0]
        self.assertNotIn('message', item)
        self.assertIn('content_ref', item)
        pieces, offset = [], 0
        while offset is not None:
            chunk = self.read(batch_id=page['batch_id'], message=item['message_key'], offset=offset)
            pieces.append(chunk['content'])
            offset = chunk['next_offset']
        self.assertEqual(json.loads(''.join(pieces))['text'], self.data[0]['text'])
        self.read_mock.assert_called_once()

    def test_undelivered_page_cannot_commit(self):
        self.data = [self.message(1), self.message(2)]
        page = self.read(limit=1)
        store = ProcessingStore(self.root)
        with store.connection() as db:
            row = db.execute('SELECT * FROM batch_messages WHERE delivered=0').fetchone()
        payload = self.commit_payload(page)
        payload['results'][0].update(message_key=row['message_key'], content_hash=row['content_hash'])
        with self.assertRaises(AgentError):
            self.service.commit(payload)

    def test_revoke_prevents_old_batch_page_chunk_commit_and_assets(self):
        self.data = [self.message()]
        page = self.read()
        payload = self.commit_payload(page)
        key = page['messages'][0]['message_key']
        self.revoke()
        for action in (lambda: self.read(batch_id=page['batch_id']),
                       lambda: self.read(batch_id=page['batch_id'], message=key),
                       lambda: self.service.commit(payload),
                       lambda: self.service.prepare({'batch_id': page['batch_id'], 'message_keys': [key]})):
            with self.assertRaises(AgentError):
                action()

    def test_rule_change_stales_batch_no_automatic_history_reanalysis(self):
        self.data = [self.message()]
        page = self.read()
        self.service.commit(self.commit_payload(page))
        self.add_group('123', instructions='新规则')
        with self.assertRaises(AgentError):
            self.read(batch_id=page['batch_id'])
        self.assertEqual(self.read()['total'], 0)

    def test_pending_survives_acquisition_cursor_and_reopen(self):
        self.data = [self.message()]
        page = self.read()
        self.service.commit(self.commit_payload(page, status='pending', text_state='pending', reason='需要上下文'))
        self.data = []
        reopened = AgentService(self.root).read()
        self.assertEqual(reopened['total'], 1)
        self.assertEqual(reopened['messages'][0]['reason'], '需要上下文')
        summary = ProcessingStore(self.root).pending()
        self.assertEqual(summary['groups'][0]['pending_count'], 1)
        self.assertEqual(summary['groups'][0]['acquired_until'], NOW)

    def test_old_failure_does_not_undo_concurrent_success(self):
        self.data = [self.message()]
        first, second = self.read(), self.read()
        self.service.commit(self.commit_payload(first))
        self.service.commit(self.commit_payload(second, status='pending', text_state='failed'))
        self.assertEqual(self.read()['total'], 0)

    def test_historical_backfill_must_not_skip_gap(self):
        self.data = []
        self.read(since=NOW - 86400)
        with ProcessingStore(self.root).connection() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM progress').fetchone()[0], 0)
        with self.assertRaises(AgentError):
            self.read(since=NOW - 10 * 86400)

    def test_long_gap_advances_with_one_day_limit(self):
        self.add_group('123', since=NOW - 10 * 86400, max_days=1)
        untils = []
        for _ in range(3):
            self.read()
            with ProcessingStore(self.root).connection() as db:
                untils.append(db.execute('SELECT observed_until FROM progress').fetchone()[0])
        self.assertLess(untils[0], untils[1])
        self.assertLess(untils[1], untils[2])

    def test_processed_neighbor_is_read_only_context_for_new_image(self):
        self.data = [self.message(1, text='下图是本周作业', timestamp=NOW - 200)]
        old = self.read()
        self.service.commit(self.commit_payload(old))
        self.data.append(self.message(2, text='新通知', timestamp=NOW - 100))
        page = self.read()
        self.assertEqual(page['total'], 1)
        self.assertEqual(len(page['context']), 1)
        context = page['context'][0]
        self.assertTrue(context['read_only_context'])
        self.assertEqual(context['message']['text'], '下图是本周作业')
        chunk = self.read(batch_id=page['batch_id'], message=context['message_key'])
        self.assertEqual(json.loads(chunk['content'])['text'], '下图是本周作业')
        payload = self.commit_payload(page)
        payload['results'][0]['message_key'] = context['message_key']
        with self.assertRaises(AgentError):
            self.service.commit(payload)

    def test_explicit_reprocess_keeps_task_links(self):
        self.data = [self.message()]
        page = self.read()
        store = self.service.todo_store()
        store.upsert('合成事项', id='existing')
        self.service.commit(self.commit_payload(page, task_ids=['existing']))
        self.assertEqual(self.read()['total'], 0)
        reprocess = self.read(since=NOW - 300, until=NOW, reprocess=True)
        self.assertEqual(reprocess['total'], 1)
        self.assertEqual(reprocess['messages'][0]['task_ids'], ['existing'])

    def test_unknown_body_stays_pending(self):
        self.data = [self.message(text='', body_status='nontext_or_unknown')]
        page = self.read()
        with self.assertRaises(AgentError):
            self.service.commit(self.commit_payload(page))
        self.assertEqual(self.read()['total'], 1)

    def test_cli_workflow_uses_one_read_and_compact_json(self):
        self.data = [self.message(i, text='合成正文' * 200) for i in range(100)]
        output = io.StringIO()
        with patch('sys.stdout', output):
            code = agent_cli.main(['read', '--limit', '200'], service=self.service)
        self.assertEqual(code, 0)
        result = json.loads(output.getvalue())
        self.assertEqual(result['data']['total'], 100)
        self.assertIsNotNone(result['data']['next_cursor'])
        self.assertLess(len(output.getvalue().encode('utf-8')), 48 * 1024)
        self.read_mock.assert_called_once()

    def test_ten_thousand_done_only_hundred_new_delivered(self):
        store = ProcessingStore(self.root)
        with store.connection() as db:
            for i in range(10000):
                m = self.message(i)
                version = content_hash(m)
                db.execute('INSERT INTO messages(message_key,group_key,timestamp,content_hash,payload,processed_hash,attachment_state) VALUES(?,?,?,?,?,?,?)',
                           (message_key(self.source, m), self.group['key'], m['timestamp'], version, encode(m), version, 'not_applicable'))
        self.data = [self.message(i) for i in range(10100)]
        page = self.read(limit=200)
        self.assertEqual(page['total'], 100)
        items = []
        while True:
            items.extend(page['messages'])
            if page['next_cursor'] is None:
                break
            page = self.read(batch_id=page['batch_id'], cursor=page['next_cursor'], limit=200)
        self.assertEqual(len(items), 100)
        self.assertTrue(all(i['message']['message_id'] >= 10000 for i in items))


class CacheTests(AgentFixture):
    def exported(self, *, snapshots=True):
        output = self.root / 'exports' / 'synthetic'
        output.mkdir(parents=True, exist_ok=True)
        report = {'request': {'confirmed': True, 'until': NOW, 'scopes': [
            {'platform': 'qq', 'group_ids': ['123', '999'], 'since': NOW - 86400, 'until': NOW}]},
            'messages': [self.message(1), self.message(2, '999')],
            'snapshots': [{'source': str(Path(self.source['paths']['database_dir']) / 'nt_msg.db')}] if snapshots else []}
        (output / 'messages.json').write_text(encode(report), encoding='utf-8')
        return output, report

    def test_explicit_cache_scoped_no_fresh_fallback_or_cursor_advance(self):
        output, _ = self.exported()
        page = self.read(since=NOW - 86400, until=NOW, from_export=str(output))
        self.assertTrue(page['used_cached_export'])
        self.assertEqual(page['total'], 1)
        self.assertEqual(page['messages'][0]['message']['group_id'], '123')
        self.read_mock.assert_not_called()
        with ProcessingStore(self.root).connection() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM progress').fetchone()[0], 0)

    def test_cache_provenance_and_range_fail_no_automatic_client_read(self):
        output, report = self.exported(snapshots=False)
        for since in (NOW - 86400, NOW - 2 * 86400):
            with self.assertRaises(AgentError):
                self.read(since=since, until=NOW, from_export=str(output))
        self.read_mock.assert_not_called()

    def test_cache_unrequested_or_tampered_rows_not_delivered(self):
        output, report = self.exported()
        report['messages'].append(self.message(3, '777'))
        (output / 'messages.json').write_text(encode(report), encoding='utf-8')
        with self.assertRaises(AgentError):
            self.read(since=NOW - 86400, until=NOW, from_export=str(output))
        self.read_mock.assert_not_called()


class TodoAgentTests(AgentFixture):
    def test_batch_idempotence_and_partial_conflict(self):
        op = {'operation_id': 'create-1', 'action': 'create', 'fields': {'title': '合成作业'}}
        first = self.service.todos_apply({'operations': [op]})['results'][0]
        replay = self.service.todos_apply({'operations': [op]})['results'][0]
        self.assertTrue(replay['replayed'])
        self.assertEqual(first['id'], replay['id'])
        bad = {**op, 'fields': {'title': '不同内容'}}
        good = {'operation_id': 'create-2', 'action': 'create', 'fields': {'title': '第二项'}}
        results = self.service.todos_apply({'operations': [bad, good]})['results']
        self.assertFalse(results[0]['ok'])
        self.assertTrue(results[1]['ok'])
        self.assertEqual(len(self.service.todo_store().tasks()), 2)

    def test_patch_preserves_completion_and_user_edit(self):
        store = self.service.todo_store()
        store.upsert('原内容', id='task')
        store.mark('task', True)
        op = {'operation_id': 'patch-1', 'action': 'patch', 'id': 'task',
              'fields': {'title': 'AI更新'}, 'baseline': {'title': '原内容'}}
        self.assertTrue(store.apply_batch([op])[0]['ok'])
        self.assertTrue(store.get('task')['done'])
        store.patch_fields('task', {'title': '用户编辑'}, {'title': 'AI更新'})
        replay = store.apply_batch([op])[0]
        self.assertTrue(replay['replayed'])
        conflict = {**op, 'operation_id': 'patch-2'}
        self.assertFalse(store.apply_batch([conflict])[0]['ok'])
        self.assertEqual(store.get('task')['title'], '用户编辑')

    def test_removed_not_recreated(self):
        store = self.service.todo_store()
        store.upsert('旧事项', id='removed')
        store.remove('removed')
        results = store.apply_batch([{'operation_id': 'new', 'action': 'create', 'id': 'removed', 'fields': {'title': '重复'}}])
        self.assertFalse(results[0]['ok'])
        self.assertEqual(store.get('removed')['removed'], 1)

    def test_invalid_item_does_not_abort_valid_items(self):
        results = self.service.todos_apply({'operations': [
            {'operation_id': 'bad-date', 'action': 'create', 'fields': {'title': 'bad', 'due': 123}},
            {'operation_id': 'good-date', 'action': 'create', 'fields': {'title': 'good', 'due': None}}]})['results']
        self.assertFalse(results[0]['ok'])
        self.assertTrue(results[1]['ok'])

    def test_todo_listing_byte_budget_has_explicit_continuation(self):
        store = self.service.todo_store()
        for i in range(10):
            store.upsert('合成事项', id=str(i), note='合成正文' * 1000)
        page = store.page(limit=50)
        self.assertLess(len(page['tasks']), 10)
        self.assertIsNotNone(page['next_offset'])
        self.assertEqual(page['total'], 10)

    def test_write_then_crash_before_processing_commit_recovers(self):
        self.data = [self.message()]
        page = self.read()
        key = page['messages'][0]['message_key']
        operation = {'operation_id': key + ':task:0', 'action': 'create', 'message_keys': [key], 'fields': {'title': '作业'}}
        payload = {'batch_id': page['batch_id'], 'operations': [operation]}
        created = self.service.todos_apply(payload)['results'][0]
        replay = AgentService(self.root).todos_apply(payload)['results'][0]
        self.assertTrue(replay['replayed'])
        self.assertEqual(created['id'], replay['id'])
        self.service.commit(self.commit_payload(page, task_ids=[created['id']]))
        self.assertEqual(self.read()['total'], 0)
        self.assertEqual(len(self.service.todo_store().tasks()), 1)


class AssetTests(AgentFixture):
    def setup_image(self):
        source = Path(self.source['paths']['media_dir']) / 'notice.png'
        source.write_bytes(b'synthetic-image-v1')
        self.data = [self.message(attachments=[{'filename': 'notice.png', 'local_path': str(source)}],
                                  attachment_content_status='not_processed')]
        return source

    def fake_collect(self, report, config, output, pid, **kwargs):
        local = output / 'image.png'
        source = Path(self.source['paths']['media_dir']) / 'notice.png'
        local.write_bytes(source.read_bytes())
        return {'items': [{'source_id': m['source_id'], 'status': 'copied_local_file',
                           'source_path': str(source), 'local_path': str(local), 'filename': 'notice.png'} for m in report['messages']]}

    def test_lazy_preparation_and_cache_no_reconversion(self):
        self.setup_image()
        with patch('attachments.collect', side_effect=self.fake_collect) as collect:
            page = self.read()
            collect.assert_not_called()
            args = {'batch_id': page['batch_id'], 'message_keys': [page['messages'][0]['message_key']]}
            prepared = self.service.prepare(args)
            again = self.service.prepare(args)
        self.assertEqual(collect.call_count, 1)
        self.assertTrue(again['results'][0]['cached'])
        self.assertEqual(prepared['results'][0]['status'], 'prepared')
        with self.assertRaises(AgentError):
            self.service.commit(self.commit_payload(page))
        self.service.commit(self.commit_payload(page, attachment_state='analyzed'))
        self.assertEqual(self.read()['total'], 0)

    def test_cache_changed_file_invalidates(self):
        source = self.setup_image()
        page = self.read()
        args = {'batch_id': page['batch_id'], 'message_keys': [page['messages'][0]['message_key']]}
        with patch('attachments.collect', side_effect=self.fake_collect) as collect:
            self.service.prepare(args)
            source.write_bytes(b'changed-synthetic-image-with-new-size')
            self.assertFalse(self.service.prepare(args)['results'][0]['cached'])
        self.assertEqual(collect.call_count, 2)

    def test_failed_cache_retry_clears_previous_prepared_flag(self):
        source = self.setup_image()
        page = self.read()
        args = {'batch_id': page['batch_id'], 'message_keys': [page['messages'][0]['message_key']]}
        with patch('attachments.collect', side_effect=self.fake_collect):
            self.service.prepare(args)
        source.write_bytes(b'changed')
        with patch('attachments.collect', side_effect=ValueError('synthetic parser failure')):
            result = self.service.prepare(args, timings=True)
        self.assertEqual(result['results'][0]['status'], 'pending')
        self.assertIn('prepare_seconds', result['timings'])
        with self.assertRaises(AgentError):
            self.service.commit(self.commit_payload(page, attachment_state='analyzed'))

    def test_cache_missing_output_reprepared_and_revoked_cache_blocked(self):
        self.setup_image()
        page = self.read()
        args = {'batch_id': page['batch_id'], 'message_keys': [page['messages'][0]['message_key']]}
        with patch('attachments.collect', side_effect=self.fake_collect) as collect:
            prepared = self.service.prepare(args)
            Path(prepared['results'][0]['items'][0]['local_path']).unlink()
            self.assertFalse(self.service.prepare(args)['results'][0]['cached'])
            self.revoke()
            with self.assertRaises(AgentError):
                self.service.prepare(args)
        self.assertEqual(collect.call_count, 2)

    def test_no_image_grant_no_file_read(self):
        self.setup_image()
        self.add_group('123', grant={'text_to_agent': True, 'images_to_agent': False, 'documents_to_agent': False})
        page = self.read()
        with patch('attachments.collect') as collect:
            result = self.service.prepare({'batch_id': page['batch_id'], 'message_keys': [page['messages'][0]['message_key']]})
        self.assertEqual(result['results'][0]['status'], 'scope_denied')
        collect.assert_not_called()

    def test_unread_image_cannot_mark_done_and_skip_requires_reason(self):
        self.setup_image()
        page = self.read()
        for fields in ({'attachment_state': 'analyzed'}, {'attachment_state': 'skipped'}, {'status': 'skipped'}):
            with self.assertRaises(AgentError):
                self.service.commit(self.commit_payload(page, **fields))
        self.service.commit(self.commit_payload(page, attachment_state='skipped', reason='用户明确不处理此图片'))
        self.assertEqual(self.read()['total'], 0)


class SnapshotAndCliTests(unittest.TestCase):
    def test_ten_groups_differing_windows_one_snapshot(self):
        request = {'confirmed': True, 'scopes': [{'platform': 'qq', 'group_ids': [str(i)], 'since': i, 'until': 100} for i in range(1, 11)],
                   'groups': [{'platform': 'qq', 'group_id': str(i), 'name': str(i)} for i in range(1, 11)]}
        @contextmanager
        def opened(*args):
            yield object(), {'synthetic': True}
        with patch.object(messages, 'open_database', side_effect=opened) as snapshot, patch.object(messages, 'selected_qq', return_value=[]) as select:
            result = messages.read_selected({}, request)
        snapshot.assert_called_once()
        self.assertEqual(select.call_count, 10)
        self.assertEqual(result['messages'], [])
        self.assertNotIn('_raw', result)

    def test_wechat_two_shards_many_groups_two_snapshots(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'message').mkdir()
            for name in ('message_0.db', 'message_1.db'):
                (root / 'message' / name).touch()
            request = {'confirmed': True,
                       'scopes': [{'platform': 'wechat', 'group_ids': [str(i) + '@chatroom'], 'since': i, 'until': 100} for i in range(10)],
                       'groups': [{'platform': 'wechat', 'group_id': str(i) + '@chatroom', 'name': str(i)} for i in range(10)]}
            @contextmanager
            def opened(*args):
                yield object(), {'synthetic': True}
            with patch.object(messages, 'open_database', side_effect=opened) as snapshot, patch.object(messages, 'selected_wechat', return_value=[]):
                messages.read_selected({'wechat': {'database_dir': str(root)}}, request)
            self.assertEqual(snapshot.call_count, 2)

    def test_project_rules_and_locator_remember_without_account_data(self):
        spec = importlib.util.spec_from_file_location('chatnotice_locator', ROOT / 'agent_locator.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        with tempfile.TemporaryDirectory(prefix='component locator 中文 ') as directory:
            component = Path(directory)
            for name in ('agent_cli.py', 'todo_store.py', 'config.example.json'):
                (component / name).touch()
            first = module.locate(str(component), sys.executable, True)
            second = module.locate(str(component))
            self.assertEqual(first['root'], second['root'])
            self.assertEqual(first['python'], second['python'])
            saved = json.loads((component / 'state/component-location.json').read_text(encoding='utf-8'))
            self.assertEqual(set(saved), {'schema_version', 'root', 'python'})
            # A malformed private record is refused, never treated as a grant.
            (component / 'state/component-location.json').write_text('[]', encoding='utf-8')
            with self.assertRaises(ValueError):
                module.locate(str(component))
        main = (ROOT / 'AGENTS.md').read_text(encoding='utf-8')
        self.assertTrue(main.startswith('# ChatNoticeTool Agent'))
        self.assertLess(len(main.encode('utf-8')), 6000)
        self.assertIn('agent_locator.py', main)
        self.assertNotIn('/skill:', main)
        self.assertIn('新增可复用工具', main)
        self.assertFalse((ROOT / 'skills/chatnotice/SKILL.md').exists())
        for ref in ('workflow.md', 'configuration.md', 'commands.md', 'troubleshooting.md', 'extending.md'):
            self.assertTrue((ROOT / 'docs/agent' / ref).is_file())

    def test_project_rule_document_links_exist(self):
        import re
        for document in [ROOT / 'AGENTS.md', *(ROOT / 'docs/agent').glob('*.md')]:
            for target in re.findall(r'\[[^\]]+\]\(([^)]+)\)', document.read_text(encoding='utf-8')):
                if not target.startswith(('http:', 'https:', '#')):
                    self.assertTrue((document.parent / target).is_file(), f'{document}: {target}')

    def test_locator_from_unrelated_cwd_reads_only_explicit_project_location(self):
        with tempfile.TemporaryDirectory(prefix='locator target 中文 ') as directory, tempfile.TemporaryDirectory() as cwd:
            component = Path(directory)
            for name in ('agent_cli.py', 'todo_store.py', 'config.example.json'):
                (component / name).touch()
            result = subprocess.run([sys.executable, '-B', '-X', 'utf8', str(ROOT / 'agent_locator.py'), '--root', str(component)],
                                    cwd=cwd, capture_output=True, text=True, encoding='utf-8', timeout=20)
            self.assertEqual(result.returncode, 0, result.stderr)
            data = json.loads(result.stdout)['data']
            self.assertEqual(data['root'], str(component))
            self.assertFalse(data['read_chat_bodies'])
            self.assertFalse((component / 'state').exists())

    def test_cleanup_owns_all_new_public_files(self):
        import cleanup
        self.assertTrue({'agent_cli.py', 'agent_config.py', 'agent_service.py', 'agent_sources.py',
                         'agent_processing.py', 'agent_assets.py', 'agent_locator.py', 'AGENTS.md', 'docs'} <= cleanup.OWNED)
        self.assertIn('/skills/chatnotice/.local/', (ROOT / '.gitignore').read_text())

    def test_cli_errors_are_json_without_echoing_private_input(self):
        for args in (['unknown-command'], ['read', '--limit', 'not-an-integer']):
            out = io.StringIO()
            with patch('sys.stdout', out):
                code = agent_cli.main(args)
            value = json.loads(out.getvalue())
            self.assertEqual(code, 2)
            self.assertFalse(value['ok'])
            self.assertEqual(value['schema_version'], 1)

    def test_help_from_other_working_directory_with_spaces(self):
        with tempfile.TemporaryDirectory(prefix='agent help 中文 ') as directory:
            result = subprocess.run([sys.executable, '-B', '-X', 'utf8', str(ROOT / 'agent_cli.py'), '--help'],
                                    cwd=directory, capture_output=True, text=True, encoding='utf-8', timeout=20)
        self.assertEqual(result.returncode, 0)
        self.assertIn('read', result.stdout)
        self.assertEqual(result.stderr, '')


if __name__ == '__main__':
    unittest.main()
