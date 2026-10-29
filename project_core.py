"""Shared project paths, time bounds and explicit chat scopes; no I/O on import."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent
LOCAL_TZ = timezone(timedelta(hours=8))
SQLITE_MAGIC = b"SQLite format 3\x00"
# Installation-specific: release clones do not block each other's widget.
WIDGET_MUTEX = 'Local\\ChatNoticeToolTodoWidget-' + hashlib.sha256(str(ROOT).lower().encode()).hexdigest()[:16]


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
