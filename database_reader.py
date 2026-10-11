"""Authenticated local snapshots, committed WAL replay, read-only SQLite access.
All encrypted copies/plaintext scratch files live under runtime/ and are removed
when the context ends. This module never modifies source databases.
"""
from __future__ import annotations
from contextlib import contextmanager
import hashlib
import os
from pathlib import Path
import shutil
import sqlite3
import struct
import tempfile
import time

import cipher
from chatnotice import project_path
from memory_reader import load_secrets


def file_digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(2 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def capture_stable(source: Path, destination: Path, attempts: int = 3) -> dict:
    wal_source = Path(str(source) + "-wal")
    wal_dest = Path(str(destination) + "-wal")
    for _ in range(attempts):
        wal_exists = wal_source.is_file()
        shutil.copyfile(source, destination)
        if wal_exists:
            shutil.copyfile(wal_source, wal_dest)
        elif wal_dest.exists():
            wal_dest.unlink()
        main_matches = file_digest(source) == file_digest(destination)
        wal_matches = (wal_exists == wal_source.is_file() and
            (not wal_exists or file_digest(wal_source) == file_digest(wal_dest)))
        if main_matches and wal_matches:
            return {"source": str(source), "wal_present": wal_exists,
                    "capture": "copied_files_matched_full_source_hashes"}
        time.sleep(0.1)
    raise RuntimeError("Database changed during snapshot; retry when the client is idle")


def wal_checksum(data: bytes, state: tuple[int, int], byteorder: str) -> tuple[int, int]:
    if len(data) % 8:
        raise ValueError("WAL checksum input must be a multiple of eight bytes")
    words = struct.unpack(byteorder + "I" * (len(data) // 4), data)
    s0, s1 = state
    for pos in range(0, len(words), 2):
        s0 = (s0 + words[pos] + s1) & 0xFFFFFFFF
        s1 = (s1 + words[pos + 1] + s0) & 0xFFFFFFFF
    return s0, s1


def inspect_wal(path: Path) -> dict:
    """Validate frame chain, applying only the last fully committed prefix."""
    with path.open("rb") as handle:
        header = handle.read(32)
        if not header:
            return {"frames": [], "committed_frames": 0, "commit_pages": None,
                    "ignored_tail": False}
        if len(header) != 32:
            raise ValueError("Incomplete WAL header")
        magic, version, page_size = struct.unpack_from(">III", header)
        if magic not in (0x377F0682, 0x377F0683) or version != 3007000 or page_size != 4096:
            raise ValueError("Unsupported WAL header/version/page size")
        order = "<" if magic == 0x377F0682 else ">"
        state = wal_checksum(header[:24], (0, 0), order)
        if state != struct.unpack_from(">II", header, 24):
            raise ValueError("WAL header checksum failed")
        salt = header[16:24]
        frames = []
        committed = 0
        commit_pages = None
        ignored_tail = False
        while True:
            offset = handle.tell()
            frame_header = handle.read(24)
            if not frame_header:
                break
            payload = handle.read(4096)
            if len(frame_header) != 24 or len(payload) != 4096:
                ignored_tail = True
                break
            if frame_header[8:16] != salt:
                ignored_tail = True
                break  # End of this WAL generation; never skip and resume.
            page, commit = struct.unpack_from(">II", frame_header)
            if not page or page > 0xFFFFFFFE:
                ignored_tail = True
                break
            next_state = wal_checksum(frame_header[:8] + payload, state, order)
            if next_state != struct.unpack_from(">II", frame_header, 16):
                ignored_tail = True
                break
            state = next_state
            frames.append((offset + 24, page))
            if commit:
                committed = len(frames)
                commit_pages = commit
        return {"frames": frames, "committed_frames": committed,
                "commit_pages": commit_pages,
                "ignored_tail": ignored_tail or committed < len(frames)}


def decode_main(source: Path, destination: Path, keys: cipher.PageKeys, prefix: int) -> int:
    size = source.stat().st_size - prefix
    if size <= 0 or size % 4096:
        raise ValueError("Unsupported encrypted file size/page alignment")
    with source.open("rb") as incoming, destination.open("xb") as output:
        incoming.seek(prefix)
        for page_number in range(1, size // 4096 + 1):
            page = incoming.read(4096)
            output.write(cipher.decrypt_page(page, page_number, keys))
    return size // 4096


def replay_wal(wal: Path, destination: Path, keys: cipher.PageKeys) -> dict:
    index = inspect_wal(wal)
    count = index["committed_frames"]
    if count:
        # Authenticate all committed frames before modifying the scratch database.
        with wal.open("rb") as incoming:
            for offset, number in index["frames"][:count]:
                incoming.seek(offset)
                if not cipher.verify_page(incoming.read(4096), number, keys):
                    raise ValueError("Committed WAL page authentication failed")
        with wal.open("rb") as incoming, destination.open("r+b") as output:
            for offset, number in index["frames"][:count]:
                if number > index["commit_pages"]:
                    continue
                incoming.seek(offset)
                output.seek((number - 1) * 4096)
                output.write(cipher.decrypt_page(incoming.read(4096), number, keys))
            output.truncate(index["commit_pages"] * 4096)
    return {"wal_committed_frames": count, "wal_ignored_tail": index["ignored_tail"]}


@contextmanager
def open_database(config: dict, platform: str, relative: str):
    root = Path(config[platform]["database_dir"]).resolve()
    source = (root / relative).resolve()
    if not source.is_relative_to(root):
        raise ValueError("Database path outside the confirmed client account")
    secrets = load_secrets()
    raw_hex = secrets.get(platform, {}).get(str(source))
    if not raw_hex:
        raise RuntimeError("No verified key available for this database")
    raw_key = bytes.fromhex(raw_hex)
    runtime = project_path("runtime/database-snapshots")
    runtime.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=runtime) as directory:
        encrypted = Path(directory) / "source.db"
        plain = Path(directory) / "plain.db"
        metadata = capture_stable(source, encrypted)
        prefix = 1024 if platform == "qq" else 0
        with encrypted.open("rb") as handle:
            handle.seek(prefix)
            first = handle.read(4096)
        if platform == "wechat":
            keys = cipher.derive_wechat_keys(raw_key, first)
        elif len(raw_key) == 32:
            keys = cipher.derive_qq_raw_keys(raw_key, first)
        else:
            keys = cipher.derive_qq_keys(raw_key, first)
        metadata["main_pages"] = decode_main(encrypted, plain, keys, prefix)
        wal = Path(str(encrypted) + "-wal")
        metadata.update(replay_wal(wal, plain, keys) if wal.exists() else
                        {"wal_committed_frames": 0, "wal_ignored_tail": False})
        # A standalone WAL-replayed copy needs rollback journal mode, not shared WAL state.
        # This changes the COPY only, never the original client database.
        with plain.open("r+b") as output:
            output.seek(18)
            output.write(b"\x01\x01")
        uri = plain.as_uri() + "?mode=ro&immutable=1"
        connection = sqlite3.connect(uri, uri=True)
        connection.row_factory = sqlite3.Row
        connection.text_factory = lambda b: b.decode("utf-8", errors="replace")
        try:
            result = connection.execute("PRAGMA quick_check").fetchall()
            if len(result) != 1 or result[0][0] != "ok":
                raise ValueError("Decoded snapshot failed SQLite consistency check")
            metadata["sqlite_quick_check"] = "ok"
            yield connection, metadata
        finally:
            connection.close()
