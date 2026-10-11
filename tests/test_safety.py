from __future__ import annotations
import hashlib
import hmac
import json
from pathlib import Path
import struct
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import chatnotice
import cleanup
import cipher


class ProjectTests(unittest.TestCase):
    def setUp(self):
        (ROOT / "runtime").mkdir(exist_ok=True)
        self.tmp = tempfile.TemporaryDirectory(dir=ROOT / "runtime")
        self.base = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def project(self):
        project = self.base / "project"
        project.mkdir()
        (project / ".chatnotice-owned.json").write_text(json.dumps({
            "project": "ChatNoticeTool", "owner_id": cleanup.OWNER_ID,
            "root": str(project.absolute())}), encoding="utf-8")
        return project

    def test_scope_requires_group(self):
        with self.assertRaises(ValueError):
            chatnotice.ReadScope("qq", (), 1, 2)

    def test_scope_rejects_private_wechat(self):
        with self.assertRaises(ValueError):
            chatnotice.ReadScope("wechat", ("wxid_private",), 1, 2)

    def test_scope_rejects_qq_name(self):
        with self.assertRaises(ValueError):
            chatnotice.ReadScope("qq", ("班级群",), 1, 2)

    def test_scope_rejects_duplicate_groups(self):
        with self.assertRaises(ValueError):
            chatnotice.ReadScope("qq", ("123", "123"), 1, 2)

    def test_scope_exact_group_platform_and_time(self):
        scope = chatnotice.ReadScope("qq", ("123",), 100, 200)
        self.assertTrue(scope.accepts("qq", "123", 100))
        self.assertFalse(scope.accepts("qq", "123", 200))
        self.assertFalse(scope.accepts("qq", "1234", 150))
        self.assertFalse(scope.accepts("wechat", "123", 150))

    def test_empty_time_range_rejected(self):
        with self.assertRaises(ValueError):
            chatnotice.ReadScope("qq", ("123",), 1, 1)

    def test_date_end_includes_whole_day(self):
        self.assertEqual(chatnotice.parse_bound("2026-10-10", end=True),
                         chatnotice.parse_bound("2026-10-11T00:00:00+08:00"))

    def test_timestamp_end_exclusive(self):
        self.assertEqual(chatnotice.parse_bound("2026-10-10T18:00:00", end=True),
                         chatnotice.parse_bound("2026-10-10T18:00:00+08:00"))

    def test_output_cannot_escape_root(self):
        with self.assertRaises(ValueError):
            chatnotice.project_path("../outside.json")

    def test_probe_does_not_modify_file(self):
        path = self.base / "fixture.db"
        content = chatnotice.SQLITE_MAGIC + bytes(4096 - 16)
        path.write_bytes(content)
        before = path.stat().st_mtime_ns
        report = chatnotice.database_metadata(path)
        self.assertEqual(report["status"], "plain_sqlite")
        self.assertFalse(report["read_content"])
        self.assertEqual(path.read_bytes(), content)
        self.assertEqual(path.stat().st_mtime_ns, before)

    def test_probe_missing_file(self):
        self.assertEqual(chatnotice.database_metadata(self.base / "absent.db")["status"], "not_found")

    def test_cleanup_preview_does_not_delete(self):
        project = self.project()
        (project / "state").mkdir()
        (project / "state" / "secret.json").write_text("test")
        plan = cleanup.perform_cleanup(project, "runtime")
        self.assertEqual([p.name for p in plan], ["state"])
        self.assertTrue((project / "state" / "secret.json").exists())

    def test_cleanup_preserves_originals_and_unknown_files(self):
        project = self.project()
        original = self.base / "client.db"
        original.write_bytes(b"original")
        (project / "config.json").write_text(json.dumps({"database": str(original)}))
        (project / "user-file.txt").write_text("keep")
        (project / "state").mkdir()
        (project / "state" / "test").write_text("delete")
        cleanup.perform_cleanup(project, "all", apply=True)
        self.assertEqual(original.read_bytes(), b"original")
        self.assertTrue((project / "user-file.txt").exists())
        self.assertFalse((project / "state").exists())
        self.assertFalse((project / "config.json").exists())

    def test_cleanup_rejects_bad_marker(self):
        project = self.project()
        (project / ".chatnotice-owned.json").write_text("{}")
        with self.assertRaises(ValueError):
            cleanup.perform_cleanup(project, "all", apply=True)
        self.assertTrue(project.exists())

    @unittest.skipUnless(sys.platform == "win32", "Windows junction safety test")
    def test_cleanup_refuses_directory_junction(self):
        import subprocess
        project = self.project()
        original = self.base / "client-data"
        original.mkdir()
        (original / "original.db").write_bytes(b"original")
        result = subprocess.run(["cmd.exe", "/c", "mklink", "/J",
            str(project / "state"), str(original)], capture_output=True)
        if result.returncode:
            self.skipTest("Could not create a test junction")
        try:
            with self.assertRaises(ValueError):
                cleanup.perform_cleanup(project, "all", apply=True)
            self.assertEqual((original / "original.db").read_bytes(), b"original")
        finally:
            (project / "state").rmdir()  # Remove junction only, never its target.

    def test_cleanup_registered_desktop_shortcut_only(self):
        from unittest.mock import patch
        project = self.project()
        desktop = self.base / 'Desktop'
        desktop.mkdir()
        owned = desktop / '群消息待办.lnk'
        owned.write_bytes(b'owned-shortcut-fixture')
        original = desktop / '群消息待办-原有.lnk'
        original.write_bytes(b'original')
        marker_path = project / '.chatnotice-owned.json'
        marker = json.loads(marker_path.read_text(encoding='utf-8'))
        marker['external_changes'] = [{'kind':'desktop_shortcut','path':str(owned),
            'sha256':hashlib.sha256(owned.read_bytes()).hexdigest(),'widget':str(project / 'todo_widget.py')}]
        marker_path.write_text(json.dumps(marker),encoding='utf-8')
        with patch.object(cleanup,'desktop_directory',return_value=desktop.resolve()):
            cleanup.perform_cleanup(project,'all',apply=True)
        self.assertFalse(owned.exists())
        self.assertEqual(original.read_bytes(),b'original')

    def test_cleanup_preserves_modified_shortcut(self):
        import io
        from unittest.mock import patch
        project = self.project()
        desktop = self.base / 'Desktop'
        desktop.mkdir()
        shortcut = desktop / '群消息待办.lnk'
        shortcut.write_bytes(b'user-modified')
        marker_path = project / '.chatnotice-owned.json'
        marker = json.loads(marker_path.read_text(encoding='utf-8'))
        marker['external_changes'] = [{'kind':'desktop_shortcut','path':str(shortcut),
            'sha256':hashlib.sha256(b'old-owned').hexdigest(),'widget':str(project / 'todo_widget.py')}]
        marker_path.write_text(json.dumps(marker),encoding='utf-8')
        with patch.object(cleanup,'desktop_directory',return_value=desktop.resolve()), patch('sys.stderr',new=io.StringIO()):
            cleanup.perform_cleanup(project,'all',apply=True)
        self.assertEqual(shortcut.read_bytes(),b'user-modified')

    def test_cleanup_full_owned_root_removed(self):
        project = self.project()
        (project / "runtime").mkdir()
        cleanup.perform_cleanup(project, "all", apply=True)
        self.assertFalse(project.exists())


class CipherTests(unittest.TestCase):
    @staticmethod
    def fixture(keys, pgno=1, salt=bytes(range(16))):
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
        start = 16 if pgno == 1 else 0
        end = 4096 - keys.reserve
        plain = bytes((i % 251 for i in range(4096)))
        if pgno == 1:
            plain = cipher.SQLITE_MAGIC + plain[16:]
        iv = bytes(range(16, 32))
        encoder = Cipher(algorithms.AES(keys.encryption), modes.CBC(iv)).encryptor()
        data = encoder.update(plain[start:end]) + encoder.finalize()
        tag = hmac.new(keys.authentication, data + iv + struct.pack("<I", pgno), keys.digest).digest()
        page = (salt if pgno == 1 else b"") + data + iv + tag
        page += bytes(4096 - len(page))
        expected = plain[:end] + bytes(keys.reserve)
        return page, expected

    def test_wechat_authenticated_decode(self):
        raw = bytes(range(32))
        salt = bytes(range(16))
        keys = cipher.derive_wechat_keys(raw, salt + bytes(4080))
        page, expected = self.fixture(keys, salt=salt)
        self.assertTrue(cipher.verify_page(page, 1, keys))
        self.assertEqual(cipher.decrypt_page(page, 1, keys), expected)

    def test_wechat_explicit_salt(self):
        raw = bytes(range(32))
        salt = bytes(range(16))
        a = cipher.derive_wechat_keys(raw, salt + bytes(4080))
        b = cipher.derive_wechat_keys(raw + salt, bytes(4096))
        self.assertEqual(a, b)

    def test_qq_authenticated_decode(self):
        password = b"1234567890abcdef"
        salt = bytes(range(16))
        keys = cipher.derive_qq_keys(password, salt + bytes(4080))
        page, expected = self.fixture(keys, salt=salt)
        actual_keys = cipher.derive_qq_keys(password, page)
        self.assertEqual(cipher.decrypt_page(page, 1, actual_keys), expected)

    def test_qq_wrong_key_rejected(self):
        salt = bytes(range(16))
        keys = cipher.derive_qq_keys(b"1234567890abcdef", salt + bytes(4080))
        page, _ = self.fixture(keys, salt=salt)
        wrong = cipher.derive_qq_keys(b"0000000000000000", page)
        with self.assertRaises(ValueError):
            cipher.decrypt_page(page, 1, wrong)

    def test_tampered_page_rejected(self):
        keys = cipher.derive_wechat_keys(bytes(range(32)), bytes(4096))
        page, _ = self.fixture(keys)
        damaged = bytearray(page)
        damaged[100] ^= 1
        self.assertFalse(cipher.verify_page(bytes(damaged), 1, keys))

    def test_page_number_authenticated(self):
        keys = cipher.derive_wechat_keys(bytes(range(32)), bytes(4096))
        page, expected = self.fixture(keys, pgno=2)
        self.assertEqual(cipher.decrypt_page(page, 2, keys), expected)
        self.assertFalse(cipher.verify_page(page, 3, keys))

    def test_truncated_page_rejected(self):
        keys = cipher.derive_wechat_keys(bytes(range(32)), bytes(4096))
        self.assertFalse(cipher.verify_page(b"truncated", 1, keys))


if __name__ == "__main__":
    unittest.main()
