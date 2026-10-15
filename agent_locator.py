"""Locate this component and its Python, independent of the working directory.

Optional --remember stores only root/Python in private state/component-location.json.
No chat, credential or directory-search operations are performed.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys

from agent_config import atomic_json, private_path, read_json

ROOT = Path(__file__).resolve().parent


def locate(root=None, python=None, remember=False):
    project = Path(root or os.environ.get('CHATNOTICE_HOME') or ROOT).expanduser()
    if not project.is_absolute():
        raise ValueError('请提供组件绝对目录')
    project = project.absolute()
    connection = private_path(project, 'state/component-location.json')
    if not all((project / name).is_file() for name in ('agent_cli.py', 'todo_store.py', 'config.example.json')):
        raise ValueError('该位置不是组件目录；请提供准确位置，不要扫描磁盘')
    saved = read_json(connection, 16384) if connection.is_file() else {}
    if not isinstance(saved, dict) or (saved and saved.get('schema_version') != 1):
        raise ValueError('组件定位记录版本不兼容')
    executable = python
    if not executable and saved.get('root') == str(project):
        executable = saved.get('python')
    if not executable:
        executable = next((str(p) for p in (project / '.venv/Scripts/python.exe', project / '.venv/bin/python')
                           if p.is_file()), sys.executable)
    executable = Path(executable).expanduser()
    if not executable.is_absolute() or not executable.is_file():
        raise ValueError('Python不存在，请用--python指定现有解释器的绝对路径')
    value = {'schema_version': 1, 'root': str(project), 'python': str(executable.absolute())}
    if remember:
        atomic_json(connection, value)
    return {**value, 'argv_prefix': [value['python'], '-B', '-X', 'utf8', str(project / 'agent_cli.py')],
            'read_chat_bodies': False, 'remembered': remember}


def main(argv=None):
    parser = argparse.ArgumentParser(description='定位ChatNoticeTool，只检查组件和Python位置')
    parser.add_argument('--root')
    parser.add_argument('--python')
    parser.add_argument('--remember', action='store_true')
    args = parser.parse_args(argv)
    try:
        data = locate(args.root, args.python, args.remember)
        print(json.dumps({'schema_version': 1, 'ok': True, 'data': data}, ensure_ascii=False))
        return 0
    except (OSError, ValueError, TypeError):
        print(json.dumps({'schema_version': 1, 'ok': False, 'error': {
            'code': 'component_location_required',
            'message': '请提供现有组件和Python的绝对路径；不会扫描磁盘或自动安装软件'}}, ensure_ascii=False))
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
