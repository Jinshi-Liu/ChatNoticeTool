"""On-demand local chat tools: diagnostics, explicit scope, and group metadata.
Authorized key acquisition is separate in memory_reader.py and is read-only.
No network requests, screenshots, sends, or background jobs. Message-content and
attachment processing must be verified after the user selects groups and dates.
"""
from __future__ import annotations
import argparse
import hashlib
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sys
from typing import Any

ROOT = Path(__file__).resolve().parent
LOCAL_TZ = timezone(timedelta(hours=8))
SQLITE_MAGIC = b"SQLite format 3\x00"
# Each installation has its own singleton; a release clone does not block another copy.
WIDGET_MUTEX = 'Local\\ChatNoticeToolTodoWidget-' + hashlib.sha256(str(ROOT).lower().encode()).hexdigest()[:16]


def load_config(platform: str | None = None) -> dict:
    path = project_path('config.json')
    if not path.is_file():
        raise ValueError('请先运行 python local_setup.py，再按 README 配置 config.json')
    config = json.loads(path.read_text(encoding='utf-8'))
    if platform:
        fields = ('database_dir', 'media_dir') if platform == 'qq' else ('database_dir', 'account_dir')
        for field in fields:
            value = config.get(platform, {}).get(field, '')
            if not isinstance(value, str) or not value.strip() or not Path(value).is_absolute():
                raise ValueError(f'请配置 {platform}.{field} 为自己的绝对路径')
    return config


def build_request(platform: str, group_ids: list[str], since: str, until: str) -> dict:
    scope = ReadScope(platform, tuple(group_ids), parse_bound(since), parse_bound(until, end=True))
    return {'confirmed': True, 'since': since, 'until': until,
            'scopes': [asdict(scope)],
            'groups': [{'platform': platform, 'group_id': id, 'name': id} for id in scope.group_ids]}



@dataclass(frozen=True)
class ReadScope:
    platform: str
    group_ids: tuple[str, ...]
    since: int
    until: int  # Exclusive upper bound.
    include_all_message_types: bool = True

    def __post_init__(self) -> None:
        if self.platform not in ("qq", "wechat"):
            raise ValueError("平台只能是 qq 或 wechat")
        if not self.group_ids:
            raise ValueError("必须明确指定群，禁止默认读取全部会话")
        if len(set(self.group_ids)) != len(self.group_ids):
            raise ValueError("群 ID 重复")
        for group in self.group_ids:
            if not isinstance(group, str) or not group or group != group.strip():
                raise ValueError("群 ID 不合法")
            if self.platform == "qq" and not (group.isascii() and group.isdigit() and int(group) > 0):
                raise ValueError("QQ 必须使用准确群号，群名需先解析为群号")
            if self.platform == "wechat" and not group.endswith("@chatroom"):
                raise ValueError("微信必须使用准确群 ID，群名需先解析为 @chatroom ID")
        if type(self.since) is not int or type(self.until) is not int or self.since >= self.until:
            raise ValueError("时间范围不合法，开始必须早于结束")

    def accepts(self, platform: str, group_id: str, timestamp: int) -> bool:
        return (platform == self.platform and group_id in self.group_ids
                and self.since <= timestamp < self.until)


def parse_bound(value: str, *, end: bool = False) -> int:
    try:
        dt = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError("日期格式应为 YYYY-MM-DD 或 ISO 日期时间") from exc
    # A date-only end includes that entire day. A timestamp end is exclusive.
    if len(value) == 10 and end:
        dt += timedelta(days=1)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=LOCAL_TZ)
    return int(dt.timestamp())


def project_path(relative: str) -> Path:
    path = ROOT / relative
    resolved = path.resolve()
    if not resolved.is_relative_to(ROOT) or resolved == ROOT:
        raise ValueError("输出路径必须在项目目录内")
    # Do not write through Windows junctions or symlinks.
    for candidate in (path, *path.parents):
        if candidate == ROOT:
            break
        if candidate.exists() and (candidate.is_symlink() or
                bool(getattr(candidate.lstat(), "st_file_attributes", 0) & 0x400)):
            raise ValueError("输出路径不能包含链接或目录联接")
    return path


def save_json(relative: str, data: Any) -> Path:
    path = project_path(relative)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = project_path(relative + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)
    return path


def database_metadata(path: Path, *, prefix_bytes: int = 0) -> dict:
    info: dict[str, Any] = {"path": str(path), "status": "not_found"}
    if not path.is_file():
        return info
    try:
        before = path.stat()
        with path.open("rb") as handle:
            header = handle.read(16)
            handle.seek(prefix_bytes)
            body_header = handle.read(16)
        after = path.stat()
        info.update({
            "size_bytes": after.st_size,
            "modified": datetime.fromtimestamp(after.st_mtime, LOCAL_TZ).isoformat(),
            "status": "plain_sqlite" if header == SQLITE_MAGIC else "encrypted_or_custom",
            "sqlite_after_prefix": body_header == SQLITE_MAGIC,
            "prefix_bytes_checked": prefix_bytes,
            "page_alignment_4096": max(0, after.st_size - prefix_bytes) % 4096 == 0,
            "changed_during_probe": (before.st_size, before.st_mtime_ns) !=
                                    (after.st_size, after.st_mtime_ns),
            "wal_present": Path(str(path) + "-wal").exists(),
            "read_content": False,
        })
    except OSError as exc:
        info.update(status="access_error", error=str(exc))
    return info


def capabilities() -> dict:
    out = {"message_export_ready": True, "attachments_fully_supported": False,
           "key_cache_present": (ROOT / "state" / "secrets.dpapi").is_file(),
           "read_chat_bodies": False}
    for platform in ("qq", "wechat"):
        key_file = ROOT / "state" / f"{platform}-key-report.json"
        group_file = ROOT / "state" / f"{platform}-groups.json"
        key_report = json.loads(key_file.read_text(encoding="utf-8")) if key_file.exists() else {}
        group_report = json.loads(group_file.read_text(encoding="utf-8")) if group_file.exists() else {}
        out[platform] = {
            "last_verified_database_count": key_report.get("verified_databases", 0),
            "group_metadata_count": len(group_report.get("groups", [])),
            "message_schema_validated": (ROOT / "state" / f"{platform}-message-schema.json").is_file(),
        }
    out["phase"] = ("group_metadata_ready" if out["key_cache_present"] and
                    all(out[p]["group_metadata_count"] for p in ("qq", "wechat")) else "diagnostics")
    return out


def diagnose(config: dict) -> dict:
    qq_value = config.get('qq', {}).get('database_dir', '')
    wx_value = config.get('wechat', {}).get('database_dir', '')
    qq_dir = Path(qq_value) if qq_value else None
    wx_dir = Path(wx_value) if wx_value else None
    wx_files = [wx_dir / 'contact' / 'contact.db'] if wx_dir else []
    if wx_dir and (wx_dir / 'message').is_dir():
        wx_files += sorted((wx_dir / 'message').glob('message_*.db'))
    # Only inspect file metadata and 16-byte format signatures. No SELECTs or keys.
    return {
        "phase": capabilities()["phase"],
        "capabilities": capabilities(),
        "chat_reading_ready": None,  # A metadata probe cannot verify a key or message schema.
        "generated_at": datetime.now(LOCAL_TZ).isoformat(),
        "qq": {"version": config["qq"]["version"], "databases": [
            database_metadata(qq_dir / name, prefix_bytes=1024)
            for name in ("group_info.db", "nt_msg.db", "group_msg_fts.db")] if qq_dir else []},
        "wechat": {"version": config["wechat"]["version"], "databases": [
            database_metadata(path) for path in wx_files]},
        "blockers": [
            "实际查询消息正文前，需确认指定群和日期范围",
            "已实现正文导出，但客户端版本、账号和具体消息类型需逐项验证",
            "图片/语音/文件内容需分别解析；本地未保存的附件不能恢复",
        ],
        "process_memory_read_authorized": bool(config["policy"].get("allow_process_memory_read")),
        "external_system_changes": [],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="ChatNoticeTool：本机授权群消息按需导出，不实时监听")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("doctor", help="检查本地格式，不读聊天正文")
    commands.add_parser("status", help="显示当前阶段和未完成事项")
    group_command = commands.add_parser("groups", help="只读取群名，不查询聊天正文")
    group_command.add_argument("--platform", required=True, choices=["qq", "wechat"])
    group_command.add_argument("--contains", default="")
    scope = commands.add_parser("scope", help="记录一次明确的读取范围，不读取消息")
    scope.add_argument("--platform", required=True, choices=["qq", "wechat"])
    scope.add_argument("--group-id", action="append", required=True)
    scope.add_argument("--since", required=True)
    scope.add_argument("--until", required=True)
    export = commands.add_parser('export', help='导出明确指定的群和时间；需要 --confirm')
    export.add_argument('--platform', required=True, choices=['qq', 'wechat'])
    export.add_argument('--group-id', action='append', required=True)
    export.add_argument('--since', required=True)
    export.add_argument('--until', required=True)
    export.add_argument('--confirm', action='store_true', help='明确确认有权导出这些群的本地消息')
    args = parser.parse_args()
    try:
        if args.command == 'export' and not args.confirm:
            raise PermissionError('请核对群和时间范围并添加 --confirm；未读取消息')
        config = load_config(getattr(args, 'platform', None)) if args.command != 'status' else {}
        if args.command == 'export':
            from messages import export_request
            request = build_request(args.platform, args.group_id, args.since, args.until)
            save_json('state/read-request.json', request)
            print(json.dumps(export_request(config, request), ensure_ascii=False, indent=2))
            return 0
        if args.command == "doctor":
            report = diagnose(config)
            out = save_json("state/doctor.json", report)
            for platform in ("qq", "wechat"):
                print(f"{platform}: {report[platform]['version']}")
                for database in report[platform]["databases"]:
                    print(f"  {database['path']}: {database['status']}")
            print(f"诊断报告：{out}")
            print("本命令仅检查格式和上次验证状态，不查询聊天正文。")
            return 0
        if args.command == "groups":
            from groups import qq_groups, wechat_groups
            report = qq_groups(config) if args.platform == "qq" else wechat_groups(config)
            out = save_json(f"state/{args.platform}-groups.json", report)
            print(f"本地群记录：{len(report['groups'])}；未查询消息正文。")
            for group in report["groups"]:
                if args.contains in group["name"] or args.contains in group["remark"]:
                    print(json.dumps(group, ensure_ascii=False))
            print(f"群名文件：{out}；临时解密副本已删除。")
            return 0
        if args.command == "scope":
            request = ReadScope(args.platform, tuple(args.group_id),
                                parse_bound(args.since), parse_bound(args.until, end=True))
            out = save_json("state/scope.json", asdict(request))
            print(f"范围已记录：{out}；没有读取消息。")
            return 0
        state = capabilities()
        print(f"阶段：{state['phase']}")
        for platform in ("qq", "wechat"):
            print(f"{platform}: 上次验证 {state[platform]['last_verified_database_count']} 个数据库，"
                  f"识别 {state[platform]['group_metadata_count']} 条群记录。")
        print('本命令不读取聊天；正文导出用 export，附件仍有未支持或缺失的类型。')
        print("QQ/微信原始数据未修改；无后台服务；无全局依赖。")
        if (ROOT / "state" / "scope.json").exists():
            print("已记录范围，但尚未读取消息。")
        else:
            print("尚未选择群和日期范围。")
        return 0
    except (OSError, ValueError, KeyError, RuntimeError, PermissionError) as exc:
        print(f"操作失败，不能据此判断没有消息：{exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
