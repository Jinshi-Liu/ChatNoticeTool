from __future__ import annotations
import json
import os
from pathlib import Path
import sqlite3
import struct
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import database_reader as db
import memory_reader as memory
import groups


class ReaderTests(unittest.TestCase):
    def setUp(self):
        (ROOT / "runtime").mkdir(exist_ok=True)
        self.tmp = tempfile.TemporaryDirectory(dir=ROOT / "runtime")
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    @staticmethod
    def wal(frames):
        salt = b"abcdefgh"
        first = struct.pack(">IIII", 0x377F0682, 3007000, 4096, 0) + salt
        state = db.wal_checksum(first, (0, 0), "<")
        result = first + struct.pack(">II", *state)
        for number, commit, payload in frames:
            prefix = struct.pack(">II", number, commit)
            state = db.wal_checksum(prefix + payload, state, "<")
            result += prefix + salt + struct.pack(">II", *state) + payload
        return result

    def test_wal_only_last_commit_prefix(self):
        path = self.root / "fixture-wal"
        path.write_bytes(self.wal([(1, 1, bytes(4096)), (2, 0, bytes(4096))]))
        report = db.inspect_wal(path)
        self.assertEqual(report["committed_frames"], 1)
        self.assertEqual(report["commit_pages"], 1)
        self.assertTrue(report["ignored_tail"])

    def test_wal_rejects_tampered_header(self):
        path = self.root / "fixture-wal"
        data = bytearray(self.wal([(1, 1, bytes(4096))]))
        data[10] ^= 1
        path.write_bytes(data)
        with self.assertRaises(ValueError):
            db.inspect_wal(path)

    def test_wal_stops_on_bad_frame_checksum(self):
        path = self.root / "fixture-wal"
        data = bytearray(self.wal([(1, 1, bytes(4096)), (2, 2, bytes(4096))]))
        data[-1] ^= 1
        path.write_bytes(data)
        report = db.inspect_wal(path)
        self.assertEqual(report["committed_frames"], 1)
        self.assertTrue(report["ignored_tail"])

    def test_wal_stops_on_stale_generation(self):
        path = self.root / "fixture-wal"
        data = bytearray(self.wal([(1, 1, bytes(4096))]))
        data[40] ^= 1
        path.write_bytes(data)
        report = db.inspect_wal(path)
        self.assertEqual(report["committed_frames"], 0)
        self.assertTrue(report["ignored_tail"])

    def test_wal_truncated_tail_not_applied(self):
        path = self.root / "fixture-wal"
        path.write_bytes(self.wal([(1, 1, bytes(4096))]) + b"partial")
        report = db.inspect_wal(path)
        self.assertEqual(report["committed_frames"], 1)
        self.assertTrue(report["ignored_tail"])

    def test_real_sqlite_wal_checksums(self):
        source = self.root / "sqlite.db"
        conn = sqlite3.connect(source)
        try:
            conn.execute("PRAGMA page_size=4096")
            conn.execute("PRAGMA journal_mode=wal")
            conn.execute("PRAGMA wal_autocheckpoint=0")
            conn.execute("CREATE TABLE fixture (value TEXT)")
            conn.execute("INSERT INTO fixture VALUES ('synthetic-test')")
            conn.commit()
            report = db.inspect_wal(Path(str(source) + "-wal"))
            self.assertGreater(report["committed_frames"], 0)
            self.assertFalse(report["ignored_tail"])
        finally:
            conn.close()

    def test_capture_leaves_original_unchanged(self):
        source = self.root / "original.db"
        copy = self.root / "copy.db"
        source.write_bytes(b"synthetic-encrypted-file")
        Path(str(source) + "-wal").write_bytes(b"synthetic-wal")
        before = db.file_digest(source)
        report = db.capture_stable(source, copy)
        self.assertTrue(report["wal_present"])
        self.assertEqual(db.file_digest(source), before)
        self.assertEqual(copy.read_bytes(), source.read_bytes())

    def test_acquisition_requires_authorization(self):
        with self.assertRaises(PermissionError):
            memory.acquire({"policy": {"allow_process_memory_read": False}}, "qq", [1])

    @unittest.skipUnless(sys.platform == "win32", "Windows DPAPI")
    def test_dpapi_roundtrip_does_not_use_plaintext(self):
        secret = b"synthetic-database-key-not-real"
        protected = memory.protect_secret(secret)
        self.assertNotIn(secret, protected)
        self.assertEqual(memory.unprotect_secret(protected), secret)

    @unittest.skipUnless(sys.platform == "win32", "Windows process identity guard")
    def test_reader_refuses_non_client_process(self):
        with self.assertRaises(ValueError):
            memory.ReadOnlyProcess(os.getpid(), "qq")

    def test_wechat_group_query_never_reads_private_contacts(self):
        from contextlib import contextmanager
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute("CREATE TABLE contact(username TEXT, nick_name TEXT, remark TEXT)")
        conn.execute("INSERT INTO contact VALUES('123@chatroom','Test Class','')")
        conn.execute("INSERT INTO contact VALUES('wxid_private','DO NOT READ','private')")
        seen = []
        conn.set_trace_callback(seen.append)
        @contextmanager
        def fixture(*args):
            yield conn, {"sqlite_quick_check": "ok"}
        try:
            with patch.object(groups, "open_database", fixture):
                report = groups.wechat_groups({})
            self.assertEqual(len(report["groups"]), 1)
            self.assertEqual(report["groups"][0]["group_id"], "123@chatroom")
            self.assertFalse(report["read_chat_bodies"])
            selects = [q for q in seen if q.startswith("SELECT")]
            self.assertEqual(len(selects), 1)
            self.assertIn("WHERE username LIKE '%@chatroom'", selects[0])
        finally:
            conn.close()

    def test_qq_group_query_only_group_list(self):
        from contextlib import contextmanager
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute('CREATE TABLE group_list("60001" INTEGER, "60007" TEXT)')
        conn.execute('INSERT INTO group_list VALUES(123,"Test Class")')
        @contextmanager
        def fixture(*args):
            yield conn, {"sqlite_quick_check": "ok"}
        try:
            with patch.object(groups, "open_database", fixture):
                report = groups.qq_groups({})
            self.assertEqual(report["groups"][0]["group_id"], "123")
            self.assertFalse(report["read_chat_bodies"])
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()
