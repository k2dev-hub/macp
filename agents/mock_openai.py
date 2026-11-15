"""Mock OpenAI-compatible server for exercising the MACP tool loop.

The mock is deterministic: write file -> compile gate -> commit -> submit ->
receipt. Replace this service with a real OpenAI-compatible base_url later.
"""

from __future__ import annotations

import json
import re
import time
import uuid

from fastapi import FastAPI

app = FastAPI(title="MACP Mock OpenAI", version="0.1.0")


def response(message: dict, finish: str = "stop") -> dict:
    return {
        "id": "chatcmpl-mock-" + uuid.uuid4().hex[:8],
        "object": "chat.completion",
        "created": int(time.time()),
        "model": "mock-tool-model",
        "choices": [{"index": 0, "message": message, "finish_reason": finish}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
    }


def call(call_id: str, name: str, arguments: dict) -> dict:
    return {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {
                "id": call_id,
                "type": "function",
                "function": {"name": name, "arguments": json.dumps(arguments)},
            }
        ],
    }


def parse_tool(message: dict) -> dict:
    try:
        return json.loads(message.get("content") or "{}")
    except json.JSONDecodeError:
        return {}


@app.get("/v1/models")
async def models():
    return {"object": "list", "data": [{"id": "mock-tool-model", "object": "model"}]}


@app.post("/v1/chat/completions")
async def chat(body: dict):
    messages = body.get("messages", [])
    tool_results = [m for m in messages if m.get("role") == "tool"]
    step = len(tool_results)

    if step == 0:
        text = "\n".join(str(m.get("content") or "") for m in messages)
        match = re.search(r"Task (T-[A-Za-z0-9-]+)", text)
        attempt = re.search(r"attempt: (\d+)", text)
        tag = match.group(1) if match else uuid.uuid4().hex[:8]
        if attempt:
            tag = f"{tag}-a{attempt.group(1)}"
        return response(
            call(
                "call_write",
                "workspace_write_file",
                {
                    "path": "src/task_" + tag.replace("-", "_") + ".py",
                    "content": f"# {tag}\ndef add(a, b):\n    return a + b\n",
                },
            ),
            "tool_calls",
        )

    if step == 1:
        return response(
            call("call_gate", "gate_run", {"gate": "compile"}),
            "tool_calls",
        )

    if step == 2:
        gate = parse_tool(tool_results[1]).get("output") or {}
        if gate.get("exit") != 0:
            return response(
                call(
                    "call_block",
                    "task_block",
                    {
                        "reason": "compile gate failed",
                        "options": ["fix compile error", "reduce scope", "handoff"],
                    },
                ),
                "tool_calls",
            )
        return response(
            call("call_commit", "workspace_commit", {"message": "Implement solution"}),
            "tool_calls",
        )

    if step == 3:
        return response(call("call_submit", "workspace_submit", {}), "tool_calls")

    if step == 4:
        gate = parse_tool(tool_results[1]).get("output") or {}
        submit = parse_tool(tool_results[3]).get("output") or {}
        evidence_id = gate.get("evidence_id")
        candidate_ref = submit.get("candidate_ref")
        base_commit = submit.get("base_commit")
        if not evidence_id or not candidate_ref or not base_commit:
            return response(
                call(
                    "call_block",
                    "task_block",
                    {
                        "reason": "missing gate evidence or candidate ref",
                        "options": ["retry commit/submit", "reduce scope", "handoff"],
                    },
                ),
                "tool_calls",
            )
        return response(
            call(
                "call_receipt",
                "receipt_submit",
                {
                    "receipt_id": f"R-{int(time.time() * 1000) % 10**9:09d}",
                    "gates": [
                        {
                            "gate": "compile",
                            "exit": 0,
                            "evidence_id": evidence_id,
                        }
                    ],
                    "candidate_ref": candidate_ref,
                    "base_commit": base_commit,
                    "pending_validation": [],
                    "deviations": [],
                },
            ),
            "tool_calls",
        )

    return response(
        {
            "role": "assistant",
            "content": "Tool loop complete; receipt submitted.",
        }
    )
