"""Runnable, trusted-code adapter example; not a DingTalk/other vendor API.

Use this launcher for every CLI operation while trying the adapter:
  python examples/adapters/jsonl_adapter.py --help
  python examples/adapters/jsonl_adapter.py inspect

For permanent integration explicitly register this factory in agent_sources.py.
No modules are discovered or executed from message content or configuration.
"""
from __future__ import annotations

import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent_config import AgentError
from agent_imports import ImportSource, MAX_FILE, MAX_MESSAGES
from agent_sources import register_source


class ExampleJSONLSource(ImportSource):
    """Map another file schema into the shared import/authorization pipeline."""
    platform = 'example_jsonl'
    formats = ('auto', 'jsonl')

    def load_rows(self, path, format):
        if format not in self.formats:
            raise AgentError('unsupported_import_format', '此示例只接受JSONL')
        rows, size = [], 0
        with path.open('rb') as stream:
            while line := stream.readline(MAX_FILE + 1):
                size += len(line)
                if size > MAX_FILE:
                    raise AgentError('input_too_large', 'JSONL超过32MiB，请分批')
                if not line.strip():
                    continue
                try:
                    value = json.loads(line.decode('utf-8-sig'))
                except (ValueError, UnicodeError):
                    raise AgentError('invalid_json', 'JSONL中有无效记录，不回显正文') from None
                if not isinstance(value, dict) or set(value) - {'event_id', 'sent_at', 'body', 'files'}:
                    raise AgentError('invalid_input', '示例记录支持event_id、sent_at、body、files')
                row = {'text': value.get('body', ''), 'attachments': value.get('files', [])}
                if 'event_id' in value:
                    row['id'] = value['event_id']
                if 'sent_at' in value:
                    row['timestamp'] = value['sent_at']
                rows.append(row)
                if len(rows) > MAX_MESSAGES:
                    raise AgentError('input_too_large', 'JSONL消息过多，请分批')
        return rows


def register():
    register_source(ExampleJSONLSource.platform, ExampleJSONLSource)


def main(argv=None, *, service=None):
    register()
    from agent_cli import main as cli_main
    return cli_main(argv, service=service)


if __name__ == '__main__':
    raise SystemExit(main())
