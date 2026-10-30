"""Schema migration and read-success evidence using only synthetic fixtures."""
import io
import json
from unittest.mock import patch

import agent_setup
from agent_config import AgentError, LEGACY_GROUP_FIELDS, atomic_json
from agent_processing import ProcessingStore, last_successful_reads
from group_settings import group_sections
from test_agent import AgentFixture, NOW


class GroupConfigV2Tests(AgentFixture):
    def legacy_config(self):
        value = self.config.load()
        value['schema_version'] = 1
        group = value['groups'][self.group['key']]
        group.update(since=NOW - 86400, until=NOW - 1, max_days=1, recipient='old agent',
                     instructions='old note', grant={'text_to_agent': True, 'images_to_agent': False,
                                                     'documents_to_agent': False})
        atomic_json(self.config.path, value)
        return value

    def test_legacy_inspect_and_gui_model_are_side_effect_free(self):
        self.legacy_config()
        before = self.config.path.read_bytes()
        result = agent_setup.SetupService(self.root).inspect()
        self.assertTrue(result['migration_required'])
        self.assertEqual(result['schema_version'], 2)
        self.assertFalse(LEGACY_GROUP_FIELDS & result['groups'][0].keys())
        sections = group_sections(self.config)
        self.assertIsNone(sections['qq'][0]['last_read_at'])
        self.assertEqual(before, self.config.path.read_bytes())
        self.assertFalse((self.root / 'state/processing.sqlite3').exists())
        self.read_mock.assert_not_called()

    def test_explicit_migrate_cli_confirmation_revision_and_persistence(self):
        old = self.legacy_config()
        before = self.config.path.read_bytes()
        for payload in ({'expected_revision': old['revision']},
                        {'confirmed': True, 'expected_revision': old['revision'] - 1}):
            with self.assertRaises(AgentError):
                self.config.migrate(payload)
            self.assertEqual(before, self.config.path.read_bytes())
        output = io.StringIO()
        with patch('sys.stdin', io.StringIO(json.dumps({'confirmed': True, 'expected_revision': old['revision']}))), patch('sys.stdout', output):
            code = agent_setup.main(['migrate', '--input', '-'], service=agent_setup.SetupService(self.root))
        self.assertEqual(code, 0, output.getvalue())
        saved = json.loads(self.config.path.read_text(encoding='utf-8'))
        self.assertEqual(saved['schema_version'], 2)
        self.assertEqual(saved['revision'], old['revision'] + 1)
        self.assertEqual(saved['sources'], old['sources'])
        self.assertEqual(saved['groups'][self.group['key']], self.group)
        self.assertFalse(self.config.migration_status()['migration_required'])
        self.read_mock.assert_not_called()

    def test_confirmed_agent_edit_also_persists_migration(self):
        old = self.legacy_config()
        self.config.apply_groups({'confirmed': True, 'expected_revision': old['revision'],
                                  'groups': [{**self.group, 'name': 'new name'}]})
        saved = json.loads(self.config.path.read_text(encoding='utf-8'))
        self.assertEqual(saved['schema_version'], 2)
        self.assertFalse(LEGACY_GROUP_FIELDS & saved['groups'][self.group['key']].keys())

    def test_migrated_legacy_limits_do_not_restrict_explicit_history_or_media(self):
        old = self.legacy_config()
        self.config.migrate({'confirmed': True, 'expected_revision': old['revision']})
        self.data = [self.message(timestamp=NOW - 20 * 86400,
                                  attachments=[{'filename': 'notice.png'}],
                                  attachment_content_status='not_processed')]
        page = self.read(since=NOW - 21 * 86400, until=NOW - 19 * 86400)
        self.assertEqual(page['total'], 1)
        from agent_assets import prepare
        def collect(report, config, output, pid, **kwargs):
            local = output / 'synthetic.png'
            local.write_bytes(b'synthetic test data')
            return {'items': [{'source_id': report['messages'][0]['source_id'],
                               'status': 'copied_local_file', 'local_path': str(local)}]}
        with patch('attachments.collect', side_effect=collect) as prepared:
            result = prepare(ProcessingStore(self.root), {'batch_id': page['batch_id'],
                             'message_keys': [page['messages'][0]['message_key']]})
        prepared.assert_called_once()
        self.assertEqual(result['results'][0]['status'], 'prepared')

    def test_removed_fields_rejected_in_new_configuration(self):
        before = self.config.path.read_bytes()
        for key in LEGACY_GROUP_FIELDS:
            with self.assertRaises(AgentError):
                self.config.apply_groups({'confirmed': True, 'expected_revision': self.config.load()['revision'],
                                          'groups': [{**self.group, key: None}]})
            self.assertEqual(before, self.config.path.read_bytes())

    def test_legacy_batch_stales_but_pending_dedup_and_progress_survive(self):
        self.data = [self.message(1), self.message(2)]
        first = self.read()
        self.commit_results(self.commit_payload(first))
        ledger = ProcessingStore(self.root)
        old = self.legacy_config()
        with ledger.connection() as db:
            db.execute('UPDATE batches SET rules=?', (json.dumps(old['groups']),))
        self.config.migrate({'confirmed': True, 'expected_revision': old['revision']})
        with self.assertRaises(AgentError) as error:
            self.read(batch_id=first['batch_id'])
        self.assertEqual(error.exception.code, 'stale_batch')
        page = self.read()
        self.assertEqual(page['total'], 1)
        self.assertEqual(page['messages'][0]['message_key'], first['messages'][1]['message_key'])
        self.assertEqual(ledger.pending()['groups'][0]['acquired_until'], NOW)


class ReadSuccessTests(AgentFixture):
    def times(self):
        return last_successful_reads(self.root, [self.group['key']])

    def test_last_read_is_independent_for_each_group_in_same_account(self):
        other = self.add_group('456')
        keys = [self.group['key'], other['key']]
        self.read(group_keys=[keys[0]])
        self.assertEqual(last_successful_reads(self.root, keys), {keys[0]: NOW})
        with patch('time.time', return_value=NOW + 100):
            self.read(group_keys=[keys[1]])
        self.assertEqual(last_successful_reads(self.root, keys),
                         {keys[0]: NOW, keys[1]: NOW + 100})
        with patch('time.time', return_value=NOW + 200):
            self.read(group_keys=[keys[0]])
        self.assertEqual(last_successful_reads(self.root, keys),
                         {keys[0]: NOW + 200, keys[1]: NOW + 100})
        rows = {row['key']: row for row in group_sections(self.config)['qq']}
        self.assertEqual(rows[keys[0]]['last_read_at'], NOW + 200)
        self.assertEqual(rows[keys[1]]['last_read_at'], NOW + 100)

    def test_batch_read_updates_only_selected_groups(self):
        other = self.add_group('456')
        unread = self.add_group('789')
        selected = [self.group['key'], other['key']]
        self.read(group_keys=selected)
        self.assertEqual(last_successful_reads(self.root, selected + [unread['key']]),
                         {key: NOW for key in selected})

    def test_empty_success_is_real_read_not_analysis_or_history_bound(self):
        self.assertEqual(self.times(), {})
        self.read(since=NOW - 30 * 86400, until=NOW - 29 * 86400)
        self.assertEqual(self.times(), {self.group['key']: NOW})
        self.assertEqual(ProcessingStore(self.root).pending()['groups'][0]['last_read_at'], NOW)
        self.assertEqual(group_sections(self.config)['qq'][0]['last_read_at'], NOW)

    def test_pending_body_and_attachment_work_do_not_delay_acquisition_timestamp(self):
        self.data = [self.message(body_status='parse_pending')]
        self.read()
        self.assertEqual(self.times()[self.group['key']], NOW)
        self.assertEqual(ProcessingStore(self.root).pending()['groups'][0]['pending_count'], 1)

    def test_paging_commit_and_failed_reads_do_not_advance_last_success(self):
        self.data = [self.message()]
        page = self.read()
        with patch('time.time', return_value=NOW + 100):
            self.read(batch_id=page['batch_id'])
            self.commit_results(self.commit_payload(page))
            self.read_mock.side_effect = RuntimeError('synthetic read failure')
            with self.assertRaises(RuntimeError):
                self.read()
        self.assertEqual(self.times()[self.group['key']], NOW)

    def test_out_of_scope_and_config_conflict_do_not_record_success(self):
        self.read_mock.side_effect = lambda config, request: {'messages': [self.message(group='999')]}
        with self.assertRaises(AgentError):
            self.read()
        self.assertEqual(self.times(), {})
        def change(config, request):
            self.add_group('123', name='concurrent rename')
            return {'messages': []}
        self.read_mock.side_effect = change
        with self.assertRaises(AgentError):
            self.read()
        self.assertEqual(self.times(), {})

    def test_multisource_failure_is_not_partial_success(self):
        dbdir, media = self.root / 'second-db', self.root / 'second-media'
        dbdir.mkdir()
        media.mkdir()
        source = self.config.configure_source({'confirmed': True, 'expected_revision': self.config.load()['revision'],
            'platform': 'qq', 'paths': {'database_dir': str(dbdir), 'media_dir': str(media)}})['source']
        self.config.apply_groups({'confirmed': True, 'expected_revision': self.config.load()['revision'],
                                  'groups': [{'source_id': source['id'], 'group_id': '456'}]})
        self.read_mock.side_effect = [{'messages': []}, RuntimeError('second source failed')]
        with self.assertRaises(RuntimeError):
            self.read()
        self.assertEqual(last_successful_reads(self.root, self.config.load()['groups']), {})

    def test_cached_export_is_not_a_live_read(self):
        with patch('agent_service.cached_report', return_value={'messages': []}):
            self.read(from_export='synthetic-only')
        self.assertEqual(self.times(), {})
        self.read_mock.assert_not_called()

    def test_old_state_has_unknown_time_and_read_only_view_does_not_migrate(self):
        self.read()
        store = ProcessingStore(self.root)
        with store.connection() as db:
            db.execute('DROP TABLE read_success')
            db.execute('PRAGMA user_version=1')
        before = store.path.read_bytes()
        self.assertEqual(self.times(), {})
        self.assertEqual(before, store.path.read_bytes())
        ProcessingStore(self.root)
        self.assertEqual(self.times(), {})
        self.read()
        self.assertEqual(self.times()[self.group['key']], NOW)
