"""Group metadata only. Never query private contacts or message content."""
from __future__ import annotations
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
