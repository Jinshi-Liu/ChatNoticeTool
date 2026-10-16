"""Stable, compact JSON CLI for a project-rule-driven external Agent.

No model calls, no background collection, no implicit memory scan. Ordinary
reads use explicitly saved group grants; legacy commands keep --confirm.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sqlite3
import sys

from agent_config import AgentError, MAX_INPUT, encode, read_json
from agent_service import AgentService
from agent_sources import candidates


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


def build_parser():
    parser = Parser(description='日常仅read批量读取、write批量写入；图片由当前Agent理解。',
                    epilog='首次配置和旧分步命令保留兼容，按docs/agent/commands.md调用，不加入日常链路。')
    commands = parser.add_subparsers(dest='command', required=True, metavar='{read,write}')
    commands.add_parser('inspect')
    commands.add_parser('setup')
    source = commands.add_parser('source').add_subparsers(dest='action', required=True)
    candidate = source.add_parser('candidates')
    candidate.add_argument('--platform', choices=['qq', 'wechat', 'dingtalk'], required=True)
    candidate.add_argument('--base', help='只在指定绝对目录下探测已知相对布局')
    configure = source.add_parser('configure')
    configure.add_argument('--input', required=True, help='UTF-8 JSON路径；-表示标准输入')
    for action in ('check', 'connect'):
        sub = source.add_parser(action)
        sub.add_argument('--source', required=True)
        if action == 'connect':
            sub.add_argument('--pid', type=int, action='append', required=True)
            sub.add_argument('--authorize-memory-read', action='store_true')
    groups = commands.add_parser('groups').add_subparsers(dest='action', required=True)
    listing = groups.add_parser('list')
    listing.add_argument('--source', required=True)
    listing.add_argument('--contains', default='')
    listing.add_argument('--offset', type=int, default=0)
    listing.add_argument('--limit', type=int, default=50)
    for action in ('plan', 'apply', 'remove'):
        groups.add_parser(action).add_argument('--input', required=True)
    importing = commands.add_parser('import')
    importing.add_argument('--group', dest='group_key', required=True)
    importing.add_argument('--file', required=True, help='用户明确选择的绝对文件路径')
    importing.add_argument('--format', default='auto', help='默认auto；其他格式由已注册适配器声明')
    importing.add_argument('--timestamp', help='缺少消息时间时使用的ISO时间；默认首次导入时刻')
    importing.add_argument('--confirm', dest='confirmed', action='store_true')
    importing.add_argument('--limit', type=int, default=50)
    importing.add_argument('--timings', action='store_true')
    importing.add_argument('--prepare-images', action='store_true', help='只准备本页已授权图片，不扫描内存')
    read = commands.add_parser('read', help='批量读消息和图片；--todos读待办；--batch继续原批次')
    read.add_argument('--file', help='读取明确选择的本地通知文件；需--group及--confirm')
    read.add_argument('--format', default='auto', help='只用于--file，默认自动识别')
    read.add_argument('--timestamp', help='只用于--file，无原时间时的明确缺省时间')
    read.add_argument('--confirm', dest='confirmed', action='store_true', help='确认允许处理所选--file')
    read.add_argument('--todos', action='store_true', help='读取组件待办，不读聊天')
    read.add_argument('--id', dest='ids', action='append', help='批量读取指定待办ID，可重复；包含已移除项')
    read.add_argument('--all', dest='include_removed', action='store_true', help='列待办时包含已移除项')
    read.add_argument('--group', dest='group_keys', action='append')
    read.add_argument('--since')
    read.add_argument('--until', default='now')
    read.add_argument('--batch', dest='batch_id')
    read.add_argument('--cursor', type=int, default=0)
    read.add_argument('--limit', type=int, default=50)
    read.add_argument('--message')
    read.add_argument('--offset', type=int, default=0)
    read.add_argument('--chars', type=int, default=8000)
    read.add_argument('--from-export')
    read.add_argument('--timings', action='store_true')
    read.add_argument('--prepare-images', action='store_true', default=True, help=argparse.SUPPRESS)
    read.add_argument('--no-prepare-images', dest='prepare_images', action='store_false', help='仅在不需要图片时关闭本页图片准备')
    read.add_argument('--raw', action='store_true', help='与--batch --message组合，按需读取完整脱敏原文')
    read.add_argument('--reprocess', action='store_true', help='用户显式要求时重分析本次范围内已处理消息，不清除待办关联')
    attach = commands.add_parser('attachments').add_subparsers(dest='action', required=True).add_parser('prepare')
    attach.add_argument('--input', required=True)
    attach.add_argument('--pid', type=int, default=0)
    attach.add_argument('--authorize-memory-read', action='store_true')
    attach.add_argument('--timings', action='store_true')
    todos = commands.add_parser('todos').add_subparsers(dest='action', required=True)
    listing = todos.add_parser('list')
    listing.add_argument('--offset', type=int, default=0)
    listing.add_argument('--limit', type=int, default=50)
    listing.add_argument('--all', action='store_true')
    todos.add_parser('get').add_argument('--id', required=True)
    apply = todos.add_parser('apply')
    apply.add_argument('--input', required=True)
    apply.add_argument('--timings', action='store_true')
    write = commands.add_parser('write', help='批量写待办和处理结果；失败项保持pending，可幂等重试')
    write.add_argument('--input', required=True, help='推荐-，直接从标准输入传JSON，不写临时请求文件')
    write.add_argument('--timings', action='store_true')
    processing = commands.add_parser('processing').add_subparsers(dest='action', required=True)
    pending = processing.add_parser('pending')
    pending.add_argument('--group', dest='group_keys', action='append')
    commit = processing.add_parser('commit')
    commit.add_argument('--input', required=True)
    commit.add_argument('--timings', action='store_true')
    return parser


def dispatch(args, service):
    if args.command == 'inspect':
        return service.inspect()
    if args.command == 'setup':
        return service.setup()
    if args.command == 'source':
        if args.action == 'candidates':
            return candidates(args.platform, args.base)
        if args.action == 'configure':
            return service.config.configure_source(input_payload(args.input))
        return service.source_action(args.source, args.action,
                                     pids=getattr(args, 'pid', None), authorized=getattr(args, 'authorize_memory_read', False))
    if args.command == 'groups':
        if args.action == 'list':
            return service.source_action(args.source, 'groups', contains=args.contains, offset=args.offset, limit=args.limit)
        payload = input_payload(args.input)
        return {'plan': service.config.plan_groups, 'apply': service.config.apply_groups,
                'remove': service.config.remove_groups}[args.action](payload)
    if args.command == 'import':
        kwargs = vars(args).copy()
        kwargs.pop('command')
        return service.import_file(**kwargs)
    if args.command == 'read':
        kwargs = vars(args).copy()
        kwargs.pop('command')
        return service.read(**kwargs)
    if args.command == 'attachments':
        return service.prepare(input_payload(args.input), pid=args.pid, authorize_memory_read=args.authorize_memory_read, timings=args.timings)
    if args.command == 'todos':
        if args.action == 'list':
            return service.todo_store().page(args.offset, args.limit, args.all)
        if args.action == 'get':
            return service.todo_store().get(args.id)
        return service.todos_apply(input_payload(args.input), timings=args.timings)
    if args.command == 'write':
        return service.write(input_payload(args.input), timings=args.timings)
    if args.action == 'pending':
        from agent_processing import ProcessingStore
        return ProcessingStore(service.root).pending(args.group_keys)
    return service.commit(input_payload(args.input), timings=args.timings)


def main(argv=None, *, service=None):
    try:
        args = build_parser().parse_args(argv)
        data = dispatch(args, service or AgentService())
        print(encode({'schema_version': 1, 'ok': True, 'data': data}))
        return 0
    except AgentError as exc:
        error = {'code': exc.code, 'message': str(exc), 'next_actions': exc.next_actions}
    except ImportError:
        error = {'code': 'dependency_missing', 'message': '当前项目Python缺少聊天/附件依赖，请按requirements.txt安装', 'next_actions': ['inspect']}
    except (OSError, ValueError, RuntimeError, sqlite3.Error) as exc:
        message = str(exc)
        if 'key' in message.lower() or 'dpapi' in message.lower():
            code = 'credentials_unavailable'
            message = '本地凭据缺失、失效或不可解封；未自动扫描内存'
            actions = ['source check', '取得用户单次授权后source connect']
        else:
            code, actions = 'operation_failed', ['inspect', '查阅docs/agent/troubleshooting.md']
            # Parser/OS diagnostics may contain private paths, but never include
            # decrypted message text, input documents or arbitrary exception repr.
            message = '操作失败；请检查路径、输入格式、客户端版本或本地资源状态'
        error = {'code': code, 'message': message, 'next_actions': actions}
    except (TypeError, KeyError, AttributeError, OverflowError):
        error = {'code': 'invalid_input', 'message': '输入字段缺失或类型错误', 'next_actions': ['查阅docs/agent/commands.md']}
    print(encode({'schema_version': 1, 'ok': False, 'error': error}))
    return 2


if __name__ == '__main__':
    raise SystemExit(main())
