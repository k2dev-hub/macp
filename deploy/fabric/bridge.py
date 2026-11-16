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
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

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


def query_chaincode(build_id: str) -> tuple[int, str]:
    return cli(
        "chaincode", "query",
        "--channelID", CHANNEL,
        "--name", CHAINCODE,
        "-c", json.dumps({"function": "query", "Args": [build_id]}),
        timeout=30,
    )


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
    # （stub.GetTxID()），命令行不会打印它。
    qcode, qout = query_chaincode(build_id)
    record = None
    if qcode == 0:
        try:
            record = json.loads(qout.strip().splitlines()[-1])
        except ValueError:
            record = None
    if record and record.get("fingerprint") != fingerprint:
        return {"error": "ledger fingerprint differs from the submitted one"}

    tx_id = (record or {}).get("tx_id") or extract_tx_id(output)
    if not tx_id:
        return {"error": "no transaction id in the peer output: " + output.strip()[-300:]}

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
    match = re.search(r"txid \[([0-9a-f]{64})\]", text)
    if match:
        return match.group(1)
    match = re.search(r"\b([0-9a-f]{64})\b", text)
    return match.group(1) if match else None


def channel_height() -> int | None:
    code, output = cli("channel", "getinfo", "-c", CHANNEL, timeout=20)
    if code != 0:
        return None
    match = re.search(r"Block Height:\s*(\d+)", output)
    return int(match.group(1)) - 1 if match else None


def extract_block(text: str) -> int | None:
    match = re.search(r"block_num[=:]\s*(\d+)", text)
    if match:
        return int(match.group(1))
    match = re.search(r"height[=:]\s*(\d+)", text)
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
