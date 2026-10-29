"""Daily Agent CLI: read authorized content, write todos and processing results."""
from __future__ import annotations

from agent_service import AgentService
from cli_support import Parser, input_payload, run


def build_parser():
    parser = Parser(description='日常仅read批量读取、write批量写入；配置和取钥使用agent_setup.py。')
    commands = parser.add_subparsers(dest='command', required=True)
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
    read.add_argument('--offset', type=int)
    read.add_argument('--chars', type=int, default=8000)
    read.add_argument('--from-export')
    read.add_argument('--timings', action='store_true')
    read.add_argument('--no-prepare-images', dest='prepare_images', action='store_false', help='关闭本页默认图片准备')
    read.add_argument('--attachments', action='store_true', help='准备本页所有已授权附件；与--batch --message组合只准备该消息附件')
    read.add_argument('--raw', action='store_true', help='与--batch --message组合，按需读取完整脱敏原文')
    read.add_argument('--reprocess', action='store_true', help='显式重分析本次范围内已处理消息，不清除待办关联')
    write = commands.add_parser('write', help='批量写待办和处理结果；失败项保持pending，可幂等重试')
    write.add_argument('--input', required=True, help='推荐-，直接从标准输入传JSON，不写临时请求文件')
    write.add_argument('--timings', action='store_true')
    return parser


def dispatch(args, service):
    if args.command == 'write':
        return service.write(input_payload(args.input), timings=args.timings)
    kwargs = vars(args).copy()
    kwargs.pop('command')
    return service.read(**kwargs)


def main(argv=None, *, service=None):
    def action():
        args = build_parser().parse_args(argv)
        return dispatch(args, service or AgentService())
    return run(action)


if __name__ == '__main__':
    raise SystemExit(main())
