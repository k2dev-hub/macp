"""Run one real DeepSeek task through MACP and wait for review.

Prereq: macp-worker-deepseek is running and idle.
The script uses only the MACP write token; the DeepSeek key stays in the
worker's secret file and is never read here.

    MACP_WRITE_TOKEN=... MACP_BASE=http://macp.example.com:8088 \
      python3 -m agents.deepseek_smoke
"""

from __future__ import annotations

import json
import time
import uuid

from .common import request

WORKER = "worker-deepseek"


def main() -> None:
    code = f"T-9{int(time.time() * 1000) % 100000}"[:7]
    status, body = request(
        "POST",
        "/v1/tasks",
        {
            "code": code,
            "title": "Implement multiply(a, b) in solution.py and make it compile",
            "workstream": "stream-core",
            "kind": "slice",
            "priority": "P1",
        },
    )
    if status != 201:
        raise SystemExit(f"create failed: {status} {body}")
    print("created", code)

    status, offer = request(
        "POST", f"/v1/tasks/{code}/dispatch", {"agent": WORKER}
    )
    if status != 201:
        raise SystemExit(f"dispatch failed: {status} {offer}")
    print("dispatched", offer["assignment_id"])

    deadline = time.time() + 180
    while time.time() < deadline:
        status, detail = request("GET", f"/v1/tasks/{code}")
        if status != 200:
            time.sleep(3)
            continue
        state = detail["state"]
        print("state", state)
        if state == "blocked":
            print(json.dumps(detail.get("events", [])[:6], ensure_ascii=False, indent=2))
            raise SystemExit("DeepSeek blocked; inspect worker logs")
        if state == "review":
            receipt_id = None
            for event in detail.get("events", []):
                if event["type"] == "receipt.submitted":
                    receipt_id = (event.get("payload") or {}).get("receipt_id")
                    break
            if not receipt_id:
                raise SystemExit("review without receipt.submitted")
            status, review = request(
                "POST",
                f"/v1/tasks/{code}/reviews",
                {
                    "receipt_id": receipt_id,
                    "decision": "accept",
                    "findings": [],
                    "reviewer": "coordinator-1",
                },
            )
            if status != 200:
                raise SystemExit(f"review failed: {status} {review}")
            print("accepted", code, "receipt", receipt_id)
            status, final = request("GET", f"/v1/tasks/{code}")
            print("final state", final["state"])
            print("events", [e["type"] for e in final.get("events", [])[:8]])
            return
        time.sleep(3)
    raise SystemExit("timeout waiting for review")


if __name__ == "__main__":
    main()
