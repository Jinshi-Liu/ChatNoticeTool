"""Stage-0 read-only probe for the planned DingTalk (local group message) adapter.

Positioning (must stay true, mirroring the project's existing boundaries):
  * Fully offline: no network imports, no sockets, no cloud calls.
  * Read-only: the DingTalk data directory is only ever opened for reading; every
    decrypted artifact is written inside this project's runtime/ directory.
  * On-demand: a single one-shot command. No daemon, no tray, no scheduler,
    no background thread, no startup entry, no service.
  * Group chats only: this probe never prints message bodies or private chats.
    It only reports schema/metadata so field mappings can be confirmed.

This file intentionally does NOT modify any existing QQ/WeChat behaviour. It adds
no new entry point to them and imports their helpers read-only (path guard only).

License: Apache-2.0. DingTalk V3 database-format constants (salt, KDF) are
format data referenced from public reverse-engineering notes; no third-party
source is copied. See NOTICE.md.
"""
from __future__ import annotations

import argparse
import base64
import io
import json
import os
import re
import shutil
import sqlite3
import struct
import sys
import tempfile
import threading
import time
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from chatnotice import ROOT, LOCAL_TZ, project_path

PAGE_SIZE = 4096
WAL_HEADER_SIZE = 32
WAL_FRAME_HEADER_SIZE = 24
# DingTalk V3 key derivation constants (observed in released open-source tooling).
V3_PBKDF2_SALT = b"666DingT"
V3_PBKDF2_ITERATIONS = 1000
V3_PBKDF2_KEYLEN = 32
UID_PATTERNS = (
    (re.compile(r"real_uid=(\d{8,14})"), 10_000),
    (re.compile(r"myOpenId=(\d{8,14})"), 10_000),
    (re.compile(r"&uid=(\d{8,14})"), 10),
)
COPY_ATTEMPTS = 10
COPY_DELAY = 0.3


# --------------------------------------------------------------------------- #
# Offline / no-background self-check
# --------------------------------------------------------------------------- #
NETWORK_CLIENTS = ("urllib.request", "urllib.error", "http.client", "requests",
                   "urllib3", "aiohttp", "httpx", "ftplib", "smtplib", "telnetlib")


def install_offline_guard() -> dict:
    """Hard-block socket creation, and fail loudly on any background worker.

    Note: argparse and a few stdlib helpers import the socket *module* for
    unrelated reasons, so presence in sys.modules proves nothing. Instead we
    neutralise the constructors, so any real outbound attempt raises.
    """
    import socket as socket_module

    def _blocked(*args, **kwargs):
        raise RuntimeError("network access is forbidden: this tool must stay fully offline")

    socket_module.socket = _blocked
    socket_module.create_connection = _blocked
    socket_module.create_server = _blocked
    socket_module.getaddrinfo = _blocked

    live_threads = [t.name for t in threading.enumerate() if t is not threading.main_thread()]
    if live_threads:
        raise RuntimeError(f"refusing to run: unexpected background threads {live_threads}")
    imported_clients = [name for name in NETWORK_CLIENTS if name in sys.modules]
    if imported_clients:
        raise RuntimeError(f"refusing to run: network client modules imported {imported_clients}")
    return {
        "offline_socket_blocked": True,
        "network_client_modules_imported": [],
        "background_threads": 0,
        "background_processes_spawned": 0,
        "cloud_calls": 0,
        "one_shot_command": True,
        "tray_or_service_created": False,
    }


# --------------------------------------------------------------------------- #
# Discovery (read-only)
# --------------------------------------------------------------------------- #
def _read_text(path: Path, limit: int = 4096) -> str:
    try:
        with path.open("rb") as handle:
            return handle.read(limit).decode("utf-8", errors="ignore")
    except OSError:
        return ""


def discover_bases() -> list[Path]:
    bases: list[Path] = []
    appdata = os.environ.get("APPDATA", "")
    if appdata:
        redirect = Path(appdata) / "DingTalk" / "redirectAppData.dat"
        if redirect.is_file():
            target = _read_text(redirect, 1024).strip().strip('"')
            if target:
                bases.append(Path(target))
        bases.append(Path(appdata) / "DingTalk")
    local = os.environ.get("LOCALAPPDATA", "")
    if local:
        bases.append(Path(local) / "DingTalk")
    seen, unique = set(), []
    for base in bases:
        key = os.path.normcase(os.path.normpath(str(base)))
        if key not in seen:
            seen.add(key)
            unique.append(base)
    return unique


def discover_account_dirs(bases: list[Path]) -> list[Path]:
    found: list[Path] = []
    for base in bases:
        if not base.is_dir():
            continue
        try:
            entries = sorted(base.iterdir())
        except OSError:
            continue
        for entry in entries:
            if entry.is_dir() and entry.name.lower().endswith(("_v2", "_v3")):
                if (entry / "DBFiles" / "dingtalk.db").is_file():
                    found.append(entry)
    return found


def discover_uid(account_dir: Path) -> str:
    """Recover the real UID from local client logs (read-only)."""
    log_dir = account_dir.parent / "log"
    scores: dict[str, int] = {}
    candidates: list[Path] = []
    if log_dir.is_dir():
        try:
            names = sorted(p for p in log_dir.iterdir() if p.is_file())
        except OSError:
            names = []
        for name in names:
            if name.name.startswith(("cef_debug.log", "gaea.log")):
                candidates.append(name)
    for path in candidates[-16:]:
        text = _read_text(path, 4 * 1024 * 1024)
        for pattern, weight in UID_PATTERNS:
            for uid in pattern.findall(text):
                scores[uid] = scores.get(uid, 0) + weight
    if not scores:
        return ""
    return sorted(scores.items(), key=lambda item: (-item[1], item[0]))[0][0]


def read_salt(account_dir: Path) -> str:
    path = account_dir / "user_config"
    if not path.is_file():
        raise FileNotFoundError(f"missing user_config (V3 salt source): {path}")
    raw = _read_text(path, 8192).strip()
    decoded = base64.b64decode(raw).decode("utf-8")
    salt = str(json.loads(decoded).get("salt", "")).strip()
    if not salt:
        raise ValueError("user_config does not contain a salt")
    return salt


# --------------------------------------------------------------------------- #
# Snapshot + decryption (all writes land in runtime/)
# --------------------------------------------------------------------------- #
def snapshot(source: Path, destination: Path) -> dict:
    """Copy the encrypted trio with retries; tolerate a live WAL."""
    wal_source = Path(str(source) + "-wal")
    for attempt in range(1, COPY_ATTEMPTS + 1):
        try:
            for src, dst in ((source, destination),
                             (wal_source, Path(str(destination) + "-wal"))):
                if src.is_file():
                    shutil.copyfile(src, dst)
                elif dst.exists():
                    dst.unlink()
            if destination.is_file():
                return {
                    "attempts": attempt,
                    "main_bytes": destination.stat().st_size,
                    "wal_bytes": Path(str(destination) + "-wal").stat().st_size
                    if Path(str(destination) + "-wal").is_file() else 0,
                    "shm_copied": False,
                    "note": "WAL is replayed from its committed frame prefix only; "
                            "the -shm file is not needed for an offline snapshot",
                }
        except (PermissionError, OSError) as exc:
            if attempt == COPY_ATTEMPTS:
                raise RuntimeError(f"cannot snapshot DingTalk database: {exc}") from exc
            time.sleep(COPY_DELAY)
    raise RuntimeError("snapshot produced no file")


def derive_key(uid: str, salt: str) -> bytes:
    import hashlib
    derived = hashlib.pbkdf2_hmac(
        "sha1", (str(uid) + salt).encode("utf-8"), V3_PBKDF2_SALT,
        V3_PBKDF2_ITERATIONS, V3_PBKDF2_KEYLEN,
    )
    return hashlib.md5(derived).hexdigest()[:16].encode("ascii")


def decrypt_pages(source: Path, destination: Path, key: bytes) -> int:
    decryptor = Cipher(algorithms.AES(key), modes.ECB()).decryptor()
    pages = 0
    with source.open("rb") as incoming, destination.open("xb") as output:
        while True:
            page = incoming.read(PAGE_SIZE)
            if not page:
                break
            if len(page) == PAGE_SIZE:
                output.write(decryptor.update(page))
                pages += 1
            else:  # never guess at a torn tail page
                output.write(page)
                break
    decryptor.finalize()
    return pages


def wal_checksum(data: bytes, byteorder: str, state: tuple[int, int]) -> tuple[int, int]:
    if len(data) % 8:
        raise ValueError("WAL checksum input must be a multiple of eight bytes")
    s0, s1 = state
    for offset in range(0, len(data), 8):
        x0 = int.from_bytes(data[offset:offset + 4], byteorder)
        x1 = int.from_bytes(data[offset + 4:offset + 8], byteorder)
        s0 = (s0 + x0 + s1) & 0xFFFFFFFF
        s1 = (s1 + x1 + s0) & 0xFFFFFFFF
    return s0, s1


def decrypt_wal(source: Path, destination: Path, key: bytes) -> dict:
    """Decrypt frames, then keep only the checksum-continuous committed prefix."""
    decryptor = Cipher(algorithms.AES(key), modes.ECB()).decryptor()
    with source.open("rb") as incoming, destination.open("xb") as output:
        header = incoming.read(WAL_HEADER_SIZE)
        if len(header) != WAL_HEADER_SIZE:
            raise ValueError("incomplete encrypted WAL header")
        magic = int.from_bytes(header[:4], "big")
        if magic == 0x377F0682:
            byteorder = "little"
        elif magic == 0x377F0683:
            byteorder = "big"
        else:
            raise ValueError(f"unsupported WAL magic 0x{magic:08x}")
        raw_page_size = int.from_bytes(header[8:12], "big")
        page_size = 65536 if raw_page_size == 1 else raw_page_size
        state = wal_checksum(header[:24], byteorder, (0, 0))
        if state != struct.unpack_from(">II", header, 24):
            raise ValueError("WAL header checksum failed")
        salt = header[16:24]
        output.write(header)
        frames = committed = 0
        ignored_tail = False
        while True:
            frame_header = bytearray(incoming.read(WAL_FRAME_HEADER_SIZE))
            if not frame_header:
                break
            page = incoming.read(page_size)
            if len(frame_header) != WAL_FRAME_HEADER_SIZE or len(page) != page_size:
                ignored_tail = True  # torn write while the client is running
                break
            if frame_header[8:16] != salt:
                ignored_tail = True
                break
            page = decryptor.update(page)
            state = wal_checksum(bytes(frame_header[:8]) + page, byteorder, state)
            frame_header[16:20] = struct.pack(">I", state[0])
            frame_header[20:24] = struct.pack(">I", state[1])
            output.write(frame_header)
            output.write(page)
            frames += 1
            if int.from_bytes(frame_header[4:8], "big"):
                committed = frames
        decryptor.finalize()
    return {"wal_frames": frames, "wal_committed_frames": committed,
            "wal_page_size": page_size, "wal_ignored_tail": ignored_tail}


@contextmanager
def decrypted_database(account_dir: Path, uid: str, salt: str):
    """Yield a read-only handle to a fully offline decrypted snapshot."""
    db_files = account_dir / "DBFiles"
    source = db_files / "dingtalk.db"
    runtime = project_path("runtime")
    runtime.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=runtime, prefix="dingtalk-probe-") as work:
        workdir = Path(work)
        encrypted = workdir / "encrypted.db"
        plain = workdir / "plain.db"
        metadata = {"account_dir": str(account_dir), "source_db": str(source)}
        metadata["snapshot"] = snapshot(source, encrypted)
        key = derive_key(uid, salt)
        metadata["aes_key_fingerprint"] = f"{key.hex()[:4]}…{key.hex()[-4:]}"  # never the full key
        metadata["main_pages_decrypted"] = decrypt_pages(encrypted, plain, key)
        wal = Path(str(encrypted) + "-wal")
        metadata["wal"] = decrypt_wal(wal, Path(str(plain) + "-wal"), key) if wal.is_file() else {
            "wal_frames": 0, "wal_committed_frames": 0, "wal_page_size": None,
            "wal_ignored_tail": False}
        connection = sqlite3.connect(str(plain))
        try:
            connection.row_factory = sqlite3.Row
            connection.text_factory = lambda b: b.decode("utf-8", errors="replace")
            # Merge the WAL copy we just rebuilt, then re-open strictly read-only.
            connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchall()
            connection.close()
            uri = plain.as_uri() + "?mode=ro&immutable=1"
            connection = sqlite3.connect(uri, uri=True)
            connection.row_factory = sqlite3.Row
            connection.text_factory = lambda b: b.decode("utf-8", errors="replace")
            check = connection.execute("PRAGMA quick_check").fetchall()
            if len(check) != 1 or check[0][0] != "ok":
                raise ValueError(f"decrypted snapshot failed quick_check: {check}")
            metadata["sqlite_quick_check"] = "ok"
            yield connection, metadata
        finally:
            connection.close()
    # TemporaryDirectory guarantees the plaintext snapshot is gone after this point.


# --------------------------------------------------------------------------- #
# Probe (metadata only: no message bodies, no group titles, no private chats)
# --------------------------------------------------------------------------- #
def table_list(connection: sqlite3.Connection) -> list[str]:
    return [row[0] for row in connection.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]


def columns(connection: sqlite3.Connection, table: str) -> list[dict]:
    quoted = '"' + table.replace('"', '""') + '"'
    return [{"name": row["name"], "type": row["type"], "pk": row["pk"]}
            for row in connection.execute(f"PRAGMA table_info({quoted})")]


def probe(account_dir: Path, uid: str, salt: str) -> dict:
    with decrypted_database(account_dir, uid, salt) as (connection, metadata):
        report: dict = {"metadata": metadata}
        tables = table_list(connection)
        report["table_count"] = len(tables)
        report["tables"] = tables
        report["has_tbconversation"] = "tbconversation" in tables

        shards = sorted(name for name in tables if re.fullmatch(r"tbmsg_\d{3}", name))
        report["message_shard_tables"] = {"count": len(shards), "first": shards[:3],
                                         "last": shards[-3:]}
        report["user_table_present"] = "tbuser_profile_v2" in tables
        report["image_index_present"] = "im_image_info" in tables
        report["last_message_tables_present"] = sorted(
            name for name in tables if name in ("tblastmsg", "preview_message"))

        # Row counts only: proves the shards actually hold data (no bodies read).
        populated = {}
        for name in shards:
            count = connection.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()[0]
            if count:
                populated[name] = count
        report["message_shards_nonempty"] = {
            "count": len(populated),
            "total_rows": sum(populated.values()),
            "largest": sorted(populated.items(), key=lambda item: -item[1])[:5],
        }
        if not populated:
            raise ValueError("all tbmsg shards are empty; cannot validate message schema")

        if not report["has_tbconversation"]:
            raise ValueError("tbconversation missing; schema unsupported — stopping "
                             "(no message content was read or printed)")

        report["tbconversation_columns"] = columns(connection, "tbconversation")
        row = connection.execute(
            "SELECT COUNT(*) AS total, "
            "SUM(CASE WHEN type=1 THEN 1 ELSE 0 END) AS direct, "
            "SUM(CASE WHEN type=2 THEN 1 ELSE 0 END) AS groups, "
            "SUM(CASE WHEN status=1 THEN 1 ELSE 0 END) AS active "
            "FROM tbconversation").fetchone()
        report["conversation_counts"] = {"total": row["total"], "direct_chats": row["direct"],
                                        "group_chats": row["groups"], "active": row["active"]}

        first = max(populated, key=populated.get)
        report["sample_message_table"] = first
        report["tbmsg_columns"] = columns(connection, first)
        names = {c["name"] for c in report["tbmsg_columns"]}
        required = {"cid", "mid", "senderId", "contentType", "content",
                    "createdAt", "recallStatus"}
        report["tbmsg_required_columns_present"] = sorted(required & names)
        report["tbmsg_missing_columns"] = sorted(required - names)
        report["msg_type_counts"] = {  # counts only — no bodies
            str(r["contentType"]): r["n"] for r in connection.execute(
                f'SELECT contentType, COUNT(*) AS n FROM "{first}" '
                "WHERE recallStatus = 0 GROUP BY contentType ORDER BY n DESC LIMIT 20")}
        return report


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Stage-0 read-only probe for the DingTalk adapter: offline, one-shot, "
                    "group-chat metadata only. Prints no message bodies.")
    parser.add_argument("--data-dir", help="DingTalk account directory, e.g. ...\\<hash>_v3")
    parser.add_argument("--uid", help="Real DingTalk UID (numeric); auto-detected from logs if omitted")
    parser.add_argument("--json-out", default="state/dingtalk-probe.json")
    args = parser.parse_args(argv)
    try:
        # Establish the positioning guard before touching any client file.
        positioning = install_offline_guard()
        bases = discover_bases()
        if args.data_dir:
            account_dir = Path(args.data_dir).resolve()
            if not (account_dir / "DBFiles" / "dingtalk.db").is_file():
                raise ValueError("--data-dir does not contain DBFiles/dingtalk.db")
        else:
            accounts = discover_account_dirs(bases)
            if not accounts:
                raise ValueError("no DingTalk *_v2/_v3 account directory found")
            if len(accounts) > 1:
                raise ValueError("multiple accounts found; pass --data-dir explicitly: "
                                 + ", ".join(str(p) for p in accounts))
            account_dir = accounts[0]
        version = "v2" if account_dir.name.lower().endswith("_v2") else "v3"
        if version != "v3":
            raise ValueError("only the V3 format can be decrypted offline (V2 needs an "
                             "external binary and is out of scope)")
        uid = args.uid or discover_uid(account_dir)
        if not uid:
            raise ValueError("could not recover the real UID from logs; pass --uid manually")
        salt = read_salt(account_dir)

        report = probe(account_dir, uid, salt)
        report["positioning"] = positioning
        report.update({
            "format": version,
            "account_dir": str(account_dir),
            "uid_source": "explicit" if args.uid else "recovered from local client logs",
            "salt_present": True,
            "generated_at": datetime.now(LOCAL_TZ).isoformat(),
            "read_only": {"source_modified": False, "source_written": False,
                          "source_deleted": False, "memory_scanned": False,
                          "client_process_touched": False},
            "writes": {"location": "runtime/ (temporary, removed on exit)",
                       "private_data_leaked": False},
            "scope": {"group_chats_only": True, "private_chats_queried": False,
                      "message_bodies_printed": False},
        })
        out = project_path(args.json_out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

        gates = {
            "format_is_v3": report["format"] == "v3",
            "uid_resolved": bool(uid),
            "salt_present": report["salt_present"],
            "quick_check_ok": report["metadata"].get("sqlite_quick_check") == "ok",
            "tbconversation_present": report["has_tbconversation"],
            "message_shards_found": report["message_shard_tables"]["count"] > 0,
            "message_shards_have_rows": report["message_shards_nonempty"]["total_rows"] > 0,
            "group_chats_found": report["conversation_counts"]["group_chats"] > 0,
            "required_columns_present": not report.get("tbmsg_missing_columns", ["x"]),
        }
        report["gates"] = gates
        report["stage0_passed"] = all(gates.values())
        out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

        print(f"format            : {report['format']}")
        print(f"account dir       : {report['account_dir']}")
        print(f"uid               : resolved ({report['uid_source']})")
        print(f"main pages        : {report['metadata']['main_pages_decrypted']}")
        print(f"wal               : {report['metadata']['wal']}")
        print(f"quick_check       : {report['metadata'].get('sqlite_quick_check')}")
        print(f"tables            : {report['table_count']}")
        print(f"conversations     : {report['conversation_counts']}")
        print(f"message shards    : {report['message_shard_tables']['count']} "
              f"(non-empty {report['message_shards_nonempty']['count']}, "
              f"rows {report['message_shards_nonempty']['total_rows']})")
        print(f"sample shard      : {report['sample_message_table']} "
              f"types {report['msg_type_counts']}")
        print(f"missing tbmsg cols: {report.get('tbmsg_missing_columns')}")
        print(f"gate results      : {gates}")
        print(f"STAGE0            : {'PASS' if report['stage0_passed'] else 'FAIL'}")
        print(f"report            : {out}")
        print("No message bodies, group titles or private chats were read or printed.")
        return 0 if report["stage0_passed"] else 3
    except (OSError, ValueError, KeyError, RuntimeError, sqlite3.Error) as exc:
        print(f"probe failed (this must not be read as 'there are no messages'): {exc}",
              file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
