"""Real subprocess/stdin smoke tests in a synthetic, disposable project only."""
from __future__ import annotations

import json
import os
from pathlib import Path
import struct
import subprocess
import sys
import tempfile
import unittest
import zlib

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from agent_config import AgentConfig

# Set both module roots before importing the CLI. No personal project state is
# inspected, and portable file imports never load client/memory-reading code.
RUNNER = """
from pathlib import Path
import sys
import chatnotice
chatnotice.ROOT = Path(sys.argv[1])
import todo_store
todo_store.ROOT = chatnotice.ROOT
import agent_cli
from agent_service import AgentService
code = agent_cli.main(sys.argv[2:], service=AgentService(chatnotice.ROOT))
assert not any(name in sys.modules for name in ('memory_reader', 'database_reader', 'cryptography'))
raise SystemExit(code)
"""


def synthetic_png():
    """A valid RGB image using only the standard library, no real attachment."""
    def chunk(kind, data):
        return struct.pack('>I', len(data)) + kind + data + struct.pack('>I', zlib.crc32(kind + data))
    pixels = (b'\x00' + bytes([255, 0, 0]) * 2) * 2
    return (b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('>IIBBBBB', 2, 2, 8, 2, 0, 0, 0))
            + chunk(b'IDAT', zlib.compress(pixels)) + chunk(b'IEND', b''))


class CliProcessTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='chatnotice-cli-synthetic-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        config = AgentConfig(self.root)
        source = config.configure_source({'expected_revision': 0, 'confirmed': True, 'platform': 'import',
                                          'collection': 'synthetic-inbox'})['source']
        self.group = config.apply_groups({'expected_revision': 1, 'confirmed': True, 'groups': [{
            'source_id': source['id'], 'group_id': 'inbox', 'name': '合成测试收件箱', 'since': 0,
            'instructions': '合成测试，不读取真实消息', 'recipient': '测试子进程',
            'grant': {'text_to_agent': True, 'images_to_agent': False, 'documents_to_agent': False}}]})['groups'][0]

    def cli(self, *args, payload=None, expected=0):
        process = subprocess.run([sys.executable, '-B', '-X', 'utf8', '-c', RUNNER, str(self.root), *args],
                                 input=json.dumps(payload, ensure_ascii=False) if payload is not None else '',
                                 capture_output=True, text=True, encoding='utf-8', cwd=ROOT,
                                 env={**os.environ, 'PYTHONDONTWRITEBYTECODE': '1'}, timeout=30)
        self.assertEqual(process.returncode, expected, process.stderr + process.stdout)
        value = json.loads(process.stdout)
        self.assertEqual(value['ok'], expected == 0)
        return value.get('data', value.get('error'))

    def test_read_file_write_stdin_replay_and_read_todos_across_processes(self):
        file = self.root / 'synthetic-notice.txt'
        file.write_text('合成通知：完成练习。没有原始时间，不推测相对日期。', encoding='utf-8')
        page = self.cli('read', '--file', str(file), '--group', self.group['key'], '--confirm', '--timings')
        self.assertFalse(page['read_client_database'])
        item = page['messages'][0]
        payload = {'batch_id': page['batch_id'], 'operations': [{
            'operation_id': 'synthetic-process:task', 'action': 'create', 'message_keys': [item['message_key']],
            'fields': {'title': '合成中文作业', 'note': '通过真正的UTF-8 stdin提交'}}],
            'results': [{'message_key': item['message_key'], 'content_hash': item['content_hash'], 'status': 'done'}]}
        first = self.cli('write', '--input', '-', '--timings', payload=payload)
        replay = self.cli('write', '--input', '-', payload=payload)
        self.assertFalse(first['has_failures'])
        self.assertTrue(replay['operations'][0]['replayed'])
        task_id = first['operations'][0]['id']
        tasks = self.cli('read', '--id', task_id)
        self.assertEqual(tasks['total'], 1)
        self.assertEqual(tasks['tasks'][0]['title'], '合成中文作业')
        self.assertEqual(tasks['tasks'][0]['done'], 0)
        self.assertEqual(self.cli('read', '--group', self.group['key'])['messages'], [])
        self.assertFalse((self.root / 'state/agent-inputs').exists())

    def test_images_pagination_cache_and_one_write_across_real_processes(self):
        config = AgentConfig(self.root)
        self.group = config.apply_groups({'expected_revision': config.load()['revision'], 'confirmed': True,
                                          'groups': [{**self.group, 'grant': {'text_to_agent': True, 'images_to_agent': True,
                                                                            'documents_to_agent': False}}]})['groups'][0]
        image, file = self.root / 'synthetic.png', self.root / 'notices.json'
        image.write_bytes(synthetic_png())
        file.write_text(json.dumps({'messages': [{'id': 'image', 'timestamp': 1, 'text': '合成图片通知', 'attachments': ['synthetic.png']},
                                                 {'id': 'text', 'timestamp': 2, 'text': '合成补充通知'}]}, ensure_ascii=False), encoding='utf-8')
        first = self.cli('read', '--file', str(file), '--group', self.group['key'], '--confirm', '--limit', '1', '--timings')
        self.assertEqual(first['attachments'][0]['status'], 'prepared')
        self.assertEqual(first['messages'][0]['attachment_state'], 'prepared')
        prepared = Path(first['attachments'][0]['items'][0]['local_path'])
        self.assertEqual(prepared.read_bytes(), synthetic_png())
        self.assertTrue(prepared.is_relative_to(self.root))
        # Original input files need not exist when continuing an existing batch.
        image.unlink()
        file.unlink()
        cached = self.cli('read', '--batch', first['batch_id'], '--limit', '1')
        self.assertTrue(cached['attachments'][0]['cached'])
        second = self.cli('read', '--batch', first['batch_id'], '--cursor', str(first['next_cursor']), '--limit', '1')
        self.assertIsNone(second['next_cursor'])
        self.assertEqual(second['attachments'], [])
        items = first['messages'] + second['messages']
        result = self.cli('write', '--input', '-', payload={
            'batch_id': first['batch_id'],
            'operations': [{'operation_id': 'synthetic:combined', 'action': 'create',
                            'message_keys': [r['message_key'] for r in items], 'fields': {'title': '合成图片与文字事项'}}],
            'results': [{'message_key': r['message_key'], 'content_hash': r['content_hash'], 'status': 'done'} for r in items]})
        self.assertFalse(result['has_failures'])
        self.assertTrue(all(r['status'] == 'done' for r in result['results']))
        self.assertEqual(self.cli('read', '--group', self.group['key'])['messages'], [])
        self.assertEqual(self.cli('read', '--todos')['total'], 1)
        self.assertFalse((self.root / 'state/agent-inputs').exists())

    def test_revocation_blocks_old_batch_in_another_process(self):
        file = self.root / 'notice.txt'
        file.write_text('仅用于撤权验收的合成通知', encoding='utf-8')
        page = self.cli('read', '--file', str(file), '--group', self.group['key'], '--confirm')
        self.cli('groups', 'remove', '--input', '-', payload={
            'expected_revision': AgentConfig(self.root).load()['revision'], 'confirmed': True, 'group_keys': [self.group['key']]})
        error = self.cli('read', '--batch', page['batch_id'], expected=2)
        self.assertEqual(error['code'], 'scope_denied')
        item = page['messages'][0]
        error = self.cli('write', '--input', '-', payload={'batch_id': page['batch_id'],
            'operations': [{'operation_id': 'revoked', 'action': 'create', 'message_keys': [item['message_key']],
                            'fields': {'title': '不得创建'}}],
            'results': [{'message_key': item['message_key'], 'content_hash': item['content_hash'], 'status': 'done'}]}, expected=2)
        self.assertEqual(error['code'], 'scope_denied')
        self.assertEqual(self.cli('read', '--todos')['tasks'], [])

    def test_invalid_stdin_does_not_create_a_todo(self):
        error = self.cli('write', '--input', '-', payload={'operations': [], 'typo': True}, expected=2)
        self.assertEqual(error['code'], 'invalid_input')
        self.assertEqual(self.cli('read', '--todos')['tasks'], [])


if __name__ == '__main__':
    unittest.main()
