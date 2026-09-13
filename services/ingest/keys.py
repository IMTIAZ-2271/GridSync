"""Minting the two kinds of credential ingest accepts.

A **device key** signs one device's readings. A **source key** lets a
utility's head-end take part in the commissioning handshake. Both are stored
only as argon2 hashes, exactly as an account password is, and shown once.

The prefixes are there so a leaked key is recognisable in a log -- as a
GridSync credential, and as which kind -- and can be revoked, rather than
looking like any other opaque string.
"""
from __future__ import annotations

import secrets

DEVICE_KEY_PREFIX = "gsk_"
SOURCE_KEY_PREFIX = "gss_"


def mint_device_key() -> str:
    #: Long enough that guessing is hopeless, short enough to paste.
    return DEVICE_KEY_PREFIX + secrets.token_urlsafe(32)


def mint_source_key() -> str:
    return SOURCE_KEY_PREFIX + secrets.token_urlsafe(32)
