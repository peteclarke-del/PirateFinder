# Vendored from Amiga File Forge, app/checksum.py.
# Copyright (c) 2026 Pete Clarke. MIT licence, full text in vendor/__init__.py.
# Reduced to sha256_bytes, the only function dms.py uses.

"""Streaming checksums for image files and generated archives."""

from __future__ import annotations

import hashlib


def sha256_bytes(data: bytes) -> str:
    """Return the SHA-256 digest for an in-memory payload."""
    return hashlib.sha256(data).hexdigest()
