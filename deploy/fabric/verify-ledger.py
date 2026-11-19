#!/usr/bin/env python3
"""Check that the consortium chain and the local database agree.

For every build record the database knows about, read the fingerprint back from
the chain and compare.  The four outcomes are the whole point of the exercise:

  consistent   both sides hold the same fingerprint
  local-only   recorded locally but never anchored (chain down, or disabled)
  chain-only   anchored but the local row is gone (database restored from an
               older backup, or a different project)
  mismatch     both exist and disagree — this must never happen

Usage
  python3 deploy/fabric/verify-ledger.py                # table + exit code
  python3 deploy/fabric/verify-ledger.py --json         # machine-readable
  python3 deploy/fabric/verify-ledger.py --limit 500    # scan more chain records

Exit code
  0  every record is consistent (or the chain is disabled and --allow-disable)
  1  at least one local-only / chain-only / mismatch record
"""

from __future__ import annotations

import argparse
import json
import os
import ssl
import sys
import urllib.error
import urllib.request

BASE = os.getenv("MACP_BASE", "https://localhost:9443")
TOKEN_FILE = os.getenv("MACP_TOKEN_FILE", "deploy/secrets/write_token")
CTX = ssl.create_default_context()
CTX.check_hostname = False
CTX.verify_mode = ssl.CERT_NONE


def api(path: str, token: str):
    request = urllib.request.Request(BASE + path)
    request.add_header("Authorization", "Bearer " + token)
    with urllib.request.urlopen(request, timeout=60, context=CTX) as response:
        return json.loads(response.read().decode("utf-8"))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=200, help="chain records to scan (1-200)")
    parser.add_argument("--json", action="store_true", help="print JSON instead of a table")
    parser.add_argument(
        "--allow-disable",
        action="store_true",
        help="exit 0 when Fabric is not configured",
    )
    args = parser.parse_args()

    token = open(TOKEN_FILE, encoding="utf-8").read().strip()
    try:
        view = api(f"/v1/fabric/ledger?limit={args.limit}", token)
    except urllib.error.URLError as exc:
        print(f"无法访问 API {BASE}: {exc}", file=sys.stderr)
        return 1

    chain = view.get("chain") or {}
    if not chain.get("enabled"):
        print("联盟链未启用（[fabric] enabled = false）")
        return 0 if args.allow_disable else 1
    if chain.get("error"):
        print(f"链不可达: {chain['error']}")
        return 1

    comparison = view.get("comparison") or []
    totals = view.get("totals") or {}

    if args.json:
        print(json.dumps(view, ensure_ascii=False, indent=2))
    else:
        print(f"通道 {chain.get('channel')} · 链码 {chain.get('chaincode')} "
              f"· 区块高度 {chain.get('block_height')}")
        print(f"本地记录 {totals.get('local_records')} · 已锚定 {totals.get('anchored')} "
              f"· 链上 {totals.get('on_chain')} · 一致 {totals.get('consistent')} "
              f"· 不一致 {totals.get('mismatch')}")
        print()
        print(f"{'状态':<12}{'构建编号':<20}{'链上时间':<22}{'链上指纹':<20}")
        print("-" * 76)
        for item in comparison:
            print(
                f"{item['state']:<12}"
                f"{item['build_id']:<20}"
                f"{(item['anchored_at'] or '')[:19]:<22}"
                f"{(item['chain_fingerprint'] or '')[:16]:<20}"
            )

    bad = [item for item in comparison if item["state"] != "consistent"]
    if bad:
        print()
        print(f"发现 {len(bad)} 条不一致：")
        for item in bad:
            print(f"  {item['state']:<12}{item['build_id']}  {item['detail']}")
        return 1

    print()
    print(f"全部一致（{len(comparison)} 条）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
