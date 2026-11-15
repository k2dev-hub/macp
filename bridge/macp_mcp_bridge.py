#!/usr/bin/env python3
"""stdio bridge: expose the MACP MCP gateway to stdio-only clients.

Reads newline-delimited JSON-RPC from stdin, forwards each message to the
gateway over HTTP, and writes the response to stdout.  The MCP session id handed
out by ``initialize`` is kept in memory and replayed on later requests.

Configuration (in the client's mcpServers entry):

    {
      "command": "python3",
      "args": ["/opt/macp/bridge/macp_mcp_bridge.py"],
      "env": {
        "MACP_MCP_URL": "https://macp.example.com/mcp",
        "MACP_MCP_TOKEN": "<project-scoped-mcp-token>"
      }
    }

The token is read from ``MACP_MCP_TOKEN`` or ``MACP_MCP_TOKEN_FILE`` and is never
written to disk, logged or echoed.  TLS verification is disabled by default
because the deployment uses a self-signed certificate; set
``MACP_MCP_VERIFY_TLS=1`` to enforce it.
"""

from __future__ import annotations

import json
import os
import ssl
import sys
import urllib.error
import urllib.request

URL = os.getenv("MACP_MCP_URL", "").strip()
TOKEN = os.getenv("MACP_MCP_TOKEN", "").strip()
if not TOKEN and os.getenv("MACP_MCP_TOKEN_FILE"):
    with open(os.environ["MACP_MCP_TOKEN_FILE"], encoding="utf-8") as handle:
        TOKEN = handle.read().strip()
VERIFY = os.getenv("MACP_MCP_VERIFY_TLS", "0") == "1"

SESSION_ID = ""
CTX = ssl.create_default_context()
if not VERIFY:
    CTX.check_hostname = False
    CTX.verify_mode = ssl.CERT_NONE


def forward(message: dict) -> dict | None:
    global SESSION_ID
    data = json.dumps(message).encode("utf-8")
    request = urllib.request.Request(URL, data=data, method="POST")
    request.add_header("Content-Type", "application/json")
    request.add_header("Accept", "application/json, text/event-stream")
    request.add_header("MCP-Protocol-Version", "2025-06-18")
    if TOKEN:
        request.add_header("Authorization", f"Bearer {TOKEN}")
    if SESSION_ID:
        request.add_header("Mcp-Session-Id", SESSION_ID)
    try:
        with urllib.request.urlopen(request, timeout=1800, context=CTX) as response:
            session = response.headers.get("Mcp-Session-Id")
            if session:
                SESSION_ID = session
            raw = response.read().decode("utf-8")
            return json.loads(raw) if raw.strip() else None
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", "replace")
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return {"jsonrpc": "2.0", "id": message.get("id"),
                    "error": {"code": -32603, "message": raw[:300]}}
    except (urllib.error.URLError, TimeoutError) as exc:
        return {"jsonrpc": "2.0", "id": message.get("id"),
                "error": {"code": -32603, "message": f"gateway unreachable: {exc}"}}


def main() -> int:
    if not URL:
        print("macp bridge: MACP_MCP_URL is required (e.g. https://macp.example.com/mcp)",
              file=sys.stderr)
        return 2
    if not TOKEN:
        print("macp bridge: MACP_MCP_TOKEN or MACP_MCP_TOKEN_FILE is required",
              file=sys.stderr)
        return 2
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            print(json.dumps({"jsonrpc": "2.0", "id": None,
                              "error": {"code": -32700, "message": "parse error"}}),
                  flush=True)
            continue
        response = forward(message)
        # Notifications get HTTP 202 and no body; write nothing back.
        if response is not None:
            print(json.dumps(response), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
