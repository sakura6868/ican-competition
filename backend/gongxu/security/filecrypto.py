"""文件级 AES-256-GCM 分块加解密。

大文件（视频）无法整体载入内存，按块加密。格式：
每块 = 12 字节 nonce + 密文+tag，连续写入；块大小固定 1 MiB。
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import BinaryIO

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from gongxu.security.crypto import _load_master_key

_NONCE_LEN = 12
CHUNK_BYTES = 1024 * 1024


def _key() -> bytes:
    return _load_master_key()


def encrypt_file(src: Path, dst: Path) -> None:
    key = _key()
    dst.parent.mkdir(parents=True, exist_ok=True)
    with src.open("rb") as fin, dst.open("wb") as fout:
        while chunk := fin.read(CHUNK_BYTES):
            nonce = os.urandom(_NONCE_LEN)
            fout.write(nonce)
            fout.write(AESGCM(key).encrypt(nonce, chunk, None))


def decrypt_file(src: Path, dst: Path) -> None:
    key = _key()
    dst.parent.mkdir(parents=True, exist_ok=True)
    try:
        with src.open("rb") as fin, dst.open("wb") as fout:
            while True:
                nonce = fin.read(_NONCE_LEN)
                if not nonce:
                    break
                ciphertext = fin.read(CHUNK_BYTES + 16)
                if len(ciphertext) == 0:
                    break
                fout.write(AESGCM(key).decrypt(nonce, ciphertext, None))
    except InvalidTag as exc:
        raise ValueError("文件密文校验失败（错误密钥或篡改）") from exc


def encrypt_stream(stream: BinaryIO, dst: Path) -> None:
    key = _key()
    dst.parent.mkdir(parents=True, exist_ok=True)
    with dst.open("wb") as fout:
        while chunk := stream.read(CHUNK_BYTES):
            nonce = os.urandom(_NONCE_LEN)
            fout.write(nonce)
            fout.write(AESGCM(key).encrypt(nonce, chunk, None))
