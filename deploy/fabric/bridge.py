#!/usr/bin/env python3
"""HTTP bridge between MACP and the Fabric peer on this host.

Why a bridge: the API container has no Fabric SDK, no gRPC and no MSP material,
and should not grow them.  This process runs next to the peer, holds the admin
identity and shells out to `peer` inside the CLI container.

    POST /anchor        {"build_id","fingerprint",...}  -> {"tx_id","block_number"}
    GET  /anchor/<id>   read the record back from the ledger
    GET  /health        peer reachability

Standard library only, so it runs on the host python without a virtualenv.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

CHANNEL = os.getenv("FABRIC_CHANNEL", "macp")
CHAINCODE = os.getenv("FABRIC_CC_NAME", "provenance")
CLI_CONTAINER = os.getenv("FABRIC_CLI_CONTAINER", "macp-fabric-cli")
TOKEN = os.getenv("FABRIC_BRIDGE_TOKEN", "")
LISTEN_PORT = int(os.getenv("FABRIC_BRIDGE_PORT", "9310"))
# Containers reach the host through the compose network gateway, so listen
# there when it exists; fall back to loopback for a bare host run.
LISTEN_BIND = os.getenv("FABRIC_BRIDGE_BIND", "172.28.0.1")

ORDERER = "orderer.example.com:7050"
PEER = "peer0.org1.example.com:7051"
BASE = "/opt/gopath/src/github.com/hyperledger/fabric/peer"
ORDERER_CA = f"{BASE}/crypto/ordererOrganizations/example.com/orderers/orderer.example.com/msp/tlscacerts/tlsca.example.com-cert.pem"
PEER_CA = f"{BASE}/crypto/peerOrganizations/org1.example.com/peers/peer0.org1.example.com/tls/ca.crt"


def cli(*args: str, timeout: int = 60) -> tuple[int, str]:
    result = subprocess.run(
        ["docker", "exec", CLI_CONTAINER, "peer", *args],
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    return result.returncode, (result.stdout or "") + (result.stderr or "")


def query_function(function: str, args: list[str]) -> tuple[int, str]:
    """Read-only chaincode query (no transaction, no ordering)."""

    return cli(
        "chaincode", "query",
        "--channelID", CHANNEL,
        "--name", CHAINCODE,
        "-c", json.dumps({"function": function, "Args": args}),
        timeout=45,
    )


def query_chaincode(build_id: str) -> tuple[int, str]:
    return query_function("query", [build_id])


def anchor(payload: dict) -> dict:
    build_id = str(payload.get("build_id") or "").strip()
    fingerprint = str(payload.get("fingerprint") or "").strip()
    if not build_id or len(fingerprint) != 64:
        return {"error": "build_id and a 64-hex fingerprint are required"}

    args = [
        build_id,
        fingerprint,
        str(payload.get("artifact_sha256") or ""),
        str(payload.get("project") or ""),
        str(payload.get("task") or ""),
        str(payload.get("builder") or ""),
        str(payload.get("created_at") or ""),
    ]
    code, output = cli(
        "chaincode", "invoke",
        "-o", ORDERER,
        "--ordererTLSHostnameOverride", "orderer.example.com",
        "--tls", "--cafile", ORDERER_CA,
        "-C", CHANNEL, "-n", CHAINCODE,
        "--peerAddresses", PEER, "--tlsRootCertFiles", PEER_CA,
        "-c", json.dumps({"function": "anchor", "Args": args}),
        timeout=90,
    )
    if code != 0:
        return {"error": (output or "invoke failed").strip()[-400:]}

    # 读回确认：链上确实存在且指纹一致。链上记录里的 tx_id 是权威值
    # （stub.GetTxID()），命令行不会打印它。提交后账本不一定立刻可见，
    # 所以要重试几次，否则会退化成"从输出里猜交易号"。
    record = None
    for attempt in range(6):
        qcode, qout = query_chaincode(build_id)
        if qcode == 0:
            try:
                record = json.loads(qout.strip().splitlines()[-1])
                break
            except ValueError:
                record = None
        time.sleep(1)
    if record and record.get("fingerprint") != fingerprint:
        return {"error": "ledger fingerprint differs from the submitted one"}

    tx_id = (record or {}).get("tx_id") or extract_tx_id(output)
    if not tx_id:
        return {"error": "no transaction id in the peer output: " + output.strip()[-300:]}
    if tx_id == fingerprint:
        return {"error": "transaction id equals the fingerprint; refusing to record it"}

    block = extract_block(output)
    if block is None:
        block = channel_height()
    return {
        "tx_id": tx_id,
        "block_number": block,
        "record": record,
        "channel": CHANNEL,
        "chaincode": CHAINCODE,
    }


def extract_tx_id(text: str) -> str | None:
    """Find the transaction id in peer output.

    Only accept a 64-hex token that is *labelled* as a transaction id.  A bare
    64-hex match would happily pick the fingerprint out of the payload, which is
    how a record once ended up with its fingerprint stored as its tx id.
    """

    for pattern in (
        r"txid \[([0-9a-f]{64})\]",
        r'"tx_id"\s*:\s*"([0-9a-f]{64})"',
        r"Transaction ID:\s*([0-9a-f]{64})",
    ):
        match = re.search(pattern, text)
        if match:
            return match.group(1)
    return None


def channel_height() -> int | None:
    """Height of the channel, or None when the peer cannot answer.

    Fabric 2.5 prints the info as JSON (`{"height":42,...}`), older releases
    print `Block Height: 42`; accept both.
    """

    code, output = cli("channel", "getinfo", "-c", CHANNEL, timeout=20)
    if code != 0:
        return None
    match = re.search(r'"height"\s*:\s*(\d+)', output) or re.search(
        r"Block Height:\s*(\d+)", output
    )
    return int(match.group(1)) - 1 if match else None


def extract_block(text: str) -> int | None:
    match = re.search(r"block_num[=:]\s*(\d+)", text)
    if match:
        return int(match.group(1))
    match = re.search(r'"?height"?\s*[=:]\s*(\d+)', text)
    if match:
        return int(match.group(1)) - 1
    return None


class Handler(BaseHTTPRequestHandler):
    server_version = "macp-fabric-bridge/1"

    def log_message(self, fmt: str, *args) -> None:  # quieter logs
        sys.stderr.write("%s %s\n" % (self.address_string(), fmt % args))

    def _send(self, status: int, body: dict) -> None:
        raw = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _authorised(self) -> bool:
        if not TOKEN:
            return True
        header = self.headers.get("Authorization", "")
        return header == "Bearer " + TOKEN

    def do_GET(self) -> None:  # noqa: N802 - http.server API
        if self.path == "/health":
            code, output = cli("channel", "list", timeout=20)
            self._send(200 if code == 0 else 503, {
                "status": "ok" if code == 0 else "unavailable",
                "channel": CHANNEL,
                "chaincode": CHAINCODE,
                "detail": output.strip()[-200:] if code else "",
            })
            return
        if self.path.startswith("/ledger") or self.path == "/stats":
            if not self._authorised():
                self._send(401, {"error": "unauthorised"})
                return
            self._serve_ledger()
            return
        prefix = "/anchor/"
        if self.path.startswith(prefix):
            if not self._authorised():
                self._send(401, {"error": "unauthorised"})
                return
            build_id = self.path[len(prefix):].strip("/")
            code, output = query_chaincode(build_id)
            if code != 0:
                status = 404 if "not found" in output else 502
                self._send(status, {"found": False, "detail": output.strip()[-300:]})
                return
            try:
                record = json.loads(output.strip().splitlines()[-1])
            except ValueError:
                self._send(502, {"found": False, "detail": output.strip()[-200:]})
                return
            self._send(200, {"found": True, "record": record})
            return
        self._send(404, {"error": "not found"})

    def _serve_ledger(self) -> None:
        """GET /ledger?limit=N  and  GET /stats.

        Both are read-only chaincode queries, so the bridge can serve them
        without a transaction.  The ledger view and the consistency check use these to
        compare the chain against the local database.
        """

        parsed = urlsplit(self.path)
        if parsed.path == "/stats":
            code, output = query_function("stats", [])
            if code != 0:
                self._send(502, {"error": output.strip()[-300:]})
                return
            try:
                self._send(200, json.loads(output.strip().splitlines()[-1]))
            except ValueError:
                self._send(502, {"error": "unparsable stats output"})
            return

        limit = 20
        query = parse_qs(parsed.query)
        if query.get("limit"):
            try:
                limit = max(1, min(200, int(query["limit"][0])))
            except ValueError:
                self._send(400, {"error": "limit must be an integer"})
                return

        code, output = query_function("recent", [str(limit)])
        if code != 0:
            self._send(502, {"error": output.strip()[-300:]})
            return
        try:
            records = json.loads(output.strip().splitlines()[-1])
        except ValueError:
            self._send(502, {"error": "unparsable ledger output"})
            return
        block = channel_height()
        self._send(200, {
            "channel": CHANNEL,
            "chaincode": CHAINCODE,
            "block_height": None if block is None else block + 1,
            "count": len(records),
            "records": records,
        })

    def do_POST(self) -> None:  # noqa: N802 - http.server API
        if self.path != "/anchor":
            self._send(404, {"error": "not found"})
            return
        if not self._authorised():
            self._send(401, {"error": "unauthorised"})
            return
        length = int(self.headers.get("Content-Length", "0"))
        try:
            payload = json.loads(self.rfile.read(length) or b"{}")
        except ValueError:
            self._send(400, {"error": "invalid json"})
            return
        try:
            result = anchor(payload)
        except subprocess.TimeoutExpired:
            self._send(504, {"error": "peer timed out"})
            return
        except Exception as exc:  # pragma: no cover - defensive
            self._send(502, {"error": str(exc)[:300]})
            return
        if result.get("error"):
            self._send(502, result)
        else:
            self._send(200, result)


def main() -> None:
    try:
        server = ThreadingHTTPServer((LISTEN_BIND, LISTEN_PORT), Handler)
        bound = LISTEN_BIND
    except OSError:
        server = ThreadingHTTPServer(("127.0.0.1", LISTEN_PORT), Handler)
        bound = "127.0.0.1"
    print(f"fabric bridge listening on {bound}:{LISTEN_PORT} "
          f"channel={CHANNEL} chaincode={CHAINCODE}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
