"""Concurrency/throughput smoke against the internal API."""

from __future__ import annotations

import statistics
import threading
import time

from .common import request

WORKERS = ["worker-e", "worker-f", "worker-g", "worker-llm"]
TASKS = 6
READ_THREADS = 4
READ_SECONDS = 10


def read_load(stop: threading.Event, latencies: list[float], errors: list[int]) -> None:
    while not stop.is_set():
        started = time.perf_counter()
        status, _ = request("GET", "/v1/tasks")
        latencies.append((time.perf_counter() - started) * 1000)
        if status != 200:
            errors.append(status)
        time.sleep(0.02)


def main() -> None:
    stamp = int(time.time() * 1000) % 100000
    codes = [f"T-3{stamp}{i:02d}" for i in range(TASKS)]
    created = []
    for code in codes:
        status, body = request(
            "POST",
            "/v1/tasks",
            {
                "code": code,
                "title": f"load smoke {code}",
                "workstream": "stream-core",
                "priority": "P2",
                "budget": {"max_tool_calls": 20, "max_model_calls": 12, "max_cost": 2},
            },
        )
        if status == 201:
            created.append(code)
        else:
            print("create failed", code, status, body)
    print(f"created={len(created)}/{TASKS}")

    stop = threading.Event()
    latencies: list[float] = []
    errors: list[int] = []
    threads = [
        threading.Thread(target=read_load, args=(stop, latencies, errors), daemon=True)
        for _ in range(READ_THREADS)
    ]
    for thread in threads:
        thread.start()

    read_started = time.time()
    dispatched = {}
    deadline = time.time() + 60
    while created and time.time() < deadline:
        for code in list(created):
            for worker in WORKERS:
                status, body = request(
                    "POST", f"/v1/tasks/{code}/dispatch", {"agent": worker}
                )
                if status == 201:
                    dispatched[code] = time.time()
                    created.remove(code)
                    break
                if status not in (409, 429):
                    break
        time.sleep(1)
    time.sleep(max(0, READ_SECONDS - (time.time() - read_started)))
    stop.set()
    print(f'dispatched={len(dispatched)}', flush=True)
    for thread in threads:
        thread.join(timeout=5)

    results = {}
    end = time.time() + 90
    while time.time() < end:
        pending = [code for code in dispatched if code not in results]
        if not pending:
            break
        for code in pending:
            status, detail = request("GET", f"/v1/tasks/{code}")
            if status == 200 and detail.get("state") in (
                "merged",
                "cancelled",
                "failed",
                "blocked",
            ):
                results[code] = detail["state"]
        time.sleep(2)

    values = sorted(latencies) or [0]
    p50 = statistics.median(values)
    p95 = values[min(len(values) - 1, int(len(values) * 0.95))]
    error_rate = len(errors) / max(1, len(latencies))
    merged = sum(1 for state in results.values() if state == "merged")
    print(
        f"reads={len(latencies)} rps={len(latencies)/READ_SECONDS:.1f} "
        f"p50={p50:.1f}ms p95={p95:.1f}ms errors={len(errors)} "
        f"error_rate={error_rate:.4f}"
    )
    print(
        f"dispatched={len(dispatched)} completed={len(results)} merged={merged} "
        f"states={sorted(set(results.values()))}"
    )
    status, audit = request("GET", "/v1/audit/chain?limit=2000")
    print("audit", status, audit)


if __name__ == "__main__":
    main()
