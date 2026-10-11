"""Local, authenticated page decoding primitives; no key extraction or file writes.
Parameters follow SQLCipher and the format references listed in NOTICE.md.
Validated versions are documented in README. Never log keys or decrypted pages.
"""
from __future__ import annotations
from dataclasses import dataclass
import hashlib
import hmac
import struct

PAGE_SIZE = 4096
SQLITE_MAGIC = b"SQLite format 3\x00"


@dataclass(frozen=True, repr=False)
class PageKeys:
    encryption: bytes
    authentication: bytes
    reserve: int
    digest: str


def derive_qq_keys(password: bytes, page1: bytes) -> PageKeys:
    if len(password) != 16 or any(byte > 127 for byte in password):
        raise ValueError("QQ passphrase must be 16 ASCII bytes")
    if len(page1) != PAGE_SIZE:
        raise ValueError("Expected one complete 4096-byte page (QQ prefix excluded)")
    salt = page1[:16]
    encryption = hashlib.pbkdf2_hmac("sha512", password, salt, 4000, dklen=32)
    authentication = hashlib.pbkdf2_hmac("sha512", encryption,
        bytes(byte ^ 0x3A for byte in salt), 2, dklen=32)
    return PageKeys(encryption, authentication, 48, "sha1")


def derive_qq_raw_keys(raw_key: bytes, page1: bytes) -> PageKeys:
    if len(raw_key) != 32 or len(page1) != PAGE_SIZE:
        raise ValueError("Expected a 32-byte QQ encryption key and a complete page")
    authentication = hashlib.pbkdf2_hmac("sha512", raw_key,
        bytes(byte ^ 0x3A for byte in page1[:16]), 2, dklen=32)
    return PageKeys(raw_key, authentication, 48, "sha1")


def derive_wechat_keys(raw_key: bytes, page1: bytes) -> PageKeys:
    if len(page1) != PAGE_SIZE or len(raw_key) not in (32, 48):
        raise ValueError("Expected a complete page and a 32/48-byte WeChat raw key")
    encryption = raw_key[:32]
    salt = raw_key[32:] if len(raw_key) == 48 else page1[:16]
    authentication = hashlib.pbkdf2_hmac("sha512", encryption,
        bytes(byte ^ 0x3A for byte in salt), 2, dklen=32)
    return PageKeys(encryption, authentication, 80, "sha512")


def verify_page(page: bytes, page_number: int, keys: PageKeys) -> bool:
    if len(page) != PAGE_SIZE or type(page_number) is not int or not 1 <= page_number <= 0xFFFFFFFF:
        return False
    start = 16 if page_number == 1 else 0
    iv_end = PAGE_SIZE - keys.reserve + 16
    digest_len = hashlib.new(keys.digest).digest_size
    stored = page[iv_end:iv_end + digest_len]
    calculated = hmac.new(keys.authentication,
        page[start:iv_end] + struct.pack("<I", page_number), keys.digest).digest()
    return hmac.compare_digest(stored, calculated)


def decrypt_page(page: bytes, page_number: int, keys: PageKeys) -> bytes:
    if not verify_page(page, page_number, keys):
        raise ValueError("Page authentication failed: wrong key, wrong format, or inconsistent snapshot")
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    start = 16 if page_number == 1 else 0
    payload_end = PAGE_SIZE - keys.reserve
    iv = page[payload_end:payload_end + 16]
    decoder = Cipher(algorithms.AES(keys.encryption), modes.CBC(iv)).decryptor()
    plaintext = decoder.update(page[start:payload_end]) + decoder.finalize()
    return (SQLITE_MAGIC if page_number == 1 else b"") + plaintext + bytes(keys.reserve)
