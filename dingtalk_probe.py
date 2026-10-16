"""Experimental DingTalk V3 snapshot primitives, adapted from PR #1.

No client discovery/log reads, network hooks, memory scanning, or CLI here.
UID is supplied explicitly through confirmed source configuration. V3 AES-ECB
has no page MAC: structural checks are not cryptographic authentication.
"""
from __future__ import annotations

import base64
from contextlib import contextmanager
import hashlib
import json
from pathlib import Path
import sqlite3
import struct
import tempfile
import time

from agent_config import AgentError, private_path

PAGE_SIZE = 4096
V3_PBKDF2_SALT = b'666DingT'


def source_path(account_dir, relative):
    root = Path(account_dir).absolute()
    if root.resolve() != root:
        raise AgentError('unsafe_path', '钉钉账号目录不能通过链接访问')
    return private_path(root, relative)


def read_salt(account_dir):
    path = source_path(account_dir, 'user_config')
    with path.open('rb') as stream:
        data = stream.read(16385)
    if len(data) > 16384:
        raise AgentError('unsupported_format', '钉钉user_config超出支持的大小')
    try:
        value = json.loads(base64.b64decode(data.strip(), validate=True).decode('utf-8'))
        salt = value.get('salt') if isinstance(value, dict) else None
        if not isinstance(salt, str) or not salt or len(salt) > 1024:
            raise ValueError()
    except (ValueError, UnicodeError):
        raise AgentError('unsupported_format', '钉钉V3配置格式不受支持，未猜测凭据') from None
    return salt


def derive_key(uid, salt):
    derived = hashlib.pbkdf2_hmac('sha1', (uid + salt).encode('utf-8'), V3_PBKDF2_SALT, 1000, 32)
    return hashlib.md5(derived).hexdigest()[:16].encode('ascii')


def decoder(key):
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    return Cipher(algorithms.AES(key), modes.ECB()).decryptor()


def validate_header(page):
    if (len(page) != PAGE_SIZE or not page.startswith(b'SQLite format 3\0')
            or int.from_bytes(page[16:18], 'big') != PAGE_SIZE):
        raise AgentError('unsupported_format', '钉钉V3解密首页无效，请核对UID与格式；未尝试其他账号')


def check_header(account_dir, uid):
    salt = read_salt(account_dir)
    with source_path(account_dir, 'DBFiles/dingtalk.db').open('rb') as stream:
        page = stream.read(PAGE_SIZE)
    if len(page) != PAGE_SIZE:
        raise AgentError('unsupported_format', '钉钉数据库首页不完整')
    crypt = decoder(derive_key(uid, salt))
    plain = crypt.update(page) + crypt.finalize()
    validate_header(plain)


def decrypt_pages(source, destination, key):
    size = source.stat().st_size
    if not size or size % PAGE_SIZE:
        raise AgentError('snapshot_invalid', '钉钉数据库页不完整，拒绝拼接或猜测尾页')
    crypt = decoder(key)
    with source.open('rb') as incoming, destination.open('xb') as output:
        for index in range(size // PAGE_SIZE):
            page = incoming.read(PAGE_SIZE)
            if len(page) != PAGE_SIZE:
                raise AgentError('snapshot_invalid', '钉钉快照长度发生变化')
            plain = crypt.update(page)
            if index == 0:
                validate_header(plain)
            output.write(plain)
        output.write(crypt.finalize())
    return size // PAGE_SIZE


def wal_checksum(data, state, order):
    if len(data) % 8:
        raise AgentError('snapshot_invalid', '无效WAL校验输入')
    words = struct.unpack(order + 'I' * (len(data) // 4), data)
    s0, s1 = state
    for index in range(0, len(words), 2):
        s0 = (s0 + words[index] + s1) & 0xFFFFFFFF
        s1 = (s1 + words[index + 1] + s0) & 0xFFFFFFFF
    return s0, s1


def replay_wal(source, destination, key):
    """Verify original frame chains before replaying the last committed prefix.

    Accept either source checksum representation only if the entire chain is
    valid in that representation; never switch modes mid-stream or rewrite
    checksums to make damaged input appear valid. Replay into a scratch main DB
    directly, without delegating recovery/checkpoint to SQLite.
    """
    with source.open('rb') as incoming:
        header = incoming.read(32)
        if not header:
            return {'wal_committed_frames': 0, 'wal_ignored_tail': False, 'checksum_mode': None}
        if len(header) != 32:
            raise AgentError('snapshot_invalid', '钉钉WAL头不完整')
        magic, version, page_size = struct.unpack_from('>III', header)
        if magic not in (0x377F0682, 0x377F0683) or version != 3007000 or page_size != PAGE_SIZE:
            raise AgentError('unsupported_format', '钉钉WAL版本或页大小不受支持')
        order = '<' if magic == 0x377F0682 else '>'
        initial = wal_checksum(header[:24], (0, 0), order)
        if initial != struct.unpack_from('>II', header, 24):
            raise AgentError('snapshot_invalid', '钉钉WAL头校验失败')
        states = {'ciphertext': initial, 'plaintext': initial}
        frames, committed, commit_pages, ignored = [], 0, None, False
        crypt = decoder(key)
        while True:
            offset = incoming.tell()
            frame = incoming.read(24)
            if not frame:
                break
            data = incoming.read(PAGE_SIZE)
            if len(frame) != 24 or len(data) != PAGE_SIZE:
                ignored = True
                break
            if frame[8:16] != header[16:24]:
                ignored = True
                break
            number, size = struct.unpack_from('>II', frame)
            if not 1 <= number <= 0xFFFFFFFE or size > 0xFFFFFFFE:
                raise AgentError('snapshot_invalid', '钉钉WAL页号或提交大小无效')
            plain = crypt.update(data)
            expected = struct.unpack_from('>II', frame, 16)
            states = {mode: calculated for mode, state in states.items()
                      if (calculated := wal_checksum(frame[:8] + (data if mode == 'ciphertext' else plain), state, order)) == expected}
            if not states:
                raise AgentError('snapshot_invalid', '钉钉WAL帧校验失败，未接受损坏的提交')
            frames.append((offset + 24, number))
            if size:
                committed, commit_pages = len(frames), size
        crypt.finalize()
        if committed:
            # No writes until all complete frames have passed source checksums.
            main_pages = destination.stat().st_size // PAGE_SIZE
            present = {number for _, number in frames[:committed] if number <= commit_pages}
            if commit_pages > main_pages and commit_pages - main_pages > len(present):
                raise AgentError('snapshot_invalid', '钉钉WAL不能完整覆盖新增页')
            if commit_pages > main_pages and any(p not in present for p in range(main_pages + 1, commit_pages + 1)):
                raise AgentError('snapshot_invalid', '钉钉WAL缺少新增页')
            with destination.open('r+b') as output:
                crypt = decoder(key)
                for offset, number in frames[:committed]:
                    if number > commit_pages:
                        continue
                    incoming.seek(offset)
                    plain = crypt.update(incoming.read(PAGE_SIZE))
                    output.seek((number - 1) * PAGE_SIZE)
                    output.write(plain)
                crypt.finalize()
                output.truncate(commit_pages * PAGE_SIZE)
    return {'wal_committed_frames': committed, 'wal_ignored_tail': ignored or committed < len(frames),
            'checksum_mode': '|'.join(sorted(states))}


@contextmanager
def decrypted_database(account_dir, uid, *, runtime):
    from database_reader import capture_stable
    root = Path(account_dir).resolve()
    source = source_path(root, 'DBFiles/dingtalk.db')
    source_path(root, 'DBFiles/dingtalk.db-wal')
    runtime = Path(runtime)
    if runtime.resolve().is_relative_to(root):
        raise AgentError('unsafe_path', '临时快照目录不能位于钉钉原始账号目录内')
    runtime.mkdir(parents=True, exist_ok=True)
    salt = read_salt(root)
    with tempfile.TemporaryDirectory(dir=runtime, prefix='dingtalk-') as directory:
        encrypted, plain = Path(directory) / 'source.db', Path(directory) / 'plain.db'
        clock = time.monotonic()
        capture = capture_stable(source, encrypted)
        key = derive_key(uid, salt)
        pages = decrypt_pages(encrypted, plain, key)
        wal = Path(str(encrypted) + '-wal')
        replay = replay_wal(wal, plain, key) if wal.exists() else {
            'wal_committed_frames': 0, 'wal_ignored_tail': False, 'checksum_mode': None}
        with plain.open('r+b') as output:
            output.seek(18)
            output.write(b'\x01\x01')
        connection = sqlite3.connect(plain.as_uri() + '?mode=ro&immutable=1', uri=True)
        connection.row_factory = sqlite3.Row
        try:
            result = connection.execute('PRAGMA quick_check').fetchall()
            if len(result) != 1 or result[0][0] != 'ok':
                raise AgentError('snapshot_invalid', '钉钉解密快照结构校验失败')
            metadata = {'source': str(source), 'capture': capture, 'main_pages': pages, **replay,
                        'sqlite_quick_check': 'ok', 'page_authentication': False,
                        'experimental': True, 'snapshot_decrypt_seconds': round(time.monotonic() - clock, 4)}
            yield connection, metadata
        finally:
            connection.close()
