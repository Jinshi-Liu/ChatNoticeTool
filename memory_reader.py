"""Authorized, bounded, read-only client memory scans. Never dump process memory.
WeChat Config.Cipher layout references: fanyuantaier/wechatauto-replica db.py
(Apache-2.0), credited in NOTICE.md. Only VM_READ + QUERY_INFORMATION.
No injection, debug attachment, memory writes, process control, or network I/O.
"""
from __future__ import annotations
import ctypes as C
from ctypes import wintypes as W
import json
import os
from pathlib import Path
import re
import struct
import time

import cipher
from chatnotice import project_path, save_json

READ_ONLY_ACCESS = 0x0010 | 0x0400
CIPHER_NAME = b"com.Tencent.WCDB.Config.Cipher"
XOR_MASK = bytes.fromhex("d2c7442458020000004889442450488b450048844c2448488944254048584c24")
HEX_LITERAL = re.compile(rb"[xX]'([0-9a-fA-F]{64,192})'")
QQ_PASSWORD = re.compile(rb"#[\x21-\x7e]{12}@uJ")


class MBI(C.Structure):
    _fields_ = [("BaseAddress", C.c_void_p), ("AllocationBase", C.c_void_p),
        ("AllocationProtect", W.DWORD), ("PartitionId", W.WORD),
        ("RegionSize", C.c_size_t), ("State", W.DWORD), ("Protect", W.DWORD), ("Type", W.DWORD)]


class DATA_BLOB(C.Structure):
    _fields_ = [("cbData", W.DWORD), ("pbData", C.POINTER(C.c_ubyte))]


def protect_secret(data: bytes) -> bytes:
    crypt = C.WinDLL("crypt32", use_last_error=True)
    kernel = C.WinDLL("kernel32", use_last_error=True)
    crypt.CryptProtectData.argtypes = [C.POINTER(DATA_BLOB), W.LPCWSTR, C.c_void_p,
        C.c_void_p, C.c_void_p, W.DWORD, C.POINTER(DATA_BLOB)]
    crypt.CryptProtectData.restype = W.BOOL
    kernel.LocalFree.argtypes = [C.c_void_p]
    kernel.LocalFree.restype = C.c_void_p
    buf = (C.c_ubyte * len(data)).from_buffer_copy(data)
    source = DATA_BLOB(len(data), buf)
    output = DATA_BLOB()
    if not crypt.CryptProtectData(C.byref(source), "ChatNoticeTool local database keys",
            None, None, None, 1, C.byref(output)):
        raise OSError(C.get_last_error(), "DPAPI protection failed")
    try:
        return C.string_at(output.pbData, output.cbData)
    finally:
        kernel.LocalFree(C.cast(output.pbData, C.c_void_p))


def unprotect_secret(data: bytes) -> bytes:
    crypt = C.WinDLL("crypt32", use_last_error=True)
    kernel = C.WinDLL("kernel32", use_last_error=True)
    crypt.CryptUnprotectData.argtypes = [C.POINTER(DATA_BLOB), C.c_void_p, C.c_void_p,
        C.c_void_p, C.c_void_p, W.DWORD, C.POINTER(DATA_BLOB)]
    crypt.CryptUnprotectData.restype = W.BOOL
    kernel.LocalFree.argtypes = [C.c_void_p]
    kernel.LocalFree.restype = C.c_void_p
    buf = (C.c_ubyte * len(data)).from_buffer_copy(data)
    source = DATA_BLOB(len(data), buf)
    output = DATA_BLOB()
    if not crypt.CryptUnprotectData(C.byref(source), None, None, None, None, 1, C.byref(output)):
        raise OSError(C.get_last_error(), "DPAPI key cache cannot be opened by this Windows account")
    try:
        return C.string_at(output.pbData, output.cbData)
    finally:
        kernel.LocalFree(C.cast(output.pbData, C.c_void_p))


def load_secrets() -> dict:
    path = project_path("state/secrets.dpapi")
    if not path.exists():
        return {"qq": {}, "wechat": {}}
    return json.loads(unprotect_secret(path.read_bytes()).decode("utf-8"))


def store_secrets(secrets: dict) -> None:
    path = project_path("state/secrets.dpapi")
    path.parent.mkdir(parents=True, exist_ok=True)
    protected = protect_secret(json.dumps(secrets).encode("utf-8"))
    tmp = project_path("state/secrets.dpapi.tmp")
    tmp.write_bytes(protected)
    tmp.replace(path)


def merge_verified_secrets(platform: str, updates: dict) -> None:
    lock = project_path("state/secrets.lock")
    lock.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + 10
    while True:
        try:
            handle = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            os.close(handle)
            break
        except FileExistsError:
            if time.monotonic() >= deadline:
                raise TimeoutError("Key-cache lock busy; no existing keys overwritten")
            time.sleep(0.05)
    try:
        secrets = load_secrets()
        secrets.setdefault(platform, {}).update(updates)
        store_secrets(secrets)
    finally:
        lock.unlink(missing_ok=True)


class ReadOnlyProcess:
    def __init__(self, pid: int, platform: str, timeout: float = 120):
        self.kernel = C.WinDLL("kernel32", use_last_error=True)
        k = self.kernel
        k.OpenProcess.argtypes = [W.DWORD, W.BOOL, W.DWORD]
        k.OpenProcess.restype = W.HANDLE
        k.CloseHandle.argtypes = [W.HANDLE]
        k.CloseHandle.restype = W.BOOL
        k.ReadProcessMemory.argtypes = [W.HANDLE, C.c_void_p, C.c_void_p,
            C.c_size_t, C.POINTER(C.c_size_t)]
        k.ReadProcessMemory.restype = W.BOOL
        k.VirtualQueryEx.argtypes = [W.HANDLE, C.c_void_p, C.POINTER(MBI), C.c_size_t]
        k.VirtualQueryEx.restype = C.c_size_t
        k.QueryFullProcessImageNameW.argtypes = [W.HANDLE, W.DWORD, W.LPWSTR, C.POINTER(W.DWORD)]
        k.QueryFullProcessImageNameW.restype = W.BOOL
        self.handle = k.OpenProcess(READ_ONLY_ACCESS, False, pid)
        if not self.handle:
            raise OSError(C.get_last_error(), f"Cannot open PID {pid} for read-only access")
        self.pid = pid
        self.deadline = time.monotonic() + timeout
        self.bytes_read = 0
        self.read_failures = 0
        name = C.create_unicode_buffer(32768)
        size = W.DWORD(len(name))
        if not k.QueryFullProcessImageNameW(self.handle, 0, name, C.byref(size)):
            self.close()
            raise OSError("Cannot verify process identity")
        allowed = {"wechat": {"weixin.exe", "wechat.exe"}, "qq": {"qq.exe"}}[platform]
        if Path(name.value).name.lower() not in allowed:
            self.close()
            raise ValueError("Refusing to scan a process that is not the authorized client")

    def close(self):
        if self.handle:
            self.kernel.CloseHandle(self.handle)
            self.handle = None

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def read(self, address: int, length: int) -> bytes:
        if time.monotonic() > self.deadline:
            raise TimeoutError("Bounded memory scan reached its time limit")
        if not 0x10000 <= address < 0x800000000000 or not 0 < length <= 2 * 1024 * 1024:
            return b""
        buf = C.create_string_buffer(length)
        got = C.c_size_t()
        self.kernel.ReadProcessMemory(self.handle, C.c_void_p(address), buf, length, C.byref(got))
        self.bytes_read += got.value
        if got.value != length:
            self.read_failures += 1
        return buf.raw[:got.value]

    def chunks(self, overlap: int = 256):
        address = 0
        while address < 0x800000000000:
            if time.monotonic() > self.deadline:
                raise TimeoutError("Bounded memory scan reached its time limit")
            mbi = MBI()
            if not self.kernel.VirtualQueryEx(self.handle, C.c_void_p(address), C.byref(mbi), C.sizeof(mbi)):
                break
            base, size = int(mbi.BaseAddress or 0), int(mbi.RegionSize)
            if size <= 0 or base + size <= address:
                break
            readable = (mbi.Protect & 0xFF) in {2, 4, 8, 0x20, 0x40, 0x80}
            if mbi.State == 0x1000 and readable and not mbi.Protect & 0x100:
                offset = 0
                tail = b""
                while offset < size:
                    length = min(2 * 1024 * 1024, size - offset)
                    data = self.read(base + offset, length)
                    if data:
                        yield base + offset - len(tail), tail + data
                        tail = data[-overlap:]
                    else:
                        tail = b""
                    offset += length
            address = base + size

    def find(self, needles: list[bytes], max_hits: int = 4096) -> dict[bytes, list[int]]:
        hits = {needle: [] for needle in needles}
        for base, data in self.chunks(max(256, max(map(len, needles), default=1))):
            for needle in needles:
                start = 0
                while True:
                    pos = data.find(needle, start)
                    if pos < 0:
                        break
                    hit = base + pos
                    if not hits[needle] or hits[needle][-1] != hit:
                        hits[needle].append(hit)
                    if sum(map(len, hits.values())) > max_hits:
                        raise ValueError("Too many memory anchors; stopped without dumping data")
                    start = pos + 1
        return hits


def wx_candidates(process: ReadOnlyProcess) -> set[bytes]:
    anchors = process.find([CIPHER_NAME])[CIPHER_NAME]
    if not anchors:
        return set()
    pairs = [struct.pack("<QQ", address, len(CIPHER_NAME)) for address in anchors]
    found = process.find(pairs)
    candidates: set[bytes] = set()
    for locations in found.values():
        for address in locations:
            node = process.read(address - 0x10, 0x50)
            if len(node) != 0x50:
                continue
            if struct.unpack_from("<Q", node, 0x10)[0] not in anchors:
                continue
            pointer = struct.unpack_from("<Q", node, 0x28)[0]
            if not 0x10000 <= pointer < 0x800000000000:
                continue
            obj = process.read(pointer + 0x88, 0x28)
            if len(obj) != 0x28:
                continue
            data_ptr, data_len = struct.unpack_from("<QQ", obj, 8)
            if not 0 < data_len <= 1024:
                continue
            blob = process.read(data_ptr, data_len)
            if len(blob) != data_len:
                continue
            decoded = bytes(value ^ XOR_MASK[i % len(XOR_MASK)] for i, value in enumerate(blob))
            for match in HEX_LITERAL.finditer(decoded):
                run = match.group(1)
                starts = list(range(0, max(1, len(run) - 63), 32))
                if len(run) > 96:
                    starts.append(len(run) - 64)
                for start in starts:
                    key = bytes.fromhex(run[start:start + 64].decode("ascii"))
                    if len(key) != 32:
                        continue
                    candidates.add(key)
                    if start + 96 <= len(run):
                        candidates.add(key + bytes.fromhex(run[start + 64:start + 96].decode("ascii")))
    return candidates


def qq_candidates(process: ReadOnlyProcess) -> set[bytes]:
    # Conservative known family only, not arbitrary strings/password harvesting.
    # Failure does not imply that all NTQQ passphrases use this pattern.
    candidates = set()
    for _, data in process.chunks():
        for match in QQ_PASSWORD.finditer(data):
            candidates.add(match.group())
            if len(candidates) > 512:
                raise ValueError("QQ candidate limit reached")
    return candidates


def qq_raw_keys(process: ReadOnlyProcess, pages: dict[str, bytes]) -> tuple[dict[str, bytes], dict]:
    """Try codec allocations near known database salts; do not collect other secrets.
    This is a bounded heuristic, not a guarantee or a protection bypass.
    """
    anchors_for = {}
    for path, page in pages.items():
        for marker in (page[:16], bytes(byte ^ 0x3A for byte in page[:16])):
            anchors_for.setdefault(marker, []).append(path)
    hits = process.find(list(anchors_for), max_hits=1024)
    results = {}
    tested = set()
    addresses = {address for locations in hits.values() for address in locations}

    def try_key(candidate: bytes):
        if len(candidate) != 32 or len(set(candidate)) < 15 or candidate in tested:
            return
        tested.add(candidate)
        if len(tested) > 100000:
            raise ValueError("QQ raw-key candidate budget reached")
        for path, page in pages.items():
            if path in results:
                continue
            if cipher.verify_page(page, 1, cipher.derive_qq_raw_keys(candidate, page)):
                results[path] = candidate

    # Salt buffers may be separate allocations. Find references to their addresses,
    # then inspect nearby codec pointers rather than scanning arbitrary key-sized windows.
    references = set()
    if addresses:
        pointers = [struct.pack("<Q", address) for address in addresses]
        reference_hits = process.find(pointers, max_hits=4096)
        references = {address for locations in reference_hits.values() for address in locations}
    for address in sorted(addresses | references):
        base = max(0x10000, (address - 2048) & ~15)
        nearby = process.read(base, 4096)
        for offset in range(0, len(nearby) - 31, 16):
            try_key(nearby[offset:offset + 32])
        # Codec structs generally contain pointers to separately allocated key buffers.
        for offset in range(0, len(nearby) - 7, 8):
            pointer = struct.unpack_from("<Q", nearby, offset)[0]
            if 0x10000 <= pointer < 0x800000000000 and pointer % 8 == 0:
                pointed = process.read(pointer, 128)
                for shift in range(0, max(0, len(pointed) - 31), 16):
                    try_key(pointed[shift:shift + 32])
                if address in references:
                    for field in range(0, len(pointed) - 7, 8):
                        next_pointer = struct.unpack_from("<Q", pointed, field)[0]
                        if 0x10000 <= next_pointer < 0x800000000000 and next_pointer % 8 == 0:
                            try_key(process.read(next_pointer, 32))
        if len(results) == len(pages):
            break
    return results, {"salt_anchors": len(addresses), "salt_pointer_references": len(references),
                     "raw_key_candidates_checked": len(tested)}


def first_pages(config: dict, platform: str) -> dict[str, bytes]:
    root = Path(config[platform]["database_dir"])
    if platform == "qq":
        paths = [root / name for name in ("nt_msg.db", "group_info.db", "group_msg_fts.db")]
        prefix = 1024
    else:
        paths = [root / "contact" / "contact.db"]
        paths += sorted((root / "message").glob("*.db"))
        paths += sorted((root / "session").glob("*.db"))
        prefix = 0
    pages = {}
    for path in paths:
        if not path.is_file():
            continue
        with path.open("rb") as handle:
            handle.seek(prefix)
            page = handle.read(cipher.PAGE_SIZE)
        if len(page) == cipher.PAGE_SIZE:
            pages[str(path)] = page
    return pages


def acquire(config: dict, platform: str, pids: list[int]) -> dict:
    if not config["policy"].get("allow_process_memory_read"):
        raise PermissionError("Process memory read not authorized")
    if C.sizeof(C.c_void_p) != 8:
        raise RuntimeError("64-bit Python required")
    pages = first_pages(config, platform)
    found = {}
    scans = []
    seen = set()
    for pid in pids:
        try:
            with ReadOnlyProcess(pid, platform) as process:
                extra = {}
                if platform == "wechat":
                    candidates = wx_candidates(process)
                else:
                    candidates = qq_candidates(process)
                    raw_found, extra = qq_raw_keys(process, pages)
                    found.update({path: key.hex() for path, key in raw_found.items()})
                fresh = candidates - seen
                seen.update(candidates)
                for key in fresh:
                    for path, page in pages.items():
                        if path in found:
                            continue
                        keys = (cipher.derive_wechat_keys(key, page) if platform == "wechat"
                                else cipher.derive_qq_keys(key, page))
                        if cipher.verify_page(page, 1, keys):
                            # Never expose key material in return value, logs, or CLI.
                            found[path] = key.hex()
                scans.append({"pid": pid, "status": "completed", "candidate_count": len(candidates),
                              "scanned_mb": round(process.bytes_read / 1048576, 1), **extra})
        except (OSError, TimeoutError, ValueError) as exc:
            scans.append({"pid": pid, "status": "failed", "error": str(exc)})
        if len(found) == len(pages):
            break
    if found:
        merge_verified_secrets(platform, found)
    report = {"platform": platform, "verified_databases": len(found),
              "databases_checked": len(pages), "verified_paths": sorted(found),
              "scan_summary": scans, "key_cache_protection": "Windows user-bound DPAPI",
              "read_chat_bodies": False, "wrote_client_memory": False}
    save_json(f"state/{platform}-key-report.json", report)
    return report


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Authorized read-only key acquisition")
    parser.add_argument("platform", choices=["qq", "wechat"])
    parser.add_argument("--pid", action="append", type=int, required=True)
    parser.add_argument('--authorize-memory-read', action='store_true',
                        help='Explicit authorization to read the selected client process memory for this run')
    args = parser.parse_args()
    try:
        from chatnotice import load_config
        config = load_config(args.platform)
        if args.authorize_memory_read: config['policy']['allow_process_memory_read'] = True
        report = acquire(config, args.platform, args.pid)
        print(json.dumps(report, ensure_ascii=False, indent=2))
        if not report['verified_databases']: raise SystemExit(2)
    except (OSError, ValueError, PermissionError) as exc:
        print(f"Acquisition failed: {exc}")
        raise SystemExit(2)
        raise SystemExit(2)
