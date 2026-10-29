"""Explicit configuration and one-shot key acquisition, outside the daily CLI."""
from __future__ import annotations

from pathlib import Path
import sys

from agent_config import AgentConfig, AgentError, encode, private_path
from agent_sources import SOURCE_FACTORIES, candidates, get_source
from cli_support import Parser, input_payload, run
from project_core import ROOT


class SetupService:
    def __init__(self, root=ROOT):
        self.root = Path(root).absolute()
        self.config = AgentConfig(self.root)

    def inspect(self):
        value = self.config.load()
        return {'root': str(self.root), 'python': str(Path(sys.executable).absolute()),
                'initialized': private_path(self.root, 'config.json').is_file(),
                'revision': value['revision'], 'sources': list(value['sources'].values()),
                'groups': list(value['groups'].values()),
                'capabilities': [get_source(p).capabilities for p in SOURCE_FACTORIES],
                'key_cache_present': private_path(self.root, 'state/secrets.dpapi').is_file(),
                'key_cache_verified': False, 'read_chat_bodies': False,
                'next_actions': ['python agent_cli.py read'] if any(g['enabled'] for g in value['groups'].values()) else [
                    'python agent_setup.py source configure --input -',
                    'python agent_setup.py groups list --source <ID>',
                    'python agent_setup.py groups plan --input -']}

    def init(self):
        from local_setup import initialize
        created = initialize(self.root)
        return {'created': created, **self.inspect()}

    def source_action(self, source_id, action, **kwargs):
        source = self.config.source(source_id)
        backend = get_source(source['platform'])
        config = self.config.client_config(source)
        if action == 'check':
            report = backend.check(config)
            if report.get('next_actions'):
                report['next_actions'] = [f'python agent_setup.py source connect --source {source_id} --pid <本人客户端PID> --authorize-memory-read']
            return report
        if action == 'connect':
            return backend.connect(config, kwargs.get('pids'), kwargs.get('authorized'))
        if action == 'groups':
            report = backend.groups(config)
            search = kwargs.get('contains', '')
            rows = [g for g in report['groups'] if search in g['name'] or search in g.get('remark', '')]
            offset, limit = kwargs.get('offset', 0), kwargs.get('limit', 50)
            if offset < 0 or not 1 <= limit <= 200:
                raise AgentError('invalid_input', 'offset须非负，limit须在1到200之间')
            page, size = [], 0
            for row in rows[offset:offset + limit]:
                length = len(encode(row).encode('utf-8'))
                if page and size + length > 32 * 1024:
                    break
                page.append(row)
                size += length
            end = offset + len(page)
            return {'source_id': source_id, 'groups': page, 'total': len(rows),
                    'next_offset': end if end < len(rows) else None, 'read_chat_bodies': False}
        raise AgentError('invalid_input', '未知来源操作')

    def images(self, batch_id, message_keys, *, pid=0, authorize_memory_read=False, timings=False):
        from agent_assets import prepare
        from agent_processing import ProcessingStore
        return prepare(ProcessingStore(self.root), {'batch_id': batch_id, 'message_keys': message_keys},
                       pid=pid, authorize_memory_read=authorize_memory_read, timings=timings)


def build_parser():
    parser = Parser(description='显式设置来源、分群规则、授权及单次取钥；日常使用agent_cli.py read/write。')
    commands = parser.add_subparsers(dest='command', required=True)
    commands.add_parser('inspect')
    commands.add_parser('init')
    source = commands.add_parser('source').add_subparsers(dest='action', required=True)
    candidate = source.add_parser('candidates')
    candidate.add_argument('--platform', choices=['qq', 'wechat', 'dingtalk'], required=True)
    candidate.add_argument('--base', help='只在指定绝对目录下探测已知相对布局')
    source.add_parser('configure').add_argument('--input', required=True)
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
    images = commands.add_parser('images', help='已交付消息的单次图片取钥；须明确本人客户端PID和内存授权')
    images.add_argument('--batch', dest='batch_id', required=True)
    images.add_argument('--message', dest='message_keys', action='append', required=True)
    images.add_argument('--pid', type=int, default=0)
    images.add_argument('--authorize-memory-read', action='store_true')
    images.add_argument('--timings', action='store_true')
    return parser


def dispatch(args, service):
    if args.command in ('inspect', 'init'):
        return getattr(service, args.command)()
    if args.command == 'images':
        return service.images(args.batch_id, args.message_keys, pid=args.pid,
                              authorize_memory_read=args.authorize_memory_read, timings=args.timings)
    if args.command == 'source':
        if args.action == 'candidates':
            return candidates(args.platform, args.base)
        if args.action == 'configure':
            return service.config.configure_source(input_payload(args.input))
        return service.source_action(args.source, args.action, pids=getattr(args, 'pid', None),
                                     authorized=getattr(args, 'authorize_memory_read', False))
    if args.action == 'list':
        return service.source_action(args.source, 'groups', contains=args.contains, offset=args.offset, limit=args.limit)
    return {'plan': service.config.plan_groups, 'apply': service.config.apply_groups,
            'remove': service.config.remove_groups}[args.action](input_payload(args.input))


def main(argv=None, *, service=None):
    def action():
        args = build_parser().parse_args(argv)
        return dispatch(args, service or SetupService())
    return run(action)


if __name__ == '__main__':
    raise SystemExit(main())
