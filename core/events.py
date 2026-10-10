"""Append-only JSONL event log: the single source of truth the observer renders.

Every process (MCP server, vendor agent, buyer agent, observer) writes here. Because they are
separate OS processes, appends take an exclusive `fcntl` lock and derive their sequence number
from the file itself, so the timeline has one global ordering rather than four per-process ones.
"""

from __future__ import annotations

import fcntl
import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
LOG_PATH = ROOT / "data" / "events.jsonl"
RUN_PATH = ROOT / "data" / "current_run.json"

_TAIL_BYTES = 65_536
_lock = threading.Lock()

#: Planes the observer colours by. a2a = negotiation protocol, mcp = data-plane tool calls,
#: contract = scope grants/checks, run = lifecycle + state, model = raw LLM calls.
PLANES = ("a2a", "mcp", "contract", "run", "model")

#: Process start/stop events, which belong to no negotiation run.
_LIFECYCLE_KINDS = {"listening", "starting", "stopped", "unreachable"}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _next_seq(handle: Any) -> int:
    """Read the tail and take the last `seq`. Locked, so two writers cannot collide."""
    size = os.fstat(handle.fileno()).st_size
    if size == 0:
        return 1
    handle.seek(max(0, size - _TAIL_BYTES))
    chunk = handle.read()
    for line in reversed(chunk.splitlines()):
        try:
            return int(json.loads(line)["seq"]) + 1
        except Exception:
            continue
    return 1


def emit(plane: str, actor: str, kind: str, **fields: Any) -> dict[str, Any]:
    """Append one event and return it. Never raises: a broken log must not break the demo."""
    # Service lifecycle events are not part of any run; tagging them with whatever run id
    # happens to be on disk makes the timeline look like old runs are still going.
    run_id = None if kind in _LIFECYCLE_KINDS else active_run().get("run_id")
    event: dict[str, Any] = {
        "ts": _now(),
        "plane": plane,
        "actor": actor,
        "kind": kind,
        "run_id": run_id,
        **{k: v for k, v in fields.items() if v is not None},
    }
    try:
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with _lock, open(LOG_PATH, "a+", encoding="utf-8") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                event["seq"] = _next_seq(handle)
                handle.write(json.dumps(event, ensure_ascii=False, separators=(",", ":"), default=str) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    except Exception as exc:  # pragma: no cover - logging must never be the failure mode
        event["log_error"] = f"{type(exc).__name__}: {exc}"
    return event


def read_all() -> list[dict[str, Any]]:
    if not LOG_PATH.exists():
        return []
    events: list[dict[str, Any]] = []
    with open(LOG_PATH, encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_SH)
        try:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    events.append(json.loads(line))
                except json.JSONDecodeError:
                    events.append({"kind": "corrupt", "plane": "run", "actor": "log", "text": line[:400]})
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    events.sort(key=lambda e: e.get("seq", 0))
    return events


def for_run(run_id: str) -> list[dict[str, Any]]:
    return [event for event in read_all() if event.get("run_id") == run_id]


def set_active_run(run_id: str, **meta: Any) -> None:
    RUN_PATH.parent.mkdir(parents=True, exist_ok=True)
    RUN_PATH.write_text(json.dumps({"run_id": run_id, **meta}, default=str), encoding="utf-8")


def active_run() -> dict[str, Any]:
    try:
        return json.loads(RUN_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}
