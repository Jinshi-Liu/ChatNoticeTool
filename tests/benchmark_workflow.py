"""Repeatable synthetic CLI benchmark; never opens personal config or chats.

Run: python -B -X utf8 tests/benchmark_workflow.py --runs 5
Source queries are simulated. Actual CLI dispatch, image discovery/conversion,
SQLite persistence, stdin write and replay are exercised in a temporary project.
These timings exclude model/image inference, shell startup and real DB decrypt.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import hashlib
import io
import json
from pathlib import Path
import statistics
import sys
import time
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'tests')]
import agent_cli
from agent_config import encode
from agent_processing import ProcessingStore
from todo_store import TodoStore
from test_agent import NOW
from test_fast_workflow import WeChatViewsTests, png


def trial(mode):
    fixture = WeChatViewsTests('test_plain_quote_is_not_a_pending_attachment')
    fixture.setUp()
    try:
        data = []
        for index in range(4):
            digest = hashlib.md5(f'benchmark-image-{index}'.encode()).hexdigest()
            folder = Path(fixture.source['paths']['account_dir']) / 'msg/attach' / hashlib.md5(fixture.group['group_id'].encode()).hexdigest() / '2030-01/Img'
            folder.mkdir(parents=True, exist_ok=True)
            (folder / (digest + '.dat')).write_bytes(png())
            xml = '<msg><img md5="' + digest + '" cdnmidimgurl="' + 'transport' * 500 + '"/></msg>'
            data.append(fixture.wx(index + 1, xml, 'image', time='2030-01-01T00:00:00+08:00', timestamp=NOW - 100 + index))
        data.append(fixture.wx(5, fixture.quote(title='提交位置说明', reference_type='3', server='1'), timestamp=NOW - 50))
        data.append({**fixture.wx(6, '合成作业：下次上课前提交', 'text'), 'attachment_content_status': 'not_applicable'})
        fixture.data = data
        counts = {'processing_stores': 0, 'todo_stores': 0, 'sqlite_connections': 0, 'cli_calls': 0}
        processing_init, todo_init = ProcessingStore.__init__, TodoStore.__init__
        processing_connection, todo_connection = ProcessingStore.connection, TodoStore.connection

        def init_processing(store, *args, **kwargs):
            counts['processing_stores'] += 1
            processing_init(store, *args, **kwargs)

        def init_todo(store, *args, **kwargs):
            counts['todo_stores'] += 1
            todo_init(store, *args, **kwargs)

        def counted_connection(original):
            @contextmanager
            def connection(store):
                counts['sqlite_connections'] += 1
                with original(store) as db:
                    yield db
            return connection

        def cli(argv, payload=None):
            counts['cli_calls'] += 1
            output = io.StringIO()
            with patch('sys.stdout', output), patch('sys.stdin', io.StringIO(encode(payload) if payload is not None else '')):
                status = agent_cli.main(argv, service=fixture.service)
            result = json.loads(output.getvalue())
            if status or not result['ok']:
                raise RuntimeError(result)
            return result['data']

        with patch.object(ProcessingStore, '__init__', init_processing), patch.object(TodoStore, '__init__', init_todo), \
                patch.object(ProcessingStore, 'connection', counted_connection(processing_connection)), \
                patch.object(TodoStore, 'connection', counted_connection(todo_connection)), \
                patch('attachments.wx_image_key', return_value=(None, None)):
            started = time.perf_counter()
            page = cli(['read', '--timings'] + (['--no-prepare-images'] if mode == 'separate' else []))
            images = page.get('attachments', [])
            if mode == 'separate':
                keys = [r['message_key'] for r in page['messages'] if r['message'].get('has_media')]
                images = cli(['attachments', 'prepare', '--input', '-', '--timings'], {'batch_id': page['batch_id'], 'message_keys': keys})['results']
            if sum(r['status'] == 'prepared' for r in images) != 5:
                raise AssertionError('All four images and the same-group quote must be prepared')
            operations = [{'operation_id': 'benchmark:homework', 'action': 'create',
                           'message_keys': [r['message_key'] for r in page['messages']],
                           'fields': {'title': '合成作业', 'note': '仅用于合成基准，不是真实待办'}}]
            results = [{'message_key': r['message_key'], 'content_hash': r['content_hash'], 'status': 'done',
                        'text_state': 'analyzed', 'attachment_state': 'analyzed' if r['message'].get('has_media') else 'not_applicable'}
                       for r in page['messages']]
            if mode == 'separate':
                applied = cli(['todos', 'apply', '--input', '-'], {'batch_id': page['batch_id'], 'operations': operations})
                if not all(r['ok'] for r in applied['results']):
                    raise AssertionError(applied)
                for row in results:
                    row['task_ids'] = [r['id'] for r in applied['results']]
                cli(['processing', 'commit', '--input', '-'], {'batch_id': page['batch_id'], 'results': results})
            else:
                result = cli(['write', '--input', '-', '--timings'], {'batch_id': page['batch_id'], 'operations': operations, 'results': results})
                if result['has_failures']:
                    raise AssertionError(result)
            elapsed = (time.perf_counter() - started) * 1000
        # Verify completion without including verification in the timing/counts.
        if fixture.service.read()['messages'] or len(fixture.service.todo_store().tasks()) != 1:
            raise AssertionError('Completion or deduplication failed')
        return {**counts, 'elapsed_ms': round(elapsed, 3), 'read_response_bytes': len(encode(page).encode('utf-8')),
                'source_messages_bytes': len(encode(data).encode('utf-8'))}
    finally:
        fixture.tearDown()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--runs', type=int, default=5)
    args = parser.parse_args()
    if not 1 <= args.runs <= 100:
        parser.error('--runs must be 1..100')
    reports = {}
    for mode in ('separate', 'combined'):
        runs = [trial(mode) for _ in range(args.runs)]
        reports[mode] = {**runs[-1], 'median_ms': round(statistics.median(r['elapsed_ms'] for r in runs), 3),
                         'min_ms': min(r['elapsed_ms'] for r in runs), 'max_ms': max(r['elapsed_ms'] for r in runs)}
    print(json.dumps({'synthetic_only': True, 'runs': args.runs,
                      'excludes': ['real_chat_database_decryption', 'model_inference', 'shell_startup'],
                      'results': reports}, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
