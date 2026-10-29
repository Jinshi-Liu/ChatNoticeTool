"""Shared bounded CLI input and compact, redacted JSON error responses."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sqlite3
import sys

from agent_config import AgentError, MAX_INPUT, encode, read_json


class Parser(argparse.ArgumentParser):
    def error(self, message):
        raise AgentError('invalid_arguments', '命令或参数无效；使用--help查看接口，不会回显原始输入', ['--help'])


def input_payload(name):
    if name == '-':
        data = sys.stdin.buffer.read(MAX_INPUT + 1) if hasattr(sys.stdin, 'buffer') else sys.stdin.read(MAX_INPUT + 1).encode('utf-8')
        if len(data) > MAX_INPUT:
            raise AgentError('input_too_large', 'JSON超过1MiB，请分批提交')
        try:
            value = json.loads(data.decode('utf-8-sig'))
        except (ValueError, UnicodeError):
            raise AgentError('invalid_json', '输入必须是UTF-8 JSON') from None
    else:
        value = read_json(Path(name))
    if not isinstance(value, dict):
        raise AgentError('invalid_input', '输入JSON必须是对象')
    return value


def run(action):
    try:
        data = action()
        print(encode({'schema_version': 1, 'ok': True, 'data': data}))
        return 0
    except AgentError as exc:
        error = {'code': exc.code, 'message': str(exc), 'next_actions': exc.next_actions}
    except ImportError:
        error = {'code': 'dependency_missing', 'message': '当前项目Python缺少聊天/附件依赖，请按requirements.txt安装',
                 'next_actions': ['python agent_setup.py inspect']}
    except (OSError, ValueError, RuntimeError, sqlite3.Error) as exc:
        if 'key' in str(exc).lower() or 'dpapi' in str(exc).lower():
            code = 'credentials_unavailable'
            message = '本地凭据缺失、失效或不可解封；未自动扫描内存'
            actions = ['python agent_setup.py source check --source <ID>',
                       'python agent_setup.py source connect --source <ID> --pid <本人客户端PID> --authorize-memory-read']
        else:
            code = 'operation_failed'
            message = '操作失败；请检查路径、输入格式、客户端版本或本地资源状态'
            actions = ['python agent_setup.py inspect', '查阅docs/agent/troubleshooting.md']
        error = {'code': code, 'message': message, 'next_actions': actions}
    except (TypeError, KeyError, AttributeError, OverflowError):
        error = {'code': 'invalid_input', 'message': '输入字段缺失或类型错误', 'next_actions': ['查阅docs/agent/commands.md']}
    print(encode({'schema_version': 1, 'ok': False, 'error': error}))
    return 2
