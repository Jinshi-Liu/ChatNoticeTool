"""Synthetic coverage of the reduced daily/setup boundary and historical reuse."""
from __future__ import annotations

from dataclasses import asdict
import io
import json
from pathlib import Path
import subprocess
import sys
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'tests')]
import agent_cli
import agent_setup
from agent_config import AgentError, encode
from agent_processing import ProcessingStore
from agent_sources import LocalSource, cached_report
from project_core import ReadScope
from test_agent import AgentFixture, NOW


class BackendInterfaceTests(AgentFixture):
    def cli(self, module, argv, payload=None):
        output = io.StringIO()
        service = self.service if module is agent_cli else agent_setup.SetupService(self.root)
        with patch('sys.stdout', output), patch('sys.stdin', io.StringIO(encode(payload) if payload is not None else '')):
            status = module.main(argv, service=service)
        return status, json.loads(output.getvalue())

    def assets(self, mixed=False):
        media = Path(self.source['paths']['media_dir'])
        (media / 'notice.png').write_bytes(b'synthetic-image')
        (media / 'notice.pdf').write_bytes(b'%PDF-synthetic')
        attachments = [{'filename': 'notice.png', 'local_path': str(media / 'notice.png')}]
        if mixed:
            attachments.append({'filename': 'notice.pdf', 'local_path': str(media / 'notice.pdf')})
        self.data = [self.message(attachments=attachments, attachment_content_status='not_processed')]

    def collect(self, report, config, output, pid, **kwargs):
        items = []
        for message in report['messages']:
            for attachment in message['attachments']:
                source = Path(attachment['local_path'])
                local = output / source.name
                local.write_bytes(source.read_bytes())
                items.append({'source_id': message['source_id'], 'status': 'copied_local_file',
                              'filename': source.name, 'source_path': str(source), 'local_path': str(local)})
        return {'items': items}

    def test_daily_cli_has_exactly_two_commands_and_no_hidden_alias(self):
        for command in ('inspect', 'setup', 'source', 'groups', 'import', 'attachments', 'todos', 'processing'):
            status, value = self.cli(agent_cli, [command])
            self.assertEqual(status, 2)
            self.assertEqual(value['error']['code'], 'invalid_arguments')
        for option in ('--prepare-images', '--pid', '--authorize-memory-read'):
            status, _ = self.cli(agent_cli, ['read', option])
            self.assertEqual(status, 2)
        self.read_mock.assert_not_called()

    def test_daily_import_does_not_load_setup_parser(self):
        process = subprocess.run(
            [sys.executable, '-B', '-X', 'utf8', '-c',
             f"import sys; sys.path.insert(0, {str(ROOT)!r}); import agent_cli; assert 'agent_setup' not in sys.modules; print('daily-only')"],
            cwd=self.root, capture_output=True, text=True, encoding='utf-8', timeout=20)
        self.assertEqual(process.returncode, 0, process.stderr)
        self.assertIn('daily-only', process.stdout)

    def test_all_attachments_opt_in_preserves_document_and_image_grants(self):
        self.assets(mixed=True)
        with patch('attachments.collect', side_effect=self.collect) as collect:
            default = self.service.read()
            self.assertEqual(default['attachments'][0]['status'], 'not_requested')
            denied = self.service.read(attachments=True)
            self.assertEqual(denied['attachments'][0]['status'], 'scope_denied')
            collect.assert_not_called()
            self.add_group('123', grant={'text_to_agent': True, 'images_to_agent': True, 'documents_to_agent': True})
            page = self.service.read(attachments=True, prepare_images=False)
        self.assertEqual(page['attachments'][0]['status'], 'prepared')
        self.assertEqual(len(page['attachments'][0]['items']), 2)
        self.assertEqual(page['messages'][0]['attachment_state'], 'prepared')
        self.service.write(self.commit_payload(page, attachment_state='analyzed'))
        self.assertEqual(self.service.read()['messages'], [])

    def test_message_attachments_report_not_content_and_no_new_page_delivery(self):
        self.assets()
        self.data.append(self.message(2, timestamp=NOW - 50))
        page = self.read(limit=1)
        key = page['messages'][0]['message_key']
        with patch('attachments.collect', side_effect=self.collect), patch('memory_reader.ReadOnlyProcess') as memory:
            status, value = self.cli(agent_cli, ['read', '--batch', page['batch_id'], '--message', key, '--attachments'])
        self.assertEqual(status, 0, value)
        self.assertEqual(set(value['data']), {'batch_id', 'results', 'remote_downloads', 'notice'})
        self.assertEqual(value['data']['results'][0]['status'], 'prepared')
        memory.assert_not_called()
        self.read_mock.assert_called_once()
        with ProcessingStore(self.root).connection() as db:
            undelivered = db.execute('SELECT message_key FROM batch_messages WHERE delivered=0').fetchone()[0]
        with self.assertRaises(AgentError) as cm:
            self.service.read(batch_id=page['batch_id'], message=undelivered, attachments=True)
        self.assertEqual(cm.exception.code, 'message_not_delivered')
        chunk = self.service.read(batch_id=page['batch_id'], message=key)
        self.assertIn('content', chunk)
        self.revoke()
        with self.assertRaises(AgentError):
            self.service.read(batch_id=page['batch_id'], message=key, attachments=True)

    def test_attachment_mode_rejects_chunk_parameters_even_explicit_offset_zero(self):
        self.data = [self.message()]
        page = self.read()
        key = page['messages'][0]['message_key']
        for extra in ({'offset': 0}, {'offset': 1}, {'raw': True}, {'chars': 1}, {'cursor': 1}):
            with self.assertRaises(AgentError):
                self.service.read(batch_id=page['batch_id'], message=key, attachments=True, **extra)
        for extra in (['--offset', '0'], ['--raw']):
            status, value = self.cli(agent_cli, ['read', '--batch', page['batch_id'], '--message', key, '--attachments', *extra])
            self.assertEqual(status, 2)
            self.assertEqual(value['error']['code'], 'invalid_input')
        with self.assertRaises(AgentError):
            self.service.read(todos=True, attachments=True)

    def test_setup_images_forwards_explicit_single_use_authorization(self):
        self.assets()
        page = self.read()
        key = page['messages'][0]['message_key']
        args = ['images', '--batch', page['batch_id'], '--message', key]
        with patch('attachments.collect', side_effect=self.collect) as collect:
            for options in (['--pid', '77'], ['--authorize-memory-read']):
                status, value = self.cli(agent_setup, args + options)
                self.assertEqual(status, 2)
                self.assertEqual(value['error']['code'], 'authorization_required')
            collect.assert_not_called()
            status, value = self.cli(agent_setup, args + ['--pid', '77', '--authorize-memory-read', '--timings'])
        self.assertEqual(status, 0, value)
        self.assertEqual(collect.call_args.args[3], 77)
        self.assertTrue(collect.call_args.args[1]['policy']['allow_process_memory_read'])
        self.assertEqual(value['data']['results'][0]['status'], 'prepared')
        self.assertIn('timings', value['data'])
        self.assertNotIn('allow_process_memory_read', encode(self.config.load()))
        self.revoke()
        status, value = self.cli(agent_setup, args + ['--pid', '77', '--authorize-memory-read'])
        self.assertEqual(status, 2)
        self.assertEqual(value['error']['code'], 'scope_denied')

    def test_setup_images_accepts_repeat_message_options(self):
        self.data = [self.message(1), self.message(2)]
        page = self.read()
        with patch('agent_assets.prepare', return_value={'synthetic': True}) as prepare:
            args = ['images', '--batch', page['batch_id']]
            for item in page['messages']:
                args += ['--message', item['message_key']]
            status, _ = self.cli(agent_setup, args)
        self.assertEqual(status, 0)
        self.assertEqual(prepare.call_args.args[1], {'batch_id': page['batch_id'],
                                                   'message_keys': [r['message_key'] for r in page['messages']]})

    def test_setup_inspect_init_and_source_check_do_not_read_bodies(self):
        (self.root / 'config.example.json').write_text('{}', encoding='utf-8')
        rules = self.root / 'AGENTS.md'
        rules.write_bytes(b'custom rules kept verbatim\r\n')
        before = rules.read_bytes()
        status, value = self.cli(agent_setup, ['init'])
        self.assertEqual(status, 0)
        self.assertFalse(value['data']['read_chat_bodies'])
        self.assertEqual(rules.read_bytes(), before)
        with patch.object(LocalSource, 'check', return_value={'next_actions': ['connect']}) as check:
            status, value = self.cli(agent_setup, ['source', 'check', '--source', self.source['id']])
        self.assertEqual(status, 0)
        check.assert_called_once()
        self.assertIn('python agent_setup.py source connect --source ' + self.source['id'], value['data']['next_actions'][0])
        self.read_mock.assert_not_called()

    def test_both_clis_share_bounded_redacted_json_input(self):
        for module, args in ((agent_cli, ['write', '--input', '-']),
                             (agent_setup, ['groups', 'apply', '--input', '-'])):
            for text, code in (('not-json-private-secret', 'invalid_json'), ('[]', 'invalid_input'),
                               (' ' * (1024 * 1024 + 1), 'input_too_large')):
                output = io.StringIO()
                with patch('sys.stdout', output), patch('sys.stdin', io.StringIO(text)):
                    status = module.main(args)
                value = json.loads(output.getvalue())
                self.assertEqual(status, 2)
                self.assertEqual(value['error']['code'], code)
                self.assertNotIn('private-secret', output.getvalue())


class HistoricalReuseTests(AgentFixture):
    def setUp(self):
        super().setUp()
        self.output = self.root / 'exports' / 'historical'
        self.output.mkdir(parents=True)
        self.request = self.request_for(['123'], NOW - 200, NOW)
        self.report = {'request': self.request_for(['123', '999'], NOW - 86400, NOW + 100),
                       'snapshots': [{'source': str(Path(self.source['paths']['database_dir']) / 'nt_msg.db')}],
                       'messages': [self.message(1, timestamp=NOW - 300), self.message(2),
                                    self.message(3, group='999'), self.message(4, timestamp=NOW + 50)]}
        self.save()

    def request_for(self, ids, since, until):
        scope = ReadScope('qq', tuple(ids), since, until)
        return {'confirmed': True, 'until': until, 'scopes': [asdict(scope)],
                'groups': [{'platform': 'qq', 'group_id': g, 'name': g} for g in ids]}

    def save(self):
        (self.output / 'messages.json').write_text(encode(self.report), encoding='utf-8')

    def cached(self, request=None, path=None):
        return cached_report(str(path or self.output), self.config.client_config(self.source),
                             request or self.request, self.root)

    def test_only_requested_group_and_time_no_fresh_read(self):
        result = self.cached()
        self.assertEqual([m['message_id'] for m in result['messages']], [2])
        self.assertTrue(result['used_cached_export'])
        self.assertEqual(result['cache_snapshot_until'], NOW + 100)
        self.assertIn('未检查客户端最新消息', result['cache_notice'])
        self.read_mock.assert_not_called()

    def test_incomplete_cache_never_falls_back_to_client(self):
        for request in (self.request_for(['777'], NOW - 200, NOW),
                        self.request_for(['123'], NOW - 2 * 86400, NOW),
                        self.request_for(['123'], NOW - 200, NOW + 200)):
            with self.assertRaises(AgentError) as cm:
                self.cached(request)
            self.assertEqual(cm.exception.code, 'cache_incomplete')
        self.read_mock.assert_not_called()

    def test_cache_paths_cannot_escape_export_root(self):
        for path in ('../outside', 'state', 'exports/../state', str(self.root.parent / 'outside')):
            with self.assertRaises(AgentError) as cm:
                self.cached(path=path)
            self.assertEqual(cm.exception.code, 'unsafe_path')
        self.read_mock.assert_not_called()

    def test_unconfirmed_cache_is_not_a_grant(self):
        self.report['request']['confirmed'] = False
        self.save()
        with self.assertRaises(AgentError) as cm:
            self.cached()
        self.assertEqual(cm.exception.code, 'cache_scope_invalid')

    def test_other_account_snapshot_not_reused(self):
        self.report['snapshots'][0]['source'] = str(self.root / 'other-account' / 'nt_msg.db')
        self.save()
        with self.assertRaises(AgentError) as cm:
            self.cached()
        self.assertEqual(cm.exception.code, 'cache_account_unverified')
