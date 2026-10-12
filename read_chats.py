"""One-shot, explicitly scoped local chat reader; no monitoring or key scanning.
Optional reuse is explicit and reports its historical snapshot time. It never
silently treats old cached data as a fresh read or broadens a requested scope.
"""
from __future__ import annotations
import argparse
from collections import Counter
from dataclasses import asdict
from datetime import datetime
import json
from pathlib import Path
import sys
import time

from chatnotice import ROOT, LOCAL_TZ, ReadScope, parse_bound, project_path, save_json
from messages import export_request

MAX_CACHED_JSON = 32 * 1024 * 1024


def make_request(platform, group_ids, since, until, confirmed):
    if not confirmed:
        raise PermissionError('请核对群和日期并添加 --confirm；未读取消息')
    if until == 'now':
        until = datetime.now(LOCAL_TZ).replace(microsecond=0).isoformat()
    scope = ReadScope(platform, tuple(group_ids), parse_bound(since), parse_bound(until, end=True))
    return {'confirmed': True, 'since': since, 'until': until,
            'scopes': [asdict(scope)],
            'groups': [{'platform': platform, 'group_id': group, 'name': group} for group in scope.group_ids]}


def _scope(request):
    value = request['scopes'][0]
    return ReadScope(value['platform'], tuple(value['group_ids']), value['since'], value['until'])


def _json_file(path):
    if path.stat().st_size > MAX_CACHED_JSON:
        raise ValueError('导出JSON超过32MiB读取上限；请分段读取，不会静默截断')
    return json.loads(path.read_text(encoding='utf-8'))


def _load_config(platform):
    path = project_path('config.json')
    if not path.is_file():
        raise ValueError('缺少 config.json；请按 README 初始化并配置自己的路径')
    config = _json_file(path)
    fields = ('database_dir',) if platform == 'qq' else ('database_dir', 'account_dir')
    for field in fields:
        value = config.get(platform, {}).get(field)
        if not isinstance(value, str) or not value or not Path(value).is_absolute():
            raise ValueError(f'请配置 {platform}.{field} 为自己的绝对路径')
    return config


def _cached_selection(request, export_dir):
    candidate = Path(export_dir)
    if candidate.is_absolute():
        try: candidate = candidate.relative_to(ROOT)
        except ValueError: raise ValueError('缓存必须位于本项目 exports 目录内')
    if not candidate.parts or candidate.parts[0] != 'exports':
        raise ValueError('--from-export 只能指定本项目 exports 下的导出目录')
    output = project_path(str(candidate))
    if not output.resolve().is_relative_to((ROOT / 'exports').resolve()):
        raise ValueError('缓存路径不能离开 exports 目录')
    report = _json_file(project_path(str(candidate / 'messages.json')))
    if not report.get('request', {}).get('confirmed'):
        raise ValueError('缓存没有明确授权的读取范围，拒绝复用')
    wanted = _scope(request)
    cached = {}
    for value in report['request']['scopes']:
        scope = ReadScope(value['platform'], tuple(value['group_ids']), value['since'], value['until'])
        if scope.platform in cached: raise ValueError('缓存范围包含重复平台')
        cached[scope.platform] = scope
    available = cached.get(wanted.platform)
    if (available is None or not set(wanted.group_ids).issubset(available.group_ids)
            or wanted.since < available.since or wanted.until > available.until):
        raise ValueError('缓存不覆盖所要求的群和完整时间范围；请去掉 --from-export 重新读取')
    selected = []
    for message in report['messages']:
        scope = cached.get(message['platform'])
        if scope is None or not scope.accepts(message['platform'], message['group_id'], message['timestamp']):
            raise ValueError('缓存包含超出其声明授权范围的消息，拒绝复用')
        if wanted.accepts(message['platform'], message['group_id'], message['timestamp']):
            selected.append(message)
    names = {(g['platform'], g['group_id']): g['name'] for g in report['request']['groups']}
    counts = Counter(m['group_id'] for m in selected)
    return {'request': request, 'output_dir': str(output),
            'message_count': len(selected),
            'groups': [{'platform': wanted.platform, 'group_id': group,
                        'name': names.get((wanted.platform, group), group), 'message_count': counts[group]}
                       for group in wanted.group_ids],
            'body_status_counts': dict(Counter(m['body_status'] for m in selected)),
            'used_cached_export': True, 'read_client_database': False,
            'cache_snapshot_until': report['request']['until'],
            'cache_notice': '仅复用已有历史导出，未再次检查客户端数据库；不是最新同步结果',
            'messages': selected, 'local_records_only': True,
            'all_attachment_contents_processed': False,
            'file_notice': '源文件可能包含原导出的其他已授权群；本次终端消息仅包含指定群和时间'}


def read_range(request, *, from_export=None, show=False):
    if not request.get('confirmed'): raise PermissionError('没有确认读取范围')
    scope = _scope(request)
    started = time.monotonic()
    if from_export:
        result = _cached_selection(request, from_export)
    else:
        config = _load_config(scope.platform)
        save_json('state/tool-read-request.json', request)
        result = export_request(config, request)
        result.update(used_cached_export=False, read_client_database=True)
        if show:
            relative = Path(result['output_dir']).relative_to(ROOT) / 'messages.json'
            result['messages'] = _json_file(project_path(str(relative)))['messages']
    result['elapsed_seconds'] = round(time.monotonic() - started, 3)
    result['scope'] = asdict(scope)
    result['attachment_notice'] = '正文已按已知格式解析；图片、语音、文件等内容不保证已处理，本地缺失不能恢复'
    if not show: result.pop('messages', None)
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description='按指定群和日期一次性读取本机消息；默认不扫描内存、不处理附件、不调用AI')
    parser.add_argument('--platform', required=True, choices=['qq', 'wechat'])
    parser.add_argument('--group-id', action='append', required=True, help='准确群ID，可重复；不接受模糊群名')
    parser.add_argument('--since', required=True, help='YYYY-MM-DD或ISO时间，默认UTC+8')
    parser.add_argument('--until', required=True, help='日期包含整天；时间为不含上界；now表示本次调用时刻')
    parser.add_argument('--confirm', action='store_true', help='明确确认有权读取指定群和时间')
    parser.add_argument('--from-export', help='显式复用本项目已有导出，如 exports/20301007-180000；不会再次读客户端')
    parser.add_argument('--show', action='store_true', help='在JSON输出中直接返回所选消息，便于助手一次调用分析')
    args = parser.parse_args(argv)
    try:
        request = make_request(args.platform, args.group_id, args.since, args.until, args.confirm)
        result = read_range(request, from_export=args.from_export, show=args.show)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (OSError, ValueError, KeyError, RuntimeError, PermissionError) as exc:
        print(json.dumps({'ok': False, 'error': str(exc),
                          'notice': '失败不能当成没有消息；没有自动扩大范围或重新扫描内存'},
                         ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
