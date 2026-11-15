"""Minimal signing service: the only process that holds agent private keys.

The API keeps public keys and asks this service to sign receipt body hashes, so
a compromised API container can *request* signatures (inside the receipt flow)
but cannot exfiltrate the private keys themselves.
"""

from __future__ import annotations

import base64
import hmac
import json
import os
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from fastapi import FastAPI, Header, HTTPException

KEYS_DIR = Path(os.getenv("MACP_SIGNER_KEYS_DIR", "/keys"))
TOKEN_FILE = os.getenv("MACP_SIGNER_TOKEN_FILE", "")


def _token() -> str:
    value = os.getenv("MACP_SIGNER_TOKEN", "").strip()
    if not value and TOKEN_FILE and Path(TOKEN_FILE).exists():
        value = Path(TOKEN_FILE).read_text(encoding="utf-8").strip()
    return value


def _authorized(authorization: str | None) -> bool:
    expected = _token()
    if not expected:
        return False
    if not authorization or not authorization.startswith("Bearer "):
        return False
    return hmac.compare_digest(authorization.removeprefix("Bearer ").strip(), expected)


app = FastAPI(title="macp-signer", docs_url=None, openapi_url=None)


@app.get("/health")
async def health() -> dict:
    keys = len(list(KEYS_DIR.glob("*.pem")))
    return {"status": "ok", "keys": keys, "token_configured": bool(_token())}


@app.post("/v1/sign")
async def sign(body: dict, authorization: str | None = Header(default=None)) -> dict:
    if not _authorized(authorization):
        raise HTTPException(401, {"code": "signer_unauthorized"})
    agent = str(body.get("agent") or "")
    data = body.get("data")
    if not agent or not isinstance(data, str):
        raise HTTPException(422, {"code": "agent_and_data_required"})
    if "/" in agent or ".." in agent:
        raise HTTPException(422, {"code": "invalid_agent"})
    path = KEYS_DIR / f"{agent}.pem"
    if not path.exists():
        raise HTTPException(404, {"code": "key_not_found", "agent": agent})
    key = serialization.load_pem_private_key(path.read_bytes(), password=None)
    signature = base64.b64encode(key.sign(data.encode("utf-8"))).decode("ascii")
    return {"agent": agent, "signature": "ed25519:" + signature, "alg": "ed25519"}


if __name__ == "__main__":  # pragma: no cover
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", "9300")))
