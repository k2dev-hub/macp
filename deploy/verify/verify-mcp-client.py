#!/usr/bin/env python3
"""Real MCP client check for the MACP gateway (official MCP Python SDK).

Connects over Streamable HTTP exactly like Claude Code / Cursor would, then:
  initialize -> tools/list -> tools/call(macp_task_get) -> resources/read.

Usage (on macp.example.com):
    MCP_TOKEN_FILE=/opt/macp/deploy/secrets/mcp_readonly_token \
    MCP_URI=https://macp.example.com:9443/mcp MCP_TASK=T-1234 \
    /opt/macp-mcp-client/bin/python deploy/verify/verify-mcp-client.py
"""

from __future__ import annotations

import asyncio
import inspect
import os
import ssl
import sys

import httpx2
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

URI = os.getenv("MCP_URI", "https://localhost:9443/mcp")
TOKEN_FILE = os.getenv("MCP_TOKEN_FILE", "/opt/macp/deploy/secrets/mcp_readonly_token")
TASK = os.getenv("MCP_TASK", "")

failures: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(("PASS " if ok else "FAIL ") + name + ((" | " + str(detail)[:160]) if detail else ""))
    if not ok:
        failures.append(name)


async def main() -> int:
    token = open(TOKEN_FILE, encoding="utf-8").read().strip()
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    # The SDK talks to HTTP through httpx2.  Verification is disabled because the
    # deployment uses a self-signed certificate (docs/MCP.md tells clients to
    # trust it or allow insecure TLS).
    http_client = httpx2.AsyncClient(
        headers={"Authorization": f"Bearer {token}"}, verify=False, timeout=60
    )

    streams = streamable_http_client(URI, http_client=http_client)
    # The SDK yields either (read, write) or (read, write, get_session_id); the
    # extra element is the session id accessor, which we do not need.
    if inspect.isasyncgen(streams):
        entered = streams.__aiter__()
    else:  # pragma: no cover - future SDKs
        entered = None

    async with streams as transport:
        read, write = transport[0], transport[1]
        async with ClientSession(read, write) as session:
            info = await session.initialize()
            # SDK 2.x uses snake_case models; keep both spellings working.
            server_info = getattr(info, "serverInfo", None) or getattr(info, "server_info", None)
            version = getattr(info, "protocolVersion", None) or getattr(info, "protocol_version", None)
            check("initialize handshake", getattr(server_info, "name", None) == "macp", server_info)
            check("protocol version", bool(version), version)

            tools = await session.list_tools()
            names = [t.name for t in tools.tools]
            check("tools/list", len(names) >= 6, names)
            check("no write tools in Phase 1",
                  "macp_shell_run" not in names and "macp_file_write" not in names, names)

            if TASK:
                result = await session.call_tool("macp_task_get", {"task": TASK})
                text = result.content[0].text if result.content else ""
                is_error = getattr(result, "isError", None)
                if is_error is None:
                    is_error = getattr(result, "is_error", False)
                check("tools/call macp_task_get", not is_error and TASK in text, text[:100])

                resource = await session.read_resource(f"macp://task/{TASK}")
                body = resource.contents[0].text if resource.contents else ""
                check("resources/read macp://task", TASK in body, body[:100])

    await http_client.aclose()
    print()
    print("FAILURES:", failures or "none")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
