"""Group metadata only. Never query private contacts or message content."""
from __future__ import annotations
import argparse
import json
from pathlib import Path

from chatnotice import save_json
from database_reader import open_database


def wechat_groups(config: dict) -> dict:
    with open_database(config, "wechat", "contact/contact.db") as (connection, metadata):
        columns = {row[1] for row in connection.execute('PRAGMA table_info("contact")')}
        if not {"username", "nick_name", "remark"} <= columns:
            raise ValueError("Unsupported WeChat contact schema")
        rows = connection.execute(
            "SELECT username, nick_name, remark FROM contact WHERE username LIKE '%@chatroom'"
        ).fetchall()
        groups = [{"platform": "wechat", "group_id": row["username"],
                   "name": row["nick_name"] or row["username"],
                   "remark": row["remark"] or ""} for row in rows]
    groups.sort(key=lambda row: (row["name"], row["group_id"]))
    return {"platform": "wechat", "groups": groups, "snapshot": metadata,
            "read_chat_bodies": False, "private_contacts_queried": False}


def qq_groups(config: dict) -> dict:
    with open_database(config, "qq", "group_info.db") as (connection, metadata):
        columns = {row[1] for row in connection.execute('PRAGMA table_info("group_list")')}
        if not {"60001", "60007"} <= columns:
            raise ValueError("Unsupported QQ group-list schema")
        rows = connection.execute('SELECT "60001" AS group_id, "60007" AS name FROM group_list').fetchall()
        groups = [{"platform": "qq", "group_id": str(row["group_id"]),
                   "name": row["name"] or str(row["group_id"]), "remark": ""}
                  for row in rows]
    groups.sort(key=lambda row: (row["name"], row["group_id"]))
    return {"platform": "qq", "groups": groups, "snapshot": metadata,
            "read_chat_bodies": False, "private_contacts_queried": False}


def inspect_group_schema(config: dict, platform: str) -> dict:
    relative = "contact/contact.db" if platform == "wechat" else "group_info.db"
    with open_database(config, platform, relative) as (connection, metadata):
        tables = [row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")]
        schema = {}
        for table in tables:
            if platform == "wechat" and table not in {"contact", "chat_room", "chatroom_member"}:
                continue
            quoted = '"' + table.replace('"', '""') + '"'
            schema[table] = [dict(row) for row in connection.execute("PRAGMA table_info(" + quoted + ")")]
    return {"snapshot": metadata, "tables": schema, "read_chat_bodies": False}


def main() -> int:
    parser = argparse.ArgumentParser(description="List group names only, without reading messages")
    parser.add_argument("platform", choices=["wechat", "qq"])
    parser.add_argument("--schema", action="store_true")
    parser.add_argument("--contains", default="", help="Display names containing this literal string")
    args = parser.parse_args()
    config = json.loads((Path(__file__).resolve().parent / "config.json").read_text(encoding="utf-8"))
    try:
        if args.schema:
            report = inspect_group_schema(config, args.platform)
            path = save_json(f"state/{args.platform}-group-schema.json", report)
            print(json.dumps({"snapshot": report["snapshot"],
                              "table_names": list(report["tables"])}, ensure_ascii=False, indent=2))
        else:
            report = wechat_groups(config) if args.platform == "wechat" else qq_groups(config)
            path = save_json(f"state/{args.platform}-groups.json", report)
            print(f"Group metadata ready: {len(report['groups'])} groups; no message content read")
            for group in report["groups"]:
                if args.contains in group["name"] or args.contains in group["remark"]:
                    print(json.dumps(group, ensure_ascii=False))
        print(f"Saved: {path}; temporary database copies removed")
        return 0
    except (OSError, ValueError, RuntimeError) as exc:
        print(f"Group metadata read failed: {exc}")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
