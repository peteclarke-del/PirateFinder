# Vendored from Amiga File Forge, app/errors.py.
# Copyright (c) 2026 Pete Clarke. MIT licence, full text in vendor/__init__.py.
# Reduced to DMSError, the only exception dms.py and dms_codec.py use.

"""Shared application exceptions."""

from __future__ import annotations


class DMSError(ValueError):
    """The bytes are not a usable DiskMasher archive."""
