"""Portable import/adapter tests: standard library only, no real chat or clients."""
from __future__ import annotations

import base64
import importlib.util
import io
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
from agent_config import AgentError, encode
from agent_imports import ImportSource, ImportStore
from agent_processing import ProcessingStore
from agent_service import AgentService
from agent_sources import SOURCE_FACTORIES, get_source, register_source

NOW = 1893758400
PNG = base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/b1sAAAAASUVORK5CYII=')


class ImportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='chatnotice import 中文 ')
        self.base = Path(self.temp.name).resolve()
        self.root, self.incoming = self.base / 'component', self.base / 'incoming'
        self.root.mkdir()
        self.incoming.mkdir()
        self.patches = [patch('chatnotice.ROOT', self.root), patch('todo_store.ROOT', self.root),
                        patch('time.time', return_value=NOW), patch.dict(SOURCE_FACTORIES)]
        for p in self.patches:
            p.start()
        self.service = AgentService(self.root)
        self.source, self.group = self.configure()

    def tearDown(self):
        for p in reversed(self.patches):
            p.stop()
        self.temp.cleanup()

    def configure(self, platform='import', collection='inbox', group_id='inbox', grant=None, since=NOW - 10 * 86400):
        config = self.service.config
        source = config.configure_source({'confirmed': True, 'expected_revision': config.load()['revision'],
                                          'platform': platform, 'collection': collection})['source']
        group = config.apply_groups({'confirmed': True, 'expected_revision': config.load()['revision'], 'groups': [{
            'source_id': source['id'], 'group_id': group_id, 'since': since, 'max_days': 7,
            'recipient': 'synthetic current agent', 'instructions': '自行整理，说明写进正文即可',
            'grant': grant or {'text_to_agent': True, 'images_to_agent': True, 'documents_to_agent': True}}]})['groups'][0]
        return source, group

    def json_file(self, rows, name='messages.json'):
        path = self.incoming / name
        path.write_text(encode({'schema_version': 1, 'messages': rows}), encoding='utf-8')
        return path

    def importing(self, file, **kwargs):
        return self.service.import_file(group_key=self.group['key'], file=str(file), confirmed=True, **kwargs)

    def commit(self, page, **kwargs):
        results = []
        for item in page['messages']:
            results.append({'message_key': item['message_key'], 'content_hash': item['content_hash'],
                            'status': 'done', 'text_state': 'analyzed', 'attachment_state': 'not_applicable',
                            'task_ids': [], **kwargs})
        return self.service.commit({'batch_id': page['batch_id'], 'results': results})

    def asset_args(self, page):
        return {'batch_id': page['batch_id'], 'message_keys': [m['message_key'] for m in page['messages']]}

    def test_no_origin_or_timestamp_or_sender_required(self):
        page = self.importing(self.json_file([{'text': '无原始来源的合成通知'}]))
        message = page['messages'][0]['message']
        self.assertEqual(message['timestamp'], NOW)
        self.assertEqual(message['timestamp_basis'], 'imported')
        self.assertEqual(message['text'], '无原始来源的合成通知')
        self.assertNotIn('sender', message)
        self.assertEqual(page['counts']['created'], 1)
        self.assertFalse(page['read_client_database'])
        key = page['messages'][0]['message_key']
        result = self.service.todos_apply({'batch_id': page['batch_id'], 'operations': [{
            'operation_id': key + ':task:0', 'message_keys': [key], 'action': 'create',
            'fields': {'title': '合成事项', 'note': '有说明就直接写在正文里'}}]})
        task = self.service.todo_store().get(result['results'][0]['id'])
        self.assertEqual(task['source'], '')
        self.assertIn('正文', task['note'])

    def test_confirmation_checked_before_file_access(self):
        with patch.object(ImportSource, 'ingest') as ingest:
            with self.assertRaises(AgentError) as error:
                self.service.import_file(group_key=self.group['key'], file='/not-opened', confirmed=False)
        self.assertEqual(error.exception.code, 'confirmation_required')
        ingest.assert_not_called()

    def test_plain_text_is_one_message_not_a_guessed_chat_parser(self):
        file = self.incoming / 'notice.txt'
        file.write_text('张三 09:00\n明天提交\n第二行', encoding='utf-8-sig')
        page = self.importing(file)
        self.assertEqual(page['total'], 1)
        self.assertEqual(page['messages'][0]['message']['text'], '张三 09:00\n明天提交\n第二行')

    def test_repeat_missing_time_keeps_first_time_and_no_reanalysis(self):
        file = self.json_file([{'text': '重复的合成通知'}])
        first = self.importing(file)
        self.commit(first)
        with patch('time.time', return_value=NOW + 100):
            second = self.importing(file)
        self.assertEqual(second['messages'], [])
        self.assertEqual(second['counts'], {'created': 0, 'updated': 0, 'unchanged': 1})
        config = self.service.config.client_config(self.source)
        with ImportStore(config).connection() as db:
            self.assertEqual(db.execute('SELECT timestamp FROM records').fetchone()[0], NOW)

    def test_id_revision_in_old_time_window_is_immediately_pending(self):
        file = self.json_file([{'id': 'notice:1', 'timestamp': NOW - 3 * 86400, 'text': '旧日期'}])
        first = self.importing(file)
        self.commit(first)
        self.assertEqual(self.service.read()['total'], 0)
        self.json_file([{'id': 'notice:1', 'timestamp': NOW - 3 * 86400, 'text': '修订日期'}])
        updated = self.importing(file)
        self.assertEqual(updated['counts']['updated'], 1)
        self.assertEqual(updated['total'], 1)
        self.assertEqual(updated['messages'][0]['message_key'], first['messages'][0]['message_key'])
        self.assertEqual(self.service.read()['total'], 1)

    def test_import_does_not_advance_unscanned_history(self):
        page = self.importing(self.json_file([{'text': '合成通知', 'timestamp': NOW - 100}]))
        self.assertEqual(page['total'], 1)
        with ProcessingStore(self.root).connection() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM progress').fetchone()[0], 0)

    def test_daily_read_uses_queue_without_history_loops_or_reparsing(self):
        page = self.importing(self.json_file([{'text': '旧日期未完成通知', 'timestamp': NOW - 9 * 86400}]))
        with patch.object(ImportSource, 'read', side_effect=AssertionError('must not rescan')):
            result = self.service.read()
        self.assertEqual(result['total'], 1)
        self.assertFalse(result['more_history'])
        self.assertTrue(result['read_windows'][0]['queue_only'])
        self.assertEqual(result['messages'][0]['message_key'], page['messages'][0]['message_key'])

    def test_explicit_rescan_calls_adapter_with_bounded_scope(self):
        self.importing(self.json_file([{'text': '旧日期', 'timestamp': NOW - 9 * 86400}]))
        with patch.object(ImportSource, 'read', autospec=True, wraps=None) as read:
            read.return_value = {'messages': [], 'snapshots': []}
            self.service.read(since=NOW - 10 * 86400, until=NOW - 8 * 86400)
        self.assertEqual(read.call_count, 1)
        self.assertEqual(read.call_args.args[2]['scopes'][0]['since'], NOW - 10 * 86400)

    def test_retry_recovers_store_commit_before_ledger_commit(self):
        file = self.json_file([{'id': 'stable', 'text': '恢复通知'}])
        with patch.object(ProcessingStore, 'record_read', side_effect=RuntimeError('synthetic interruption')):
            with self.assertRaises(RuntimeError):
                self.importing(file)
        result = self.importing(file)
        self.assertEqual(result['counts']['unchanged'], 1)
        self.assertEqual(result['total'], 1)

    def test_reused_first_import_time_must_still_be_authorized(self):
        file = self.json_file([{'id': 'stable', 'text': '时间权限'}])
        self.importing(file)
        group = dict(self.group, since=NOW + 50)
        self.service.config.apply_groups({'confirmed': True, 'expected_revision': self.service.config.load()['revision'], 'groups': [group]})
        with patch('time.time', return_value=NOW + 100):
            with self.assertRaises(AgentError) as cm:
                self.importing(file)
        self.assertEqual(cm.exception.code, 'scope_denied')

    def test_missing_time_retry_uses_saved_time_with_past_grant_end(self):
        file = self.json_file([{'id': 'known', 'timestamp': NOW - 3 * 86400, 'text': '已知历史时间'}])
        first = self.importing(file)
        self.commit(first)
        self.json_file([{'id': 'known', 'text': '已知历史时间'}])
        group = dict(self.group, until=NOW - 2 * 86400)
        self.service.config.apply_groups({'confirmed': True, 'expected_revision': self.service.config.load()['revision'], 'groups': [group]})
        repeated = self.importing(file)
        self.assertEqual(repeated['counts']['unchanged'], 1)
        self.assertEqual(repeated['total'], 0)

    def test_resolved_time_span_is_checked_before_import_commit(self):
        file = self.json_file([{'id': 'old', 'timestamp': NOW - 9 * 86400, 'text': '旧日期'}])
        self.importing(file)
        self.json_file([{'id': 'old', 'text': '旧日期'}, {'id': 'new', 'text': '新日期'}])
        with self.assertRaises(AgentError) as cm:
            self.importing(file)
        self.assertEqual(cm.exception.code, 'scope_denied')
        with ImportStore(self.service.config.client_config(self.source)).connection() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM records').fetchone()[0], 1)

    def test_saved_missing_time_does_not_falsely_widen_import_range(self):
        file = self.json_file([{'id': 'old', 'timestamp': NOW - 9 * 86400, 'text': '旧日期'}])
        self.importing(file)
        self.json_file([{'id': 'old', 'text': '旧日期'},
                        {'id': 'old2', 'timestamp': NOW - 9 * 86400 + 100, 'text': '同一历史窗口'}])
        repeated = self.importing(file)
        self.assertEqual(repeated['counts'], {'created': 1, 'updated': 0, 'unchanged': 1})

    def test_direct_image_copied_and_prepared_without_external_parsers(self):
        image = self.incoming / 'notice.png'
        image.write_bytes(PNG)
        page = self.importing(image)
        with patch.dict(sys.modules, {'attachments': None, 'memory_reader': None, 'PIL': None}):
            prepared = self.service.prepare(self.asset_args(page))
            again = self.service.prepare(self.asset_args(page))
        item = prepared['results'][0]['items'][0]
        self.assertEqual(Path(item['local_path']).read_bytes(), PNG)
        self.assertTrue(Path(item['local_path']).is_relative_to(self.root / 'state/imports'))
        self.assertTrue(again['results'][0]['cached'])
        self.commit(page, attachment_state='analyzed')

    def test_original_image_can_be_removed_after_import(self):
        file = self.incoming / 'photo.png'
        file.write_bytes(PNG)
        page = self.importing(file)
        file.unlink()
        prepared = self.service.prepare(self.asset_args(page))
        self.assertEqual(prepared['results'][0]['status'], 'prepared')

    def test_tampered_asset_does_not_keep_prepared_state(self):
        file = self.incoming / 'photo.png'
        file.write_bytes(PNG)
        page = self.importing(file)
        first = self.service.prepare(self.asset_args(page))
        Path(first['results'][0]['items'][0]['local_path']).write_bytes(b'tampered')
        second = self.service.prepare(self.asset_args(page))
        self.assertEqual(second['results'][0]['status'], 'pending')
        with self.assertRaises(AgentError):
            self.commit(page, attachment_state='analyzed')

    def test_import_image_never_accepts_memory_authorization(self):
        file = self.incoming / 'photo.png'
        file.write_bytes(PNG)
        page = self.importing(file)
        with self.assertRaises(AgentError) as cm:
            self.service.prepare(self.asset_args(page), pid=1, authorize_memory_read=True)
        self.assertEqual(cm.exception.code, 'unsupported_operation')

    def test_json_with_text_and_relative_image(self):
        (self.incoming / 'picture.png').write_bytes(PNG)
        page = self.importing(self.json_file([{'id': 42, 'text': '通知见图', 'attachments': ['picture.png']}]))
        self.assertEqual(page['messages'][0]['message']['message_id'], '42')
        self.assertEqual(page['messages'][0]['message']['attachments'][0]['kind'], 'image')
        self.assertEqual(self.service.prepare(self.asset_args(page))['results'][0]['status'], 'prepared')

    def test_document_import_and_attachment_permission(self):
        pdf = self.incoming / 'notice.pdf'
        pdf.write_bytes(b'%PDF-1.4\nSynthetic unparsed fixture\n')
        page = self.importing(pdf)
        self.assertEqual(page['messages'][0]['message']['attachments'][0]['kind'], 'document')
        self.assertEqual(self.service.prepare(self.asset_args(page))['results'][0]['status'], 'prepared')
        self.source, self.group = self.configure(collection='no-images', grant={
            'text_to_agent': True, 'images_to_agent': False, 'documents_to_agent': True})
        image = self.incoming / 'picture.png'
        image.write_bytes(PNG)
        with self.assertRaises(AgentError) as cm:
            self.importing(image)
        self.assertEqual(cm.exception.code, 'scope_denied')

    def test_image_signature_not_extension_controls_grant(self):
        disguised = self.incoming / 'not-a-document.txt'
        disguised.write_bytes(PNG)
        self.source, self.group = self.configure(collection='no-images', grant={
            'text_to_agent': True, 'images_to_agent': False, 'documents_to_agent': True})
        with self.assertRaises(AgentError):
            self.importing(self.json_file([{'attachments': ['not-a-document.txt']}]))

    def test_missing_attachment_rejected_not_silently_dropped(self):
        with self.assertRaises(AgentError) as cm:
            self.importing(self.json_file([{'text': '附件缺失', 'attachments': ['missing.png']}]))
        self.assertEqual(cm.exception.code, 'path_not_found')

    def test_escape_absolute_url_and_links_rejected(self):
        outside = self.base / 'outside.png'
        outside.write_bytes(PNG)
        for reference in ('../outside.png', str(outside), 'https://example.invalid/picture.png', 'C:\\private\\image.png'):
            with self.assertRaises(AgentError):
                self.importing(self.json_file([{'attachments': [reference]}]))
        link = self.incoming / 'linked.png'
        try:
            link.symlink_to(outside)
        except OSError:
            return
        with self.assertRaises(AgentError):
            self.importing(self.json_file([{'attachments': ['linked.png']}]))

    def test_invalid_json_encoding_and_unknown_file_types_rejected(self):
        bad = self.incoming / 'bad.txt'
        bad.write_bytes(b'\xff\xfeinvalid')
        with self.assertRaises(AgentError) as cm:
            self.importing(bad)
        self.assertEqual(cm.exception.code, 'invalid_encoding')
        unknown = self.incoming / 'script.exe'
        unknown.write_bytes(b'MZ-not-executed')
        with self.assertRaises(AgentError):
            self.importing(unknown)
        with self.assertRaises(AgentError):
            self.importing(self.json_file([{'execute': 'not-a-command'}]))

    def test_future_outside_grant_and_oversized_time_span_rejected(self):
        for rows in ([{'timestamp': NOW + 1, 'text': 'future'}],
                     [{'timestamp': NOW - 11 * 86400, 'text': 'too old'}],
                     [{'timestamp': NOW - 8 * 86400, 'text': 'a'}, {'timestamp': NOW - 1, 'text': 'b'}]):
            with self.assertRaises(AgentError):
                self.importing(self.json_file(rows))

    def test_invalid_or_duplicate_ids_fail_without_partial_rows(self):
        file = self.json_file([{'id': 'same', 'text': 'a'}, {'id': 'same', 'text': 'b'}])
        with self.assertRaises(AgentError):
            self.importing(file)
        config = self.service.config.client_config(self.source)
        with ImportStore(config).connection() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM records').fetchone()[0], 0)

    def test_import_limits_are_explicit(self):
        image = self.incoming / 'big.png'
        image.write_bytes(PNG)
        with patch('agent_imports.MAX_FILE', 10):
            with self.assertRaises(AgentError) as cm:
                self.importing(image)
        self.assertEqual(cm.exception.code, 'input_too_large')
        with patch('agent_imports.MAX_MESSAGES', 1):
            with self.assertRaises(AgentError):
                self.importing(self.json_file([{'text': 'a'}, {'text': 'b'}]))

    def test_group_and_namespace_separation(self):
        file = self.json_file([{'id': 'same-id', 'text': 'same text'}])
        first = self.importing(file)
        self.source, self.group = self.configure(collection='another')
        second = self.importing(file)
        self.assertNotEqual(first['messages'][0]['message_key'], second['messages'][0]['message_key'])
        with self.assertRaises(AgentError):
            self.service.import_file(group_key='ungranted', file=str(file), confirmed=True)

    def test_attachment_ids_are_isolated_between_groups_in_one_source(self):
        first_image = self.incoming / 'first.png'
        second_image = self.incoming / 'second.png'
        first_image.write_bytes(PNG)
        second_image.write_bytes(PNG + b'synthetic trailing data')
        first = self.importing(self.json_file([{'id': 'same', 'attachments': ['first.png']}]))
        self.source, self.group = self.configure(group_id='other-group')
        second = self.importing(self.json_file([{'id': 'same', 'attachments': ['second.png']}]))
        self.assertNotEqual(first['messages'][0]['message']['source_id'], second['messages'][0]['message']['source_id'])
        page = self.service.read()
        self.assertEqual(page['total'], 2)
        prepared = self.service.prepare(self.asset_args(page))
        manifests = {r['message_key']: r['items'] for r in prepared['results']}
        self.assertEqual(len(manifests[first['messages'][0]['message_key']]), 1)
        self.assertEqual(manifests[first['messages'][0]['message_key']][0]['filename'], 'first.png')
        self.assertEqual(manifests[second['messages'][0]['message_key']][0]['filename'], 'second.png')

    def test_revoked_batch_cannot_read_prepare_or_commit(self):
        page = self.importing(self.json_file([{'text': '合成通知'}]))
        self.service.config.remove_groups({'confirmed': True, 'expected_revision': self.service.config.load()['revision'],
                                          'group_keys': [self.group['key']]})
        for action in (lambda: self.service.read(batch_id=page['batch_id']),
                       lambda: self.service.prepare(self.asset_args(page)), lambda: self.commit(page)):
            with self.assertRaises(AgentError):
                action()

    def test_metadata_listing_and_connection_do_not_touch_client_memory(self):
        result = self.service.source_action(self.source['id'], 'check')
        self.assertEqual(result['status'], 'ready')
        self.assertFalse(result['read_chat_bodies'])
        self.importing(self.json_file([{'text': 'hello'}]))
        groups = self.service.source_action(self.source['id'], 'groups')
        self.assertEqual(groups['groups'][0]['group_id'], 'inbox')
        with self.assertRaises(AgentError) as cm:
            self.service.source_action(self.source['id'], 'connect', pids=[1], authorized=True)
        self.assertEqual(cm.exception.code, 'unsupported_operation')

    def test_cli_import_pagination_commit_and_no_duplicate_analysis(self):
        file = self.json_file([{'id': str(i), 'text': '合成通知'} for i in range(4)])
        output = io.StringIO()
        with patch('sys.stdout', output):
            code = agent_cli.main(['import', '--group', self.group['key'], '--file', str(file), '--confirm', '--limit', '2'], service=self.service)
        self.assertEqual(code, 0, output.getvalue())
        page = json.loads(output.getvalue())['data']
        self.assertEqual(len(page['messages']), 2)
        self.commit(page)
        second = self.service.read(batch_id=page['batch_id'], cursor=page['next_cursor'])
        self.assertEqual(len(second['messages']), 2)
        self.commit(second)
        self.assertEqual(self.importing(file)['total'], 0)

    def test_unknown_import_database_never_overwritten(self):
        config = self.service.config.client_config(self.source)
        store = ImportStore(config)
        store.directory.mkdir(parents=True)
        db = sqlite3.connect(store.path)
        db.execute('CREATE TABLE personal(x)')
        db.commit()
        db.close()
        before = store.path.read_bytes()
        with self.assertRaises(AgentError):
            self.importing(self.json_file([{'text': 'no write'}]))
        self.assertEqual(store.path.read_bytes(), before)

    def test_example_adapter_maps_jsonl_and_uses_same_pipeline(self):
        spec = importlib.util.spec_from_file_location('example_jsonl_adapter', ROOT / 'examples/adapters/jsonl_adapter.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        module.register()
        self.source, self.group = self.configure(platform='example_jsonl')
        file = self.incoming / 'events.jsonl'
        file.write_text(json.dumps({'event_id': 'x', 'body': '合成厂商格式，无需来源字段'}, ensure_ascii=False) + '\n', encoding='utf-8')
        page = self.importing(file, format='jsonl')
        self.assertEqual(page['messages'][0]['message']['platform'], 'example_jsonl')
        self.assertEqual(page['messages'][0]['message']['text'], '合成厂商格式，无需来源字段')
        self.commit(page)
        self.assertEqual(self.service.read()['total'], 0)
        with self.assertRaises(AgentError):
            register_source('example_jsonl', module.ExampleJSONLSource)
        with self.assertRaises(AgentError):
            get_source('unregistered')

    def test_basic_read_file_and_write_text_need_no_extra_calls(self):
        file = self.incoming / 'notice.txt'
        file.write_text('合成通知：正文无原始来源', encoding='utf-8')
        page = self.service.read(group_keys=[self.group['key']], file=str(file), confirmed=True)
        message = page['messages'][0]
        result = self.service.write({'batch_id': page['batch_id'], 'results': [
            {'message_key': message['message_key'], 'content_hash': message['content_hash'], 'status': 'done'}]})
        self.assertFalse(result['has_failures'])
        self.assertEqual(self.service.read()['messages'], [])

    def test_basic_read_image_and_write_are_standard_library_only(self):
        file = self.incoming / 'picture.png'
        file.write_bytes(PNG)
        with patch.dict(sys.modules, {'attachments': None, 'memory_reader': None, 'PIL': None, 'cryptography': None}):
            page = self.service.read(group_keys=[self.group['key']], file=str(file), confirmed=True)
            self.assertEqual(page['attachments'][0]['status'], 'prepared')
            message = page['messages'][0]
            result = self.service.write({'batch_id': page['batch_id'], 'operations': [{
                'operation_id': 'portable-image', 'action': 'create', 'message_keys': [message['message_key']],
                'fields': {'title': '合成图片事项'}}], 'results': [
                    {'message_key': message['message_key'], 'content_hash': message['content_hash'], 'status': 'done'}]})
        self.assertTrue(result['operations'][0]['ok'])
        self.assertEqual(len(result['results'][0]['task_ids']), 1)

    def test_basic_read_file_requires_selection_confirmation(self):
        file = self.json_file([{'text': '合成通知'}])
        with patch.object(ImportSource, 'ingest') as ingest:
            with self.assertRaises(AgentError):
                self.service.read(group_keys=[self.group['key']], file=str(file))
        ingest.assert_not_called()

    def test_basic_read_write_standalone_todos_and_removed_ids(self):
        result = self.service.write({'operations': [
            {'operation_id': str(i), 'action': 'create', 'fields': {'title': f'合成待办{i}'}} for i in range(2)]})
        ids = [r['id'] for r in result['operations']]
        self.service.todo_store().mark(ids[1], True)
        self.service.todo_store().remove(ids[1])
        self.assertEqual(self.service.read(todos=True)['total'], 1)
        selected = self.service.read(ids=ids)['tasks']
        self.assertEqual(len(selected), 2)
        removed = next(r for r in selected if r['id'] == ids[1])
        self.assertEqual((removed['done'], removed['removed']), (1, 1))
        self.assertEqual(self.service.read(todos=True, limit=1, include_removed=True)['next_cursor'], 1)

    def test_cli_basic_read_supports_file_and_todo_modes(self):
        file = self.json_file([{'text': '合成通知'}])
        output = io.StringIO()
        with patch('sys.stdout', output):
            code = agent_cli.main(['read', '--file', str(file), '--group', self.group['key'], '--confirm'], service=self.service)
        self.assertEqual(code, 0, output.getvalue())
        self.assertEqual(json.loads(output.getvalue())['data']['counts']['created'], 1)
        with patch('sys.stdout', io.StringIO()):
            self.assertEqual(agent_cli.main(['read', '--todos'], service=self.service), 0)

    def test_portable_pipeline_imports_no_client_or_image_packages(self):
        result = subprocess.run([sys.executable, '-B', '-X', 'utf8', '-c',
                                 "import agent_cli,sys; assert not any(n in sys.modules for n in ('cryptography','memory_reader','attachments','PIL')); print('stdlib-only')"],
                                cwd=ROOT, capture_output=True, text=True, encoding='utf-8', timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('stdlib-only', result.stdout)


if __name__ == '__main__':
    unittest.main()
