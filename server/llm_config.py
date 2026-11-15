"""Encrypted LLM provider configuration."""

from __future__ import annotations

import os
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

KEY_FILE = os.getenv("MACP_LLM_MASTER_KEY_FILE", "/run/secrets/llm_master_key")


def _fernet() -> Fernet:
    key = Path(KEY_FILE).read_bytes().strip()
    return Fernet(key)


def encrypt(value: str) -> str:
    return _fernet().encrypt(value.encode("utf-8")).decode("ascii")


def decrypt(value: str | None) -> str:
    if not value:
        return ""
    try:
        return _fernet().decrypt(value.encode("ascii")).decode("utf-8")
    except (InvalidToken, ValueError):
        return ""


def mask(value: str | None) -> dict:
    plain = decrypt(value)
    if not plain:
        return {"api_key_set": False, "api_key_hint": ""}
    return {"api_key_set": True, "api_key_hint": "已配置"}
