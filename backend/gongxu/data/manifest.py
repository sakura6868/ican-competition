"""Streaming SHA256 used by encrypted assets and model integrity checks."""

from hashlib import sha256
from pathlib import Path


def sha256_file(path: str | Path) -> str:
    digest = sha256()
    with Path(path).open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
