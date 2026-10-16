"""Synthetic light read/write workflow, XML projection, and discovery work counts."""
from __future__ import annotations

import hashlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
from xml.sax.saxutils import escape

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'tests')]
from test_agent import AgentFixture, NOW
import agent_cli
from agent_config import AgentError, encode
from agent_processing import ProcessingStore
from attachments import collect
from PIL import Image


def png():
    stream = io.BytesIO()
    Image.new('RGB', (2, 2), 'red').save(stream, format='PNG')
    return stream.getvalue()


class FastWorkflowTests(AgentFixture):
    def setup_image(self):
        self.image = Path(self.source['paths']['media_dir']) / 'notice.png'
        self.image.write_bytes(png())
        self.data = [self.message(attachments=[{'filename': 'notice.png', 'local_path': str(self.image)}],
                                  attachment_content_status='not_processed')]

    def fake_collect(self, report, config, output, pid, **kwargs):
        items = []
        for i, message in enumerate(report['messages']):
            local = output / f'{i}.png'
            local.write_bytes(self.image.read_bytes())
            items.append({'source_id': message['source_id'], 'filename': 'notice.png',
                          'status': 'copied_local_file', 'source_path': str(self.image), 'local_path': str(local)})
        return {'items': items}

    def payload(self, page):
        return {'batch_id': page['batch_id'], 'results': [
            {'message_key': m['message_key'], 'content_hash': m['content_hash'], 'status': 'done'}
            for m in page['messages']]}

    def operation(self, page, index=0, opid='synthetic:create', **kwargs):
        return {'operation_id': opid, 'action': 'create', 'fields': {'title': '合成通知'},
                'message_keys': [page['messages'][index]['message_key']], **kwargs}

    def test_default_read_prepares_images_and_one_write_finishes(self):
        self.setup_image()
        with patch('attachments.collect', side_effect=self.fake_collect) as prepare:
            page = self.service.read(timings=True)
        prepare.assert_called_once()
        self.assertEqual(page['attachments'][0]['status'], 'prepared')
        self.assertTrue(Path(page['attachments'][0]['items'][0]['local_path']).is_file())
        self.assertEqual(page['next_actions'], ['write --input -'])
        payload = self.payload(page)
        payload['operations'] = [self.operation(page)]
        written = self.service.write(payload, timings=True)
        self.assertFalse(written['has_failures'])
        self.assertEqual(written['results'][0]['status'], 'done')
        self.assertEqual(len(written['results'][0]['task_ids']), 1)
        self.assertIn('attachment_prepare', page['timings'])
        self.assertIn('processing_commit_seconds', written['timings'])
        self.assertEqual(self.service.read()['messages'], [])

    def test_read_initializes_processing_store_only_once(self):
        self.setup_image()
        original = ProcessingStore.__init__
        calls = []
        def initialize(store, *args, **kwargs):
            calls.append(store)
            original(store, *args, **kwargs)
        with patch.object(ProcessingStore, '__init__', initialize), patch('attachments.collect', side_effect=self.fake_collect):
            self.service.read()
        self.assertEqual(len(calls), 1)

    def test_existing_processing_store_initialization_is_read_only(self):
        from contextlib import contextmanager
        ProcessingStore(self.root)
        connection = ProcessingStore.connection
        statements = []
        @contextmanager
        def traced(store):
            with connection(store) as db:
                db.set_trace_callback(statements.append)
                yield db
        with patch.object(ProcessingStore, 'connection', traced):
            ProcessingStore(self.root)
        self.assertFalse(any('CREATE ' in sql or ('PRAGMA ' in sql and '=' in sql and 'foreign_keys' not in sql)
                             for sql in statements), statements)

    def test_existing_todo_store_initialization_is_read_only(self):
        from contextlib import contextmanager
        from todo_store import TodoStore
        self.service.todo_store()
        connection = TodoStore.connection
        statements = []
        @contextmanager
        def traced(store):
            with connection(store) as db:
                db.set_trace_callback(statements.append)
                yield db
        with patch.object(TodoStore, 'connection', traced):
            self.service.todo_store()
        self.assertFalse(any('CREATE ' in sql or ('PRAGMA ' in sql and '=' in sql) for sql in statements), statements)

    def test_missing_processing_table_is_initialized_without_version_change(self):
        store = ProcessingStore(self.root)
        with store.connection() as db:
            db.execute('DROP TABLE assets')
        repaired = ProcessingStore(self.root)
        with repaired.connection() as db:
            self.assertIsNotNone(db.execute("SELECT name FROM sqlite_master WHERE name='assets'").fetchone())
            self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0], 1)

    def test_write_reuses_validated_store_instances(self):
        from todo_store import TodoStore
        self.data = [self.message()]
        page = self.service.read()
        payload = self.payload(page)
        payload['operations'] = [self.operation(page)]
        processing_init, todo_init = ProcessingStore.__init__, TodoStore.__init__
        counts = {'processing': 0, 'todo': 0}
        def initialize_processing(store, *args, **kwargs):
            counts['processing'] += 1
            processing_init(store, *args, **kwargs)
        def initialize_todo(store, *args, **kwargs):
            counts['todo'] += 1
            todo_init(store, *args, **kwargs)
        with patch.object(ProcessingStore, '__init__', initialize_processing), patch.object(TodoStore, '__init__', initialize_todo):
            result = self.service.write(payload)
        self.assertFalse(result['has_failures'])
        self.assertEqual(counts, {'processing': 1, 'todo': 1})

    def test_shared_task_checked_once_per_commit_not_once_per_message(self):
        from todo_store import TodoStore
        self.data = [self.message(i) for i in range(1, 13)]
        page = self.service.read()
        payload = self.payload(page)
        payload['operations'] = [self.operation(page, message_keys=[r['message_key'] for r in page['messages']])]
        original = TodoStore.get
        checked = []
        def get(store, task_id):
            checked.append(task_id)
            return original(store, task_id)
        with patch.object(TodoStore, 'get', get):
            result = self.service.write(payload)
        self.assertFalse(result['has_failures'])
        self.assertEqual(len(checked), 1)

    def test_only_current_page_prepared_no_refetch_on_next_page(self):
        self.setup_image()
        self.data += [{**self.data[0], 'message_id': 2, 'source_id': 'qq:123:main:2'}]
        with patch('attachments.collect', side_effect=self.fake_collect) as prepare:
            first = self.service.read(limit=1)
            self.assertEqual(len(prepare.call_args.args[0]['messages']), 1)
            second = self.service.read(batch_id=first['batch_id'], cursor=first['next_cursor'], limit=1)
        self.assertEqual(len(first['attachments']), 1)
        self.assertEqual(len(second['attachments']), 1)
        self.assertEqual(prepare.call_count, 2)
        self.read_mock.assert_called_once()

    def test_read_reuses_valid_image_cache(self):
        self.setup_image()
        with patch('attachments.collect', side_effect=self.fake_collect) as prepare:
            self.service.read()
            second = self.service.read()
        prepare.assert_called_once()
        self.assertTrue(second['attachments'][0]['cached'])

    def test_default_read_no_grant_does_not_open_image(self):
        self.setup_image()
        self.add_group('123', grant={'text_to_agent': True, 'images_to_agent': False, 'documents_to_agent': False})
        with patch('attachments.collect') as prepare:
            page = self.service.read()
        prepare.assert_not_called()
        self.assertEqual(page['attachments'][0]['status'], 'scope_denied')
        with self.assertRaises(AgentError):
            self.service.write(self.payload(page))

    def test_no_prepare_flag_preserves_explicit_legacy_path(self):
        self.setup_image()
        with patch('attachments.collect') as prepare:
            self.service.read(prepare_images=False)
        prepare.assert_not_called()

    def test_document_is_not_automatically_prepared(self):
        self.data = [self.message(attachments=[{'filename': 'notice.pdf'}], attachment_content_status='not_processed')]
        with patch('attachments.collect') as prepare:
            page = self.service.read()
        prepare.assert_not_called()
        self.assertEqual(page['attachments'][0]['status'], 'not_requested')

    def test_per_operation_failure_blocks_only_dependent_message(self):
        self.data = [self.message(1), self.message(2)]
        page = self.service.read()
        payload = self.payload(page)
        payload['operations'] = [self.operation(page, 0, opid='good'),
                                 self.operation(page, 1, opid='bad', fields={'title': ''})]
        result = self.service.write(payload)
        self.assertTrue(result['has_failures'])
        self.assertEqual([r['status'] for r in result['results']], ['done', 'pending'])
        pending = self.service.read()
        self.assertEqual(pending['total'], 1)
        self.assertEqual(pending['messages'][0]['message_key'], page['messages'][1]['message_key'])
        self.assertEqual(len(self.service.todo_store().tasks()), 1)

    def test_success_links_preserved_even_when_another_dependency_fails(self):
        self.data = [self.message()]
        page = self.service.read()
        payload = self.payload(page)
        payload['operations'] = [self.operation(page, opid='good'), self.operation(page, opid='bad', fields={'title': ''})]
        result = self.service.write(payload)
        pending = self.service.read()['messages'][0]
        self.assertEqual(pending['task_ids'], [result['operations'][0]['id']])
        self.assertEqual(pending['analysis_state'], 'pending')

    def test_explicit_dependency_subset_cannot_hide_failed_operation(self):
        self.data = [self.message()]
        page = self.service.read()
        payload = self.payload(page)
        payload['results'][0]['operation_ids'] = ['good']
        payload['operations'] = [self.operation(page, opid='good'), self.operation(page, opid='bad', fields={'title': ''})]
        self.assertEqual(self.service.write(payload)['results'][0]['status'], 'pending')

    def test_write_replay_after_interruption_does_not_duplicate(self):
        self.data = [self.message()]
        page = self.service.read()
        payload = self.payload(page)
        payload['operations'] = [self.operation(page)]
        real = ProcessingStore.commit
        def interrupted(store, data, exists, **kwargs):
            if kwargs.get('validate_only'):
                return real(store, data, exists, **kwargs)
            raise RuntimeError('synthetic crash after todo transaction')
        with patch.object(ProcessingStore, 'commit', interrupted):
            with self.assertRaises(RuntimeError):
                self.service.write(payload)
        result = self.service.write(payload)
        self.assertTrue(result['operations'][0]['replayed'])
        self.assertEqual(len(self.service.todo_store().tasks()), 1)
        self.assertEqual(self.service.read()['messages'], [])

    def test_too_many_automatic_links_rejected_before_any_todo_write(self):
        self.data = [self.message()]
        page = self.service.read()
        payload = self.payload(page)
        payload['operations'] = [self.operation(page, opid=f'many:{i}') for i in range(101)]
        with self.assertRaises(AgentError) as raised:
            self.service.write(payload)
        self.assertEqual(raised.exception.code, 'too_many_task_links')
        self.assertEqual(self.service.todo_store().tasks(), [])
        self.assertEqual(self.service.read()['total'], 1)

    def test_many_operations_on_one_task_do_not_overcount_automatic_links(self):
        task_id = self.service.todo_store().upsert('原标题', id='one-target')
        self.data = [self.message()]
        page = self.service.read()
        payload = self.payload(page)
        payload['operations'] = [self.operation(page, opid=f'patch:{i}', action='patch', id=task_id,
                                                fields={'title': '新标题'}, baseline={'title': '原标题'}) for i in range(101)]
        result = self.service.write(payload)
        self.assertFalse(result['has_failures'])
        self.assertEqual(result['results'][0]['task_ids'], [task_id])
        self.assertEqual(len(self.service.todo_store().tasks()), 1)

    def test_bad_content_version_prevents_any_todo_write(self):
        self.data = [self.message()]
        page = self.service.read()
        payload = self.payload(page)
        payload['results'][0]['content_hash'] = 'wrong'
        payload['operations'] = [self.operation(page)]
        with self.assertRaises(AgentError):
            self.service.write(payload)
        self.assertEqual(self.service.todo_store().tasks(), [])

    def test_revocation_prevents_any_todo_write(self):
        self.data = [self.message()]
        page = self.service.read()
        payload = self.payload(page)
        payload['operations'] = [self.operation(page)]
        self.revoke()
        with self.assertRaises(AgentError):
            self.service.write(payload)
        self.assertEqual(self.service.todo_store().tasks(), [])

    def test_cross_message_operations_without_result_are_rejected(self):
        self.data = [self.message()]
        page = self.service.read()
        payload = self.payload(page)
        payload['operations'] = [self.operation(page, message_keys=['unseen'])]
        with self.assertRaises(AgentError):
            self.service.write(payload)

    def test_write_only_todos_without_message_processing(self):
        result = self.service.write({'operations': [{'operation_id': 'manual', 'action': 'create', 'fields': {'title': '独立待办'}}]})
        self.assertTrue(result['operations'][0]['ok'])
        self.assertNotIn('processing', result)

    def test_misspelled_write_fields_cannot_silently_mark_done(self):
        self.data = [self.message()]
        page = self.service.read()
        payload = self.payload(page)
        payload['todos'] = [self.operation(page)]
        with self.assertRaises(AgentError):
            self.service.write(payload)
        self.assertEqual(self.service.read()['total'], 1)
        self.assertEqual(self.service.todo_store().tasks(), [])

    def test_cli_read_write_stdin_needs_no_request_file(self):
        self.data = [self.message()]
        read_output = io.StringIO()
        with patch('sys.stdout', read_output):
            self.assertEqual(agent_cli.main(['read'], service=self.service), 0)
        page = json.loads(read_output.getvalue())['data']
        output = io.StringIO()
        with patch('sys.stdin', io.StringIO(encode(self.payload(page)))), patch('sys.stdout', output):
            self.assertEqual(agent_cli.main(['write', '--input', '-'], service=self.service), 0)
        self.assertFalse(json.loads(output.getvalue())['data']['has_failures'])
        self.assertFalse((self.root / 'state/agent-inputs').exists())


class WeChatViewsTests(AgentFixture):
    payload = FastWorkflowTests.payload
    fake_collect = FastWorkflowTests.fake_collect

    def setUp(self):
        AgentFixture.setUp(self)
        directory, account = self.root / 'wx-db', self.root / 'wx-account'
        directory.mkdir()
        account.mkdir()
        self.source = self.config.configure_source({'confirmed': True, 'expected_revision': self.config.load()['revision'],
                                                   'platform': 'wechat', 'paths': {'database_dir': str(directory), 'account_dir': str(account)}})['source']
        self.group = self.add_group('synthetic@chatroom')
        self.image = account / 'fixture.png'
        self.image.write_bytes(png())

    def wx(self, mid, text, typ='file_link_or_card', **kwargs):
        return self.message(mid, group=self.group['group_id'], platform='wechat', type=typ, text=text,
                            source_id=f'wechat:synthetic:{mid}', server_id=mid, attachment_content_status='not_processed', **kwargs)

    def quote(self, title='新通知', reference_type='1', server='123', content='被引用的文字'):
        return f'<msg><appmsg><type>57</type><title>{escape(title)}</title><refermsg><type>{reference_type}</type><svrid>{server}</svrid><content>{escape(content)}</content></refermsg></appmsg></msg>'

    def test_plain_quote_is_not_a_pending_attachment(self):
        self.data = [self.wx(1, self.quote(content='引用正文' * 5000))]
        with patch('attachments.collect') as prepare:
            page = self.service.read()
        prepare.assert_not_called()
        item = page['messages'][0]
        self.assertEqual(item['message']['text'], '新通知')
        self.assertEqual(item['message']['type'], 'quote')
        self.assertEqual(item['attachment_state'], 'not_applicable')
        self.assertIn('raw_ref', item['message'])
        self.assertLess(len(encode(page)), 5000)
        self.service.write(self.payload(page))
        self.assertEqual(self.service.read()['messages'], [])

    def test_image_xml_metadata_hidden_but_raw_is_available(self):
        xml = '<msg><img md5="' + 'a' * 32 + '" cdnmidimgurl="https://example.invalid/' + 'metadata' * 3000 + '" width="20" height="30"/></msg>'
        self.data = [self.wx(1, xml, 'image')]
        page = self.service.read(prepare_images=False)
        message = page['messages'][0]['message']
        self.assertNotIn('cdnmidimgurl', encode(page))
        self.assertEqual(message['dimensions'], {'width': 20, 'height': 30})
        self.assertIn('attachment_ref', message)
        default = self.service.read(batch_id=page['batch_id'], message=page['messages'][0]['message_key'])
        self.assertNotIn('cdnmidimgurl', default['content'])
        raw = self.service.read(batch_id=page['batch_id'], message=page['messages'][0]['message_key'], raw=True)
        self.assertIn('cdnmidimgurl', raw['content'])

    def test_linked_image_quote_reuses_one_batch_resource(self):
        self.data = [self.wx(123, '<msg><img md5="' + 'a' * 32 + '"/></msg>', 'image'),
                     self.wx(456, self.quote(reference_type='3', server='123'), timestamp=NOW - 50)]
        with patch('attachments.collect', side_effect=self.fake_collect) as prepare:
            page = self.service.read(timings=True)
        self.assertEqual(len(prepare.call_args.args[0]['messages']), 1)
        self.assertEqual([r['status'] for r in page['attachments']], ['prepared', 'prepared'])
        self.assertEqual(page['attachments'][0]['items'][0]['local_path'], page['attachments'][1]['items'][0]['local_path'])
        self.assertEqual(page['timings']['attachment_prepare']['sources'][0]['unique_resources'], 1)
        self.service.write(self.payload(page))

    def test_image_caption_prefix_is_not_discarded_as_xml_transport(self):
        self.data = [self.wx(1, '明天下午交练习\n<msg><img md5="' + 'a' * 32 + '"/></msg>', 'image')]
        page = self.service.read(prepare_images=False)
        self.assertEqual(page['messages'][0]['message']['text'], '明天下午交练习')

    def test_large_quote_title_is_available_in_full_compact_chunks(self):
        title = '实际正文不应丢弃' * 3000
        self.data = [self.wx(1, self.quote(title=title))]
        page = self.service.read()
        item = page['messages'][0]
        self.assertIn('content_ref', item)
        offset, parts = 0, []
        while True:
            chunk = self.service.read(batch_id=page['batch_id'], message=item['message_key'], offset=offset)
            parts.append(chunk['content'])
            if chunk['next_offset'] is None:
                break
            offset = chunk['next_offset']
        self.assertEqual(json.loads(''.join(parts))['text'], title)

    def test_later_quote_reuses_original_image_cache_across_batches(self):
        parent = self.wx(123, '<msg><img md5="' + 'a' * 32 + '"/></msg>', 'image')
        self.data = [parent]
        with patch('attachments.collect', side_effect=self.fake_collect) as prepare:
            first = self.service.read()
            self.service.write(self.payload(first))
            self.data = [self.wx(456, self.quote(reference_type='3', server='123'), timestamp=NOW - 50)]
            second = self.service.read()
        prepare.assert_called_once()
        self.assertTrue(second['attachments'][0]['cached'])
        self.assertEqual(second['attachments'][0]['items'][0]['local_path'], first['attachments'][0]['items'][0]['local_path'])
        self.service.write(self.payload(second))

    def test_reference_cannot_borrow_image_from_another_group(self):
        self.add_group('other@chatroom')
        parent = self.wx(123, '<msg><img md5="' + 'a' * 32 + '"/></msg>', 'image', group_id='other@chatroom')
        self.data = [parent]
        old = self.service.read(prepare_images=False)
        result = self.payload(old)
        result['results'][0].update(status='skipped', reason='synthetic user-approved skip')
        self.service.write(result)
        self.data = [self.wx(456, self.quote(reference_type='3', server='123'))]
        page = self.service.read()
        self.assertEqual(page['total'], 1)
        self.assertEqual(page['attachments'][0]['status'], 'pending')

    def test_unresolved_image_quote_remains_pending(self):
        self.data = [self.wx(456, self.quote(reference_type='3', server='not-found'))]
        page = self.service.read()
        self.assertEqual(page['attachments'][0]['status'], 'pending')
        with self.assertRaises(AgentError):
            self.service.write(self.payload(page))

    def test_malformed_quote_and_unknown_cards_not_silently_done(self):
        for xml in ('<msg><appmsg>', '<msg><appmsg><type>999</type></appmsg></msg>'):
            self.data = [self.wx(1, xml)]
            page = self.service.read()
            with self.assertRaises(AgentError):
                self.service.write(self.payload(page))



class FirstReadWindowTests(AgentFixture):
    def test_all_history_grant_does_not_scan_empty_decades_first(self):
        self.add_group('123', since=0)
        self.data = [self.message()]
        page = self.service.read()
        window = page['read_windows'][0]
        self.assertEqual(window['since'], NOW - 7 * 86400)
        self.assertEqual(window['until'], NOW)
        self.assertEqual(window['history_available_before'], NOW - 7 * 86400)
        self.assertFalse(page['more_history'])
        self.assertEqual(page['total'], 1)
        self.assertIn('history_notice', page)
        self.assertEqual(self.config.load()['groups'][self.group['key']]['since'], 0)
        self.service.read()
        scopes = self.read_mock.call_args.args[1]['scopes']
        self.assertEqual(scopes[0]['since'], NOW - 86400)

    def test_explicit_early_history_is_still_read_and_paginated(self):
        self.add_group('123', since=0)
        page = self.service.read(since=0)
        window = page['read_windows'][0]
        self.assertEqual(window['since'], 0)
        self.assertEqual(window['until'], 7 * 86400)
        self.assertTrue(page['more_history'])
        self.assertNotIn('history_available_before', window)
        self.assertNotIn('history_notice', page)

    def test_initial_recent_window_stays_inside_closed_grant(self):
        upper = NOW - 30 * 86400
        self.add_group('123', since=0, until=upper)
        self.data = [self.message(timestamp=upper - 100), self.message(2)]
        page = self.service.read()
        self.assertEqual(page['read_windows'][0]['since'], upper - 7 * 86400)
        self.assertEqual(page['read_windows'][0]['until'], upper)
        self.assertEqual(page['total'], 1)

    def test_recent_window_does_not_skip_existing_catchup_cursor(self):
        lower = NOW - 30 * 86400
        self.add_group('123', since=lower)
        self.service.read(since=lower, until=lower + 86400)
        page = self.service.read()
        self.assertEqual(page['read_windows'][0]['since'], lower)
        self.assertEqual(page['read_windows'][0]['until'], lower + 7 * 86400)
        self.assertTrue(page['more_history'])
        self.assertNotIn('history_notice', page)


class ImageDiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='wx-image-synthetic-')
        self.root = Path(self.temp.name).resolve()
        self.account, self.output = self.root / 'account', self.root / 'output'
        self.account.mkdir()
        self.output.mkdir()
        self.config = {'wechat': {'account_dir': str(self.account)}, 'attachments': {'extra_allowed_roots': []},
                       'policy': {'allow_process_memory_read': False}}
        self.messages = []

    def tearDown(self):
        self.temp.cleanup()

    def add_image(self, index, month='2030-01', group='synthetic@chatroom'):
        digest = hashlib.md5(f'synthetic-{index}'.encode()).hexdigest()
        folder = self.account / 'msg/attach' / hashlib.md5(group.encode()).hexdigest() / month / 'Img'
        folder.mkdir(parents=True, exist_ok=True)
        (folder / (digest + '.dat')).write_bytes(png())
        self.messages.append({'source_id': str(index), 'platform': 'wechat', 'type': 'image', 'group_id': group,
                              'group_name': '合成群', 'time': '2030-01-01T00:00:00+08:00',
                              'text': f'<msg><img md5="{digest}"/></msg>'})

    def run_collect(self):
        with patch('attachments.os.walk', wraps=os.walk) as walk, patch('attachments.wx_image_key', return_value=(None, None)):
            result = collect({'messages': self.messages}, self.config, self.output, 0, persist=False)
        return result, walk.call_count

    def test_direct_month_hash_paths_need_no_walk(self):
        for index in range(4):
            self.add_image(index)
        result, walks = self.run_collect()
        self.assertEqual(walks, 0)
        self.assertEqual(result['timings']['wechat_direct_hits'], 4)
        self.assertTrue(all(i['status'] == 'image_decoded' for i in result['items']))

    def test_cross_month_fallback_walks_group_only_once_for_four_images(self):
        for index in range(4):
            self.add_image(index, month='2029-12')
        result, walks = self.run_collect()
        self.assertEqual(walks, 1)
        self.assertEqual(result['timings']['wechat_group_walks'], 1)
        self.assertTrue(all(i['status'] == 'image_decoded' for i in result['items']))

    def test_missing_hash_does_not_walk_whole_group(self):
        self.add_image(1)
        self.messages[0]['text'] = '<msg><img/></msg>'
        result, walks = self.run_collect()
        self.assertEqual(walks, 0)
        self.assertEqual(result['items'][0]['status'], 'not_saved_locally')

    def test_group_indexes_do_not_mix(self):
        self.add_image(1, month='2029-12', group='one@chatroom')
        self.add_image(2, month='2029-12', group='two@chatroom')
        result, walks = self.run_collect()
        self.assertEqual(walks, 2)
        self.assertTrue(all(i['status'] == 'image_decoded' for i in result['items']))


if __name__ == '__main__':
    unittest.main()
