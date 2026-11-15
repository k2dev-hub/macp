"""OIDC access-token verification for human principals."""

from __future__ import annotations

import base64
import json
import os
import time
import urllib.request

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from fastapi import HTTPException

from .settings import SETTINGS as settings

ISSUER = os.getenv("OIDC_ISSUER", settings.oidc_issuer).rstrip("/")
JWKS_URL = os.getenv("OIDC_JWKS_URL", "http://oidc:9100/jwks")
CLIENT_ID = os.getenv("OIDC_CLIENT_ID", str(settings.get("oidc.client_id") or "macp"))
CACHE_SECONDS = 300
_cache: dict = {"at": 0.0, "keys": {}}


def _b64decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def _load_keys() -> dict:
    now = time.time()
    if _cache["keys"] and now - _cache["at"] < CACHE_SECONDS:
        return _cache["keys"]
    with urllib.request.urlopen(JWKS_URL, timeout=5) as response:
        body = json.loads(response.read().decode("utf-8"))
    keys = {}
    for item in body.get("keys", []):
        n = int.from_bytes(_b64decode(item["n"]), "big")
        e = int.from_bytes(_b64decode(item["e"]), "big")
        keys[item["kid"]] = rsa.RSAPublicNumbers(e, n).public_key()
    _cache.update(at=now, keys=keys)
    return keys


def verify_token(token: str) -> dict:
    try:
        header_b64, payload_b64, signature_b64 = token.split(".")
        header = json.loads(_b64decode(header_b64))
        payload = json.loads(_b64decode(payload_b64))
        keys = _load_keys()
        public_key = keys.get(header.get("kid"))
        if public_key is None:
            raise ValueError("unknown kid")
        public_key.verify(
            _b64decode(signature_b64),
            (header_b64 + "." + payload_b64).encode("ascii"),
            padding.PKCS1v15(),
            hashes.SHA256(),
        )
    except Exception as exc:
        raise HTTPException(401, {"code": "invalid_oidc_token", "detail": str(exc)}) from exc
    if payload.get("iss") != ISSUER or payload.get("aud") != CLIENT_ID:
        raise HTTPException(401, {"code": "invalid_oidc_token", "detail": "issuer/audience"})
    if int(payload.get("exp", 0)) < int(time.time()):
        raise HTTPException(401, {"code": "invalid_oidc_token", "detail": "expired"})
    return payload


def scopes_for(claims: dict) -> set[str]:
    roles = set(claims.get("roles") or [])
    if "macp_admin" in roles:
        return {"*"}
    scopes = {
        "task:create",
        "task:dispatch",
        "task:claim",
        "task:heartbeat",
        "task:release",
        "receipt:write",
        "review:write",
        "merge:write",
        "lease:write",
        "approval:write",
        "approval:read",
        "tool:invoke",
    }
    if "macp_operator" not in roles:
        scopes = {"task:create", "review:write"}
    return scopes
