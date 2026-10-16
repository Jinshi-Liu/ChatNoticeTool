"""DingTalk local group-chat, text-only, on-demand export (adapter layer).

Positioning — identical to the rest of this project:
  * Fully offline: outbound sockets are hard-blocked before any client file is read.
  * Read-only: the DingTalk data directory is opened for reading only; every
    decrypted artifact lives in this project's runtime/ and is removed on exit.
  * On-demand: one explicit command, one batch. No daemon, no tray, no scheduler,
    no background thread, no startup entry.
  * Scope: GROUP chats only (tbconversation.type = 2). A cid that is missing or
    is a direct chat is rejected outright — never silently widened.
  * Content: TEXT messages only (contentType = 1). Media, cards and files are
    never downloaded or executed.

This file is a NEW standalone entry point. It does not modify, wrap, or bypass
any QQ/WeChat code path; those capabilities are untouched.

License: Apache-2.0. See NOTICE.md.
"""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timedelta, timezone
import json
import re
import sys
from pathlib import Path

from chatnotice import LOCAL_TZ, project_path, save_json
from dingtalk_probe import (discover_account_dirs, discover_bases, discover_uid,
                            install_offline_guard, read_salt, decrypted_database)

MSG_TYPE_TEXT = 1
DIRECT_CHAT = 1
GROUP_CHAT = 2


def resolve_account_dir(explicit: str | None) -> Path:
    if explicit:
        account_dir = Path(explicit).resolve()
        if not (account_dir / "DBFiles" / "dingtalk.db").is_file():
            raise ValueError("--data-dir 不含 DBFiles/dingtalk.db")
        return account_dir
    accounts = discover_account_dirs(discover_bases())
    if not accounts:
        raise ValueError("未发现钉钉 *_v2/_v3 账号目录")
    if len(accounts) > 1:
        raise ValueError("发现多个账号目录，请用 --data-dir 明确指定: "
                         + ", ".join(str(p) for p in accounts))
    return accounts[0]


def dingtalk_groups(connection) -> list[dict]:
    """Group-chat metadata only (type=2). Direct chats are never listed."""
    rows = connection.execute(
        "SELECT cid, title, memberCount, createAt, lastModify FROM tbconversation "
        "WHERE type=? AND status=1 ORDER BY title, cid", (GROUP_CHAT,)).fetchall()
    return [{"platform": "dingtalk", "group_id": row["cid"],
             "name": (row["title"] or row["cid"]), "member_count": row["memberCount"],
             "created_ms": row["createAt"], "last_modify_ms": row["lastModify"]}
            for row in rows]


def require_group_chats(connection, group_ids: list[str]) -> dict[str, str]:
    """Map each requested cid to its title; reject unknown or non-group cids."""
    known = {row["cid"]: (row["type"], row["title"])
             for row in connection.execute(
                 "SELECT cid, type, title FROM tbconversation WHERE cid IN ("
                 + ",".join("?" for _ in group_ids) + ")", group_ids)}
    names: dict[str, str] = {}
    for cid in group_ids:
        if cid not in known:
            raise ValueError(f"群 ID 不存在于本地会话表: {cid}")
        conv_type, title = known[cid]
        if conv_type != GROUP_CHAT:
            raise ValueError(f"{cid} 是单聊会话，已按范围约束拒绝；仅支持群聊导出")
        names[cid] = title or cid
    return names


def sender_names(connection, sender_ids: list[str]) -> dict[str, str]:
    if not sender_ids:
        return {}
    rows = connection.execute(
        "SELECT uid, nick, realName FROM tbuser_profile_v2 WHERE uid IN ("
        + ",".join("?" for _ in sender_ids) + ")", sender_ids)
    return {row["uid"]: (row["realName"] or row["nick"] or row["uid"]) for row in rows}


def find_shard(connection, cid: str) -> str | None:
    """Locate the populated tbmsg_NNN shard holding this conversation."""
    shards = [row[0] for row in connection.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'tbmsg\\_%' "
        "ESCAPE '\\' ORDER BY name")]
    for shard in shards:
        if connection.execute(f'SELECT 1 FROM "{shard}" WHERE cid=? LIMIT 1',
                              (cid,)).fetchone():
            return shard
    return None


def decode_text(content: str | None) -> tuple[str, str]:
    """Text messages only: content is a JSON object {"text": ...} or raw text."""
    if content is None:
        return "", "empty"
    raw = content.strip()
    if not raw:
        return "", "empty"
    try:
        data = json.loads(raw)
    except ValueError:
        return raw, "utf8"
    if not isinstance(data, dict):
        return raw, "utf8"
    text = data.get("text", "")
    if not isinstance(text, str):
        return "", "nontext_field"
    # Strip lone surrogates that would break downstream JSON.
    text = text.encode("utf-8", errors="replace").decode("utf-8", errors="replace")
    return text, "decoded" if text else "empty"


def selected_dingtalk(connection, group_ids: list[str], since_ms: int,
                      until_ms: int, names: dict[str, str]) -> list[dict]:
    """Text-only rows inside the confirmed group + time window."""
    rows: list[dict] = []
    senders: set[str] = set()
    for cid in group_ids:
        shard = find_shard(connection, cid)
        if shard is None:
            continue  # no local rows for this group; not an error
        sql = (f'SELECT mid, senderId, createdAt, content FROM "{shard}" '
               "WHERE cid=? AND createdAt>=? AND createdAt<? AND recallStatus=0 "
               "AND contentType=? ORDER BY createdAt, mid")
        for row in connection.execute(sql, (cid, since_ms, until_ms, MSG_TYPE_TEXT)):
            rows.append(dict(row, cid=cid, shard=shard))
            senders.add(row["senderId"])
    resolved = sender_names(connection, sorted(senders))
    messages = []
    for row in rows:
        text, status = decode_text(row["content"])
        timestamp = int(row["createdAt"] // 1000)
        messages.append({
            "source_id": f"dingtalk:{row['cid']}:{row['shard']}:{row['mid']}",
            "platform": "dingtalk",
            "group_id": row["cid"],
            "group_name": names[row["cid"]],
            "message_id": str(row["mid"]),
            "timestamp": timestamp,
            "time": datetime.fromtimestamp(timestamp, LOCAL_TZ).isoformat(),
            "message_type_code": MSG_TYPE_TEXT,
            "sender": resolved.get(row["senderId"], str(row["senderId"])),
            "text": text,
            "body_status": status,
            "type": "text",
            "attachments": [],
            "attachment_content_status": "not_applicable",
            "shard": row["shard"],
        })
    messages.sort(key=lambda m: (m["timestamp"], m["group_id"], m["message_id"]))
    return messages


def parse_bound_ms(value: str, *, end: bool = False) -> int:
    try:
        dt = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError("日期格式应为 YYYY-MM-DD 或 ISO 日期时间") from exc
    if len(value) == 10 and end:
        dt += timedelta(days=1)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=LOCAL_TZ)
    return int(dt.timestamp() * 1000)


def export(config: dict, request: dict) -> dict:
    if not request.get("confirmed"):
        raise PermissionError("Group selection not confirmed")
    group_ids = request["group_ids"]
    if len(set(group_ids)) != len(group_ids):
        raise ValueError("群 ID 重复")
    account_dir = Path(config["account_dir"])
    uid = config["uid"]
    salt = read_salt(account_dir)
    since_ms = parse_bound_ms(request["since"])
    until_ms = parse_bound_ms(request["until"], end=True)
    if since_ms >= until_ms:
        raise ValueError("时间范围不合法，开始必须早于结束")

    with decrypted_database(account_dir, uid, salt) as (connection, metadata):
        names = require_group_chats(connection, group_ids)
        messages = selected_dingtalk(connection, group_ids, since_ms, until_ms, names)
        snapshots = [dict(metadata, platform="dingtalk")]

    batch = datetime.now(LOCAL_TZ).strftime("%Y%m%d-%H%M%S-%f")
    output = project_path("exports/" + batch)
    output.mkdir(parents=True, exist_ok=False)
    report = {"request": request, "message_count": len(messages), "messages": messages,
              "snapshots": snapshots, "parse_errors": [],
              "local_records_only": True, "group_chats_only": True,
              "text_messages_only": True, "private_chats_queried": False,
              "all_attachment_contents_processed": False}
    save_json(f"exports/{batch}/messages.json", report)
    counts = Counter(m["group_id"] for m in messages)
    summary = {"batch": batch, "output_dir": str(output),
               "since": request["since"], "until": request["until"],
               "message_count": len(messages),
               "groups": [{"platform": "dingtalk", "group_id": cid, "name": names[cid],
                           "message_count": counts[cid]} for cid in group_ids],
               "body_status_counts": dict(Counter(m["body_status"] for m in messages)),
               "raw_selected_protection": "text only; no media keys present",
               "unrelated_private_chats_queried": False}
    save_json(f"exports/{batch}/summary.json", summary)
    save_json("state/latest-dingtalk-export.json", summary)
    lines = ["# DingTalk selected group text messages",
             f"Window: {request['since']} — {request['until']}",
             f"Groups: {', '.join(names[c] for c in group_ids)}", ""]
    for message in messages:
        lines += [f"## {message['time']} | {message['group_name']} | {message['sender']}",
                  message["text"] or f"[{message['body_status']}]", ""]
    (output / "messages.md").write_text("\n".join(lines), encoding="utf-8")
    return summary


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="钉钉本地群聊文本消息按需导出：离线、只读、仅群聊、仅文本、一次性")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("groups", help="只列出本地群聊（不列单聊、不读正文）")
    group_command = commands.add_parser("export", help="导出指定群+时间的文本消息；需 --confirm")
    group_command.add_argument("--group-id", action="append", required=True)
    group_command.add_argument("--since", required=True)
    group_command.add_argument("--until", required=True)
    group_command.add_argument("--confirm", action="store_true",
                               help="明确确认有权导出这些群的本地消息")
    for command in (commands.choices["groups"], group_command):
        command.add_argument("--data-dir", help="钉钉账号目录；省略则自动发现")
        command.add_argument("--uid", help="真实 UID；省略则从本地日志恢复")
    args = parser.parse_args(argv)
    try:
        guard = install_offline_guard()
        account_dir = resolve_account_dir(args.data_dir)
        if account_dir.name.lower().endswith("_v2"):
            raise ValueError("当前适配层仅支持 V3 离线解密")
        uid = args.uid or discover_uid(account_dir)
        if not uid:
            raise ValueError("无法从日志恢复真实 UID，请用 --uid 显式提供")
        config = {"account_dir": str(account_dir), "uid": uid}
        if args.command == "groups":
            salt = read_salt(account_dir)
            with decrypted_database(account_dir, uid, salt) as (connection, _meta):
                groups = dingtalk_groups(connection)
            report = {"platform": "dingtalk", "groups": groups, "read_chat_bodies": False,
                      "direct_chats_listed": False, "positioning": guard}
            out = save_json("state/dingtalk-groups.json", report)
            print(f"本地群聊记录：{len(groups)}；未查询消息正文，未列出任何单聊。")
            for group in groups:
                print(f"{group['group_id']}\t{group['name']} ({group['member_count']}人)")
            print(f"群列表文件：{out}")
            return 0
        if not args.confirm:
            raise PermissionError("请核对群和时间范围并添加 --confirm；未读取消息")
        request = {"confirmed": True, "platform": "dingtalk",
                   "group_ids": args.group_id, "since": args.since, "until": args.until}
        summary = export(config, request)
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        print(f"输出目录：{summary['output_dir']}（messages.json / messages.md / summary.json）")
        print("仅导出群聊文本消息；单聊、媒体、卡片均未读取。")
        return 0
    except (OSError, ValueError, KeyError, RuntimeError, PermissionError) as exc:
        print(f"操作失败，不能据此判断没有消息：{exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
