"""Synthetic DingTalk source tests; never discover/read a real client or logs."""
from __future__ import annotations

import base64
import io
import json
from pathlib import Path
import sqlite3
import struct
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
import agent_cli
from agent_config import AgentError
from agent_service import AgentService
from agent_sources import candidates, get_source
import database_reader
import dingtalk_export as source
import dingtalk_probe as probe

NOW = 1893758400
UID, SALT = '12345678', 'synthetic-salt'


def encrypt(data):
    crypt = Cipher(algorithms.AES(probe.derive_key(UID, SALT)), modes.ECB()).encryptor()
    return crypt.update(data) + crypt.finalize()


def encrypted_wal(data, mode='plaintext'):
    result = bytearray(data[:32])
    state = struct.unpack_from('>II', data, 24)
    order = '<' if int.from_bytes(data[:4], 'big') == 0x377F0682 else '>'
    for offset in range(32, len(data), 4120):
        header = bytearray(data[offset:offset + 24])
        page = data[offset + 24:offset + 4120]
        encrypted = encrypt(page)
        if mode == 'ciphertext':
            state = probe.wal_checksum(header[:8] + encrypted, state, order)
            header[16:24] = struct.pack('>II', *state)
        result.extend(header)
        result.extend(encrypted)
    return bytes(result)


def wal_frames(frames):
    header = struct.pack('>IIII', 0x377F0682, 3007000, 4096, 0) + b'abcdefgh'
    state = probe.wal_checksum(header, (0, 0), '<')
    data = header + struct.pack('>II', *state)
    for number, commit, plain in frames:
        prefix = struct.pack('>II', number, commit)
        state = probe.wal_checksum(prefix + plain, state, '<')
        data += prefix + b'abcdefgh' + struct.pack('>II', *state) + encrypt(plain)
    return data


class DingTalkTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='dingtalk-synthetic-')
        self.root = Path(self.temp.name).resolve()
        self.project, self.account = self.root / 'component', self.root / 'client_v3'
        self.project.mkdir()
        (self.account / 'DBFiles').mkdir(parents=True)
        (self.account / 'user_config').write_bytes(base64.b64encode(json.dumps({'salt': SALT}).encode()))
        self.patches = [patch('chatnotice.ROOT', self.project), patch('todo_store.ROOT', self.project), patch('time.time', return_value=NOW)]
        for p in self.patches:
            p.start()
        self.service = AgentService(self.project)
        self.client = self.account / 'DBFiles/dingtalk.db'
        self.key = probe.derive_key(UID, SALT)
        self.writers = []

    def tearDown(self):
        for writer in self.writers:
            writer.close()
        for p in reversed(self.patches):
            p.stop()
        self.temp.cleanup()

    def fixture(self, rows=None, wal=False, mode='plaintext', keep_open=False):
        database = self.root / ('fixture-' + str(len(list(self.root.glob('fixture-*')))) + '.db')
        db = sqlite3.connect(database)
        db.execute('PRAGMA page_size=4096')
        db.execute('PRAGMA journal_mode=WAL')
        db.execute('CREATE TABLE tbconversation(cid TEXT,type INTEGER,title TEXT,status INTEGER)')
        db.executemany('INSERT INTO tbconversation VALUES(?,?,?,1)', [('G1', 2, '合成群'), ('G2', 2, '另一合成群'), ('P1', 1, '合成单聊')])
        for name in ('tbmsg_000', 'tbmsg_001'):
            db.execute(f'CREATE TABLE {name}(mid TEXT,cid TEXT,senderId TEXT,createdAt INTEGER,content TEXT,contentType INTEGER,recallStatus INTEGER)')
        db.commit()
        db.execute('PRAGMA wal_checkpoint(TRUNCATE)')
        if rows is None:
            rows = [('tbmsg_000', '1', 'G1', '合成通知', 1, NOW - 100)]
        for table, mid, cid, body, typ, timestamp in rows:
            db.execute(f'INSERT INTO {table} VALUES(?,?,?,?,?,?,0)', (mid, cid, 'synthetic-user', timestamp * 1000 + 123, json.dumps({'text': body}), typ))
        db.commit()
        if not wal:
            db.execute('PRAGMA wal_checkpoint(TRUNCATE)')
        self.client.write_bytes(encrypt(database.read_bytes()))
        wal_path = Path(str(database) + '-wal')
        if wal and wal_path.is_file():
            Path(str(self.client) + '-wal').write_bytes(encrypted_wal(wal_path.read_bytes(), mode))
        if keep_open:
            self.writers.append(db)
            return db, database
        db.close()

    def configure(self, cid='G1'):
        self.source = self.service.config.configure_source({'confirmed': True, 'expected_revision': self.service.config.load()['revision'],
                    'platform': 'dingtalk', 'uid': UID, 'paths': {'account_dir': str(self.account)}})['source']
        self.group = self.service.config.apply_groups({'confirmed': True, 'expected_revision': self.service.config.load()['revision'], 'groups': [{
            'source_id': self.source['id'], 'group_id': cid, 'since': NOW - 86400, 'recipient': 'synthetic agent',
            'instructions': '由Agent自行判断', 'grant': {'text_to_agent': True, 'images_to_agent': False, 'documents_to_agent': False}}]})['groups'][0]

    def open(self):
        return probe.decrypted_database(self.account, UID, runtime=self.project / 'runtime/dingtalk-snapshots')

    def test_read_write_normal_flow_and_source_unchanged(self):
        self.fixture()
        self.configure()
        before = self.client.read_bytes()
        with patch('dingtalk_probe.check_header', side_effect=AssertionError('no repeated check')):
            page = self.service.read(timings=True)
        item = page['messages'][0]
        self.assertEqual(item['message']['text'], '合成通知')
        self.assertEqual(item['message']['platform'], 'dingtalk')
        result = self.service.write({'batch_id': page['batch_id'], 'operations': [{
            'operation_id': 'synthetic-task', 'action': 'create', 'message_keys': [item['message_key']], 'fields': {'title': '合成事项'}}],
            'results': [{'message_key': item['message_key'], 'content_hash': item['content_hash'], 'status': 'done'}]})
        self.assertFalse(result['has_failures'])
        self.assertEqual(self.service.read()['messages'], [])
        self.assertEqual(before, self.client.read_bytes())
        self.assertEqual(list((self.project / 'runtime/dingtalk-snapshots').iterdir()), [])

    def test_groups_only_metadata_and_explicit_uid(self):
        self.fixture()
        self.configure()
        result = self.service.source_action(self.source['id'], 'groups')
        self.assertEqual({g['group_id'] for g in result['groups']}, {'G1', 'G2'})
        self.assertFalse(result['read_chat_bodies'])
        checked = self.service.source_action(self.source['id'], 'check')
        self.assertEqual(checked['status'], 'format_candidate')
        self.assertFalse(checked['read_chat_bodies'])
        with self.assertRaises(AgentError):
            source.DingTalkSource().configure({'paths': {'account_dir': str(self.account)}})

    def test_no_confirm_no_client_access(self):
        with patch.object(source.DingTalkSource, '_open') as opened:
            with self.assertRaises(AgentError):
                source.DingTalkSource().read({}, {'confirmed': False})
            opened.assert_not_called()
        with patch.object(source.DingTalkSource, 'configure') as configuring:
            with self.assertRaises(AgentError):
                self.service.config.configure_source({'platform': 'dingtalk'})
            configuring.assert_not_called()

    def test_no_group_authorization_no_snapshot(self):
        self.fixture()
        with patch.object(source.DingTalkSource, '_open') as opened:
            with self.assertRaises(AgentError):
                self.service.read()
            opened.assert_not_called()

    def test_reject_direct_or_missing_conversation_before_message_queries(self):
        self.fixture()
        self.configure(cid='P1')
        with patch.object(source, 'selected_dingtalk') as selected:
            with self.assertRaises(AgentError):
                self.service.read()
            selected.assert_not_called()
        self.configure(cid='missing')
        with self.assertRaises(AgentError):
            self.service.read()

    def test_all_shards_and_time_scope_without_silent_empty(self):
        self.fixture([('tbmsg_000', 'old', 'G1', '窗外', 1, NOW - 90000),
                      ('tbmsg_001', 'new', 'G1', '窗内', 1, NOW - 100),
                      ('tbmsg_001', 'private', 'P1', '不应交付', 1, NOW - 100)])
        self.configure()
        page = self.service.read()
        self.assertEqual(page['total'], 1)
        self.assertEqual(page['messages'][0]['message']['text'], '窗内')

    def test_identical_cross_shard_duplicates_once_conflicts_fail(self):
        self.fixture([('tbmsg_000', 'same', 'G1', '相同', 1, NOW - 100), ('tbmsg_001', 'same', 'G1', '相同', 1, NOW - 100)])
        self.configure()
        self.assertEqual(self.service.read()['total'], 1)
        with self.open() as (db, _):
            self.assertEqual(len(source.selected_dingtalk(db, ['G1'], (NOW - 200) * 1000, NOW * 1000, {'G1': '合成群'})), 1)
        # A second isolated account fixture containing conflicting copies.
        self.client.unlink()
        self.fixture([('tbmsg_000', 'same', 'G1', '旧版', 1, NOW - 100), ('tbmsg_001', 'same', 'G1', '新版', 1, NOW - 100)])
        with self.assertRaises(AgentError) as error:
            self.service.read()
        self.assertEqual(error.exception.code, 'ambiguous_message')

    def test_unsupported_media_visible_pending_not_silent_drop(self):
        self.fixture([('tbmsg_000', 'image', 'G1', '未交付的CDN数据', 2, NOW - 100)])
        self.configure()
        page = self.service.read()
        item = page['messages'][0]
        self.assertEqual(item['message']['text'], '')
        self.assertEqual(page['attachments'][0]['status'], 'unsupported_or_no_attachment')
        with self.assertRaises(AgentError):
            self.service.write({'batch_id': page['batch_id'], 'results': [{'message_key': item['message_key'], 'content_hash': item['content_hash'], 'status': 'done'}]})

    def test_unknown_json_is_not_falsely_empty(self):
        text, status = source.decode_text('{"unknown":"synthetic"}')
        self.assertIn('unknown', text)
        self.assertEqual(status, 'nontext_or_unknown')

    def test_plain_numeric_replies_are_not_unknown_bodies(self):
        self.assertEqual(source.decode_text('1'), ('1', 'utf8'))
        self.assertEqual(source.decode_text('普通回复'), ('普通回复', 'utf8'))

    def test_snapshot_retries_checkpoint_race_instead_of_mixing_states(self):
        writer, database = self.fixture([('tbmsg_000', 'a', 'G1', 'A0', 1, NOW - 100),
                                         ('tbmsg_001', 'b', 'G1', 'B0', 1, NOW - 100)], keep_open=True)
        real_copy, changed = database_reader.shutil.copyfile, False
        def racing_copy(src, dst):
            nonlocal changed
            result = real_copy(src, dst)
            if Path(src) == self.client and not changed:
                changed = True
                writer.execute('UPDATE tbmsg_000 SET content=?', (json.dumps({'text': 'A1'}),))
                writer.commit()
                writer.execute('PRAGMA wal_checkpoint(TRUNCATE)')
                writer.execute('UPDATE tbmsg_001 SET content=?', (json.dumps({'text': 'B1'}),))
                writer.commit()
                self.client.write_bytes(encrypt(database.read_bytes()))
                Path(str(self.client) + '-wal').write_bytes(encrypted_wal(Path(str(database) + '-wal').read_bytes()))
            return result
        with patch.object(database_reader.shutil, 'copyfile', side_effect=racing_copy):
            with self.open() as (db, meta):
                values = [json.loads(db.execute(f'SELECT content FROM {t}').fetchone()[0])['text'] for t in ('tbmsg_000', 'tbmsg_001')]
                self.assertEqual(values, ['A1', 'B1'])
                self.assertEqual(meta['sqlite_quick_check'], 'ok')

    def test_snapshot_never_stabilizes_fails(self):
        self.fixture()
        real_copy = database_reader.shutil.copyfile
        def changing(src, dst):
            result = real_copy(src, dst)
            if Path(src) == self.client:
                data = bytearray(self.client.read_bytes())
                data[-1] ^= 1
                self.client.write_bytes(data)
            return result
        with patch.object(database_reader.shutil, 'copyfile', side_effect=changing):
            with self.assertRaises(RuntimeError):
                with self.open():
                    self.fail('must not accept unstable capture')

    def test_real_wal_plaintext_checksums_and_cleanup(self):
        self.fixture(wal=True)
        with self.open() as (db, meta):
            self.assertEqual(db.execute('SELECT COUNT(*) FROM tbmsg_000').fetchone()[0], 1)
            self.assertEqual(meta['checksum_mode'], 'plaintext')
            self.assertFalse(meta['page_authentication'])
            self.assertFalse({'key', 'aes_key_fingerprint', 'aes_key_hex'} & set(meta))
            self.assertNotIn(self.key.hex(), json.dumps(meta))

    def test_ciphertext_checksum_chain_supported(self):
        self.fixture(wal=True, mode='ciphertext')
        with self.open() as (db, meta):
            self.assertEqual(db.execute('SELECT COUNT(*) FROM tbmsg_000').fetchone()[0], 1)
            self.assertEqual(meta['checksum_mode'], 'ciphertext')

    def test_checksum_mode_cannot_switch_mid_chain(self):
        wal, main = self.root / 'mixed-wal', self.root / 'mixed-main'
        main.write_bytes(b'A' * 4096)
        data = bytearray(wal_frames([(1, 1, b'B' * 4096), (1, 1, b'C' * 4096)]))
        second = 32 + 4120
        previous = struct.unpack_from('>II', data, 48)
        checksum = probe.wal_checksum(data[second:second + 8] + data[second + 24:second + 4120], previous, '<')
        data[second + 16:second + 24] = struct.pack('>II', *checksum)
        wal.write_bytes(data)
        with self.assertRaises(AgentError):
            probe.replay_wal(wal, main, self.key)
        self.assertEqual(main.read_bytes(), b'A' * 4096)

    def test_same_message_moving_shards_does_not_duplicate_analysis(self):
        self.fixture()
        self.configure()
        page = self.service.read()
        item = page['messages'][0]
        self.service.write({'batch_id': page['batch_id'], 'results': [
            {'message_key': item['message_key'], 'content_hash': item['content_hash'], 'status': 'done'}]})
        self.fixture([('tbmsg_001', '1', 'G1', '合成通知', 1, NOW - 100)])
        self.assertEqual(self.service.read()['messages'], [])

    def test_different_explicit_uid_has_new_namespace(self):
        self.fixture()
        self.configure()
        another = self.service.config.configure_source({'confirmed': True, 'expected_revision': self.service.config.load()['revision'],
                  'platform': 'dingtalk', 'uid': '87654321', 'paths': {'account_dir': str(self.account)}})['source']
        self.assertNotEqual(self.source['id'], another['id'])
        self.assertEqual(self.service.config.load()['groups'][self.group['key']]['source_id'], self.source['id'])

    def test_bad_source_checksum_fails_not_rewritten(self):
        self.fixture(wal=True)
        wal = Path(str(self.client) + '-wal')
        data = bytearray(wal.read_bytes())
        data[48] ^= 128
        wal.write_bytes(data)
        with self.assertRaises(AgentError) as error:
            with self.open():
                self.fail('bad source checksum accepted')
        self.assertEqual(error.exception.code, 'snapshot_invalid')

    def test_bad_encrypted_payload_fails(self):
        self.fixture(wal=True)
        wal = Path(str(self.client) + '-wal')
        data = bytearray(wal.read_bytes())
        data[-1] ^= 1
        wal.write_bytes(data)
        with self.assertRaises(AgentError):
            with self.open():
                self.fail('bad page accepted')

    def test_only_committed_prefix_replayed(self):
        wal, main = self.root / 'sample-wal', self.root / 'sample-main'
        main.write_bytes(b'A' * 4096)
        wal.write_bytes(wal_frames([(1, 1, b'B' * 4096), (1, 0, b'C' * 4096)]))
        result = probe.replay_wal(wal, main, self.key)
        self.assertEqual(main.read_bytes(), b'B' * 4096)
        self.assertTrue(result['wal_ignored_tail'])
        self.assertEqual(result['wal_committed_frames'], 1)

    def test_torn_tail_after_commit_is_ignored(self):
        self.fixture(wal=True)
        wal = Path(str(self.client) + '-wal')
        with wal.open('ab') as stream:
            stream.write(b'partial')
        with self.open() as (db, meta):
            self.assertEqual(db.execute('SELECT COUNT(*) FROM tbmsg_000').fetchone()[0], 1)
            self.assertTrue(meta['wal_ignored_tail'])

    def test_truncated_main_and_wrong_uid_fail(self):
        self.fixture()
        probe.check_header(self.account, UID)
        with self.assertRaises(AgentError):
            probe.check_header(self.account, '87654321')
        self.client.write_bytes(self.client.read_bytes()[:-1])
        with self.assertRaises(AgentError):
            with self.open():
                self.fail('partial tail accepted')

    def test_invalid_wal_size_rejected_before_large_allocation(self):
        wal, main = self.root / 'sample-wal', self.root / 'sample-main'
        main.write_bytes(b'A' * 4096)
        data = bytearray(wal_frames([(1, 1, b'B' * 4096)]))
        struct.pack_into('>I', data, 8, 0xFFFFFFFF)
        data[24:32] = struct.pack('>II', *probe.wal_checksum(data[:24], (0, 0), '<'))
        wal.write_bytes(data)
        with self.assertRaises(AgentError):
            probe.replay_wal(wal, main, self.key)

    def test_client_links_are_refused(self):
        self.fixture()
        link = self.account / 'alias'
        try:
            link.symlink_to(self.account / 'DBFiles', target_is_directory=True)
        except OSError:
            self.skipTest('symlink privilege unavailable')
        with self.assertRaises(AgentError):
            probe.source_path(self.account, 'alias/dingtalk.db')

    def test_no_memory_or_socket_monkeypatch_added(self):
        import socket
        original = socket.socket
        self.fixture()
        self.configure()
        self.service.read()
        self.assertIs(socket.socket, original)
        with self.assertRaises(AgentError):
            self.service.source_action(self.source['id'], 'connect', pids=[1], authorized=True)

    def test_candidates_metadata_only_and_bounded(self):
        self.fixture()
        with patch.object(probe, 'read_salt', side_effect=AssertionError('must not read config')):
            result = candidates('dingtalk', str(self.root))
        self.assertEqual(len(result['candidates']), 1)
        self.assertFalse(result['read_chat_bodies'])

    def test_cli_stays_two_daily_commands(self):
        help = agent_cli.build_parser().format_help()
        self.assertIn('{read,write}', help)
        self.assertNotIn('dingtalk_probe', help)
        self.assertTrue(get_source('dingtalk').capabilities['experimental'])


if __name__ == '__main__':
    unittest.main()
