"""Canonical JSON and hashing helpers shared by the API and simulator."""

from __future__ import annotations

import hashlib
import json
from typing import Any


def canonical_json(value: Any) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def event_hash(prev_hash: str | None, body: dict[str, Any]) -> str:
    payload = (prev_hash or "") + "\n" + canonical_json(body).decode("utf-8")
    return "sha256:" + sha256_hex(payload.encode("utf-8"))
