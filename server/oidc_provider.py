"""Minimal built-in OIDC provider for MACP.

Supports authorization-code login with a password form, plus a password grant
for automated tests.  Keys and secrets are mounted from deploy/secrets.
"""

from __future__ import annotations

import base64
import hashlib
import html
import os
import secrets
import time
from pathlib import Path
from urllib.parse import urlencode

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from fastapi import FastAPI, Form, Header, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from .settings import SETTINGS as settings

ISSUER = os.getenv("OIDC_ISSUER", settings.oidc_issuer).rstrip("/")
CLIENT_ID = os.getenv("OIDC_CLIENT_ID", str(settings.get("oidc.client_id") or "macp"))
KEY_FILE = os.getenv("OIDC_KEY_FILE", "/run/secrets/oidc_signing_key")
CLIENT_SECRET_FILE = os.getenv(
    "OIDC_CLIENT_SECRET_FILE", "/run/secrets/oidc_client_secret"
)
ADMIN_PASSWORD_FILE = os.getenv(
    "OIDC_ADMIN_PASSWORD_FILE", "/run/secrets/oidc_admin_password"
)
REDIRECT_URIS = [
    uri.strip()
    for uri in os.getenv(
        "OIDC_REDIRECT_URIS",
        # Configured allow-list; defaults to local development only.
        ",".join(settings.get("oidc.redirect_uris") or []),
    ).split(",")
    if uri.strip()
]
ADMIN_EMAIL = os.getenv("OIDC_ADMIN_EMAIL", "admin@macp.local")
KID = "macp-1"

app = FastAPI(title="MACP OIDC Provider", version="0.1.0")
CODES: dict[str, dict] = {}


def read_secret(path: str) -> str:
    return Path(path).read_text(encoding="utf-8").strip()


def private_key():
    return serialization.load_pem_private_key(Path(KEY_FILE).read_bytes(), password=None)


def b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def sign_jwt(claims: dict) -> str:
    header = {"alg": "RS256", "typ": "JWT", "kid": KID}
    payload = {**claims, "iss": ISSUER, "aud": CLIENT_ID, "iat": int(time.time())}
    signing_input = (
        b64(__import__("json").dumps(header, separators=(",", ":")).encode())
        + "."
        + b64(__import__("json").dumps(payload, separators=(",", ":")).encode())
    )
    signature = private_key().sign(
        signing_input.encode("ascii"), padding.PKCS1v15(), hashes.SHA256()
    )
    return signing_input + "." + b64(signature)


def access_token(sub: str, email: str, name: str, roles: list[str]) -> str:
    now = int(time.time())
    return sign_jwt(
        {
            "sub": sub,
            "email": email,
            "name": name,
            "roles": roles,
            "exp": now + 3600,
            "typ": "Bearer",
        }
    )


def verify(token: str) -> dict:
    import json

    try:
        header_b64, payload_b64, signature_b64 = token.split(".")
        signing_input = (header_b64 + "." + payload_b64).encode("ascii")
        signature = base64.urlsafe_b64decode(signature_b64 + "=" * (-len(signature_b64) % 4))
        private_key().public_key().verify(
            signature, signing_input, padding.PKCS1v15(), hashes.SHA256()
        )
        payload = json.loads(
            base64.urlsafe_b64decode(payload_b64 + "=" * (-len(payload_b64) % 4))
        )
    except Exception as exc:
        raise HTTPException(401, "invalid token") from exc
    if payload.get("iss") != ISSUER or payload.get("aud") != CLIENT_ID:
        raise HTTPException(401, "invalid issuer/audience")
    if int(payload.get("exp", 0)) < int(time.time()):
        raise HTTPException(401, "token expired")
    return payload


@app.get("/.well-known/openid-configuration")
async def discovery():
    return {
        "issuer": ISSUER,
        "authorization_endpoint": f"{ISSUER}/authorize",
        "token_endpoint": f"{ISSUER}/token",
        "userinfo_endpoint": f"{ISSUER}/userinfo",
        "jwks_uri": f"{ISSUER}/jwks",
        "response_types_supported": ["code"],
        "subject_types_supported": ["public"],
        "id_token_signing_alg_values_supported": ["RS256"],
        "scopes_supported": ["openid", "profile", "email"],
        "token_endpoint_auth_methods_supported": ["client_secret_post"],
    }


@app.get("/jwks")
async def jwks():
    numbers = private_key().public_key().public_numbers()
    return {
        "keys": [
            {
                "kty": "RSA",
                "use": "sig",
                "alg": "RS256",
                "kid": KID,
                "n": b64(numbers.n.to_bytes((numbers.n.bit_length() + 7) // 8, "big")),
                "e": b64(numbers.e.to_bytes((numbers.e.bit_length() + 7) // 8, "big")),
            }
        ]
    }


def login_page(params: dict, error: str = "") -> HTMLResponse:
    hidden = "".join(
        f'<input type="hidden" name="{html.escape(k)}" value="{html.escape(v)}">'
        for k, v in params.items()
    )
    return HTMLResponse(
        f"""<!doctype html><html><head><meta charset="utf-8"><title>MACP SSO</title>
<style>
*{{box-sizing:border-box}}
html,body{{height:100%}}
body{{font-family:system-ui,-apple-system,"Segoe UI","PingFang SC",sans-serif;background:radial-gradient(circle at 20% 10%,#16365f,#0b1220 60%);color:#fff;display:flex;align-items:center;justify-content:center;margin:0;padding:16px}}
.card{{width:360px;max-width:100%;background:#182334;padding:32px;border-radius:14px;box-shadow:0 18px 50px #0008;border:1px solid #2b3b52}}
h2{{margin:0 0 4px;font-size:22px}}
.sub{{color:#98a2b3;font-size:13px;margin:0 0 22px}}
label{{display:block;font-size:12px;color:#98a2b3;margin:0 0 6px}}
input{{display:block;width:100%;height:42px;padding:0 12px;margin:0 0 16px;border-radius:8px;border:1px solid #3b4a61;background:#0f1724;color:#fff;font-size:14px;outline:none}}
input:focus{{border-color:#3b82f6;box-shadow:0 0 0 3px rgba(59,130,246,.18)}}
button{{display:block;width:100%;height:42px;border:0;border-radius:8px;background:#2563eb;color:#fff;font-weight:600;font-size:14px;cursor:pointer}}
button:hover{{background:#1d4ed8}}
.err{{color:#fda29b;background:#3b1d1d;border:1px solid #7a2e2e;border-radius:8px;padding:9px 12px;font-size:12px;margin-bottom:14px}}
</style></head><body>
<form class="card" method="post" action="{ISSUER}/authorize">
<h2>MACP SSO</h2><p class="sub">使用管理员账号登录</p>
{f'<div class="err">{html.escape(error)}</div>' if error else ''}
{hidden}
<label>账号</label><input name="username" value="admin" autocomplete="username">
<label>密码</label><input name="password" type="password" autocomplete="current-password">
<button type="submit">登录</button>
</form></body></html>"""
    )


@app.get("/authorize")
async def authorize(
    client_id: str,
    redirect_uri: str,
    response_type: str = "code",
    state: str = "",
    scope: str = "openid",
):
    if client_id != CLIENT_ID or redirect_uri not in REDIRECT_URIS:
        raise HTTPException(400, "invalid client or redirect_uri")
    if response_type != "code":
        raise HTTPException(400, "only response_type=code is supported")
    return login_page(
        {
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "response_type": response_type,
            "state": state,
            "scope": scope,
        }
    )


@app.post("/authorize")
async def authorize_submit(
    client_id: str = Form(...),
    redirect_uri: str = Form(...),
    response_type: str = Form("code"),
    state: str = Form(""),
    scope: str = Form("openid"),
    username: str = Form(...),
    password: str = Form(...),
):
    if client_id != CLIENT_ID or redirect_uri not in REDIRECT_URIS:
        raise HTTPException(400, "invalid client or redirect_uri")
    expected = read_secret(ADMIN_PASSWORD_FILE)
    if username != "admin" or not secrets.compare_digest(password, expected):
        return login_page(
            {
                "client_id": client_id,
                "redirect_uri": redirect_uri,
                "response_type": response_type,
                "state": state,
                "scope": scope,
            },
            "账号或密码错误",
        )
    code = secrets.token_urlsafe(32)
    CODES[code] = {
        "sub": "admin",
        "email": ADMIN_EMAIL,
        "name": "MACP Admin",
        "roles": ["macp_admin"],
        "redirect_uri": redirect_uri,
        "expires": time.time() + 300,
    }
    query = urlencode({"code": code, "state": state})
    return RedirectResponse(f"{redirect_uri}?{query}", status_code=302)


@app.post("/token")
async def token(request: Request):
    form = await request.form()
    grant_type = form.get("grant_type")
    client_id = form.get("client_id")
    client_secret = form.get("client_secret")
    if client_id != CLIENT_ID or not secrets.compare_digest(
        str(client_secret or ""), read_secret(CLIENT_SECRET_FILE)
    ):
        raise HTTPException(401, "invalid client credentials")
    if grant_type == "password":
        # Dev/test convenience; production clients should use authorization code.
        username = str(form.get("username") or "")
        password = str(form.get("password") or "")
        if username != "admin" or not secrets.compare_digest(
            password, read_secret(ADMIN_PASSWORD_FILE)
        ):
            raise HTTPException(401, "invalid resource owner credentials")
        claims = {
            "sub": "admin",
            "email": ADMIN_EMAIL,
            "name": "MACP Admin",
            "roles": ["macp_admin"],
        }
    elif grant_type == "authorization_code":
        code = str(form.get("code") or "")
        entry = CODES.pop(code, None)
        if not entry or entry["expires"] < time.time():
            raise HTTPException(400, "invalid or expired code")
        if form.get("redirect_uri") != entry["redirect_uri"]:
            raise HTTPException(400, "redirect_uri mismatch")
        claims = {
            key: entry[key] for key in ("sub", "email", "name", "roles")
        }
    else:
        raise HTTPException(400, "unsupported grant_type")
    token_value = access_token(
        claims["sub"], claims["email"], claims["name"], claims["roles"]
    )
    return {
        "access_token": token_value,
        "id_token": token_value,
        "token_type": "Bearer",
        "expires_in": 3600,
    }


@app.get("/userinfo")
async def userinfo(authorization: str | None = Header(default=None)):
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "bearer token required")
    claims = verify(authorization.removeprefix("Bearer ").strip())
    return {
        "sub": claims["sub"],
        "email": claims.get("email"),
        "name": claims.get("name"),
        "roles": claims.get("roles", []),
    }


@app.get("/health")
async def health():
    private_key()
    return {"status": "ok", "issuer": ISSUER}
