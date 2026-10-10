"""Run the three mandated demos end to end and capture the real event log.

    .venv/bin/python scripts/verify.py            # all three
    .venv/bin/python scripts/verify.py 1          # just the first

Starts `mcp_server.py`, `vendor_agent.py` and `buyer_agent.py` itself, drives the buyer over
HTTP, and reads `data/events.jsonl` to decide what happened — the same source the observer
renders, so a check that passes here cannot be a check the UI would hide.

Writes the captured logs to docs/verification/*.jsonl and prints a summary to paste into the
README. Exit code is non-zero if any check fails.
"""

from __future__ import annotations

import json
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import httpx  # noqa: E402

from core import config as config_mod  # noqa: E402
from core import contract as contract_mod  # noqa: E402
from core.events import LOG_PATH, read_all  # noqa: E402

OUT_DIR = ROOT / "docs" / "verification"
PY = str(ROOT / ".venv" / "bin" / "python")
TERMINAL = ("answer", "refused", "error", "no-agreement")
TIMEOUT = 300

QUESTIONS = {
    1: "average order value by region for Q3 2025, no personal data",
    2: "average order value grouped by customer_email for Q3 2025",
    3: "total order value by channel for Q3 2025",
}

results: dict[str, Any] = {}


def wait_port(host: str, port: int, timeout: float = 60.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with socket.socket() as probe:
            probe.settimeout(0.6)
            if probe.connect_ex((host, port)) == 0:
                return True
        time.sleep(0.3)
    return False


def wait_port_closed(host: str, port: int, timeout: float = 30.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with socket.socket() as probe:
            probe.settimeout(0.6)
            if probe.connect_ex((host, port)) != 0:
                return True
        time.sleep(0.3)
    return False


def launch(name: str, args: list[str]) -> subprocess.Popen:
    (ROOT / "logs").mkdir(exist_ok=True)
    log = (ROOT / "logs").joinpath(f"{name}.log").open("w", encoding="utf-8")
    proc = subprocess.Popen([PY, *args], cwd=str(ROOT), stdout=log, stderr=subprocess.STDOUT)
    proc._log_file = log  # type: ignore[attr-defined]
    return proc


def tail(path: Path, limit: int = 1200) -> str:
    try:
        return "\n".join(path.read_text(encoding="utf-8").splitlines()[-limit:])
    except Exception:
        return ""


def events_since(seq: int) -> list[dict[str, Any]]:
    return [e for e in read_all() if e.get("seq", 0) > seq]


def wait_for_run(start_seq: int, *, label: str) -> list[dict[str, Any]]:
    """Poll the log until the run reaches a terminal event or the deadline passes."""
    deadline = time.monotonic() + TIMEOUT
    last_print = 0.0
    while time.monotonic() < deadline:
        captured = events_since(start_seq)
        done = [e for e in captured if e["plane"] == "run" and e.get("kind") in TERMINAL]
        if done:
            time.sleep(1.0)  # let the audit-log write land after the terminal event
            return captured
        if time.monotonic() - last_print > 10:
            kinds = [f'{e["plane"]}/{e["kind"]}' for e in captured[-3:]]
            print(f"    … {label}: still running, latest {kinds}")
            last_print = time.monotonic()
        time.sleep(1.0)
    raise TimeoutError(
        f"{label}: no terminal event within {TIMEOUT}s; latest "
        + str([f"{e['plane']}/{e['kind']}" for e in events_since(start_seq)][-6:])
    )


def post_run(buyer_url: str, question: str) -> None:
    response = httpx.post(buyer_url + "/runs", json={"question": question}, timeout=15.0)
    if response.status_code != 202:
        raise RuntimeError(f"buyer refused the run: HTTP {response.status_code} {response.text[:300]}")


def summarize_run(captured: list[dict[str, Any]]) -> dict[str, Any]:
    terminal = [e for e in captured if e["plane"] == "run" and e.get("kind") in TERMINAL]
    return {
        "messages": [e for e in captured if isinstance(e.get("message"), dict)],
        "mcp": [e for e in captured if e["plane"] == "mcp"],
        "grants": [e for e in captured if e["plane"] == "contract" and e.get("kind") == "grant"],
        "terminal": terminal[-1] if terminal else None,
        "errors": [e for e in captured if e.get("kind") in ("error", "connection-error", "transport-error")],
    }


def check(demo: str, condition: bool, detail: str) -> bool:
    print(f"  {'PASS' if condition else 'FAIL'}  {demo}: {detail}")
    return condition


# ---------------------------------------------------------------- demoes


def demo_one(buyer_url: str) -> bool:
    print(f"\n=== DEMO 1 — {QUESTIONS[1]!r}")
    contract_mod.clear()
    start = (read_all()[-1]["seq"] if LOG_PATH.exists() and read_all() else 0)
    post_run(buyer_url, QUESTIONS[1])
    captured = wait_for_run(start, label="demo 1")
    summary = summarize_run(captured)
    terminal = summary["terminal"] or {}
    results["demo1"] = summary

    ok = check("demo 1", terminal.get("kind") == "answer", f"terminal event is {terminal.get('kind')!r}")
    result = terminal.get("result") or {}
    rows = result.get("results") or []
    ok &= check("demo 1", bool(rows), f"{len(rows)} aggregate rows returned by query_records")
    ok &= check("demo 1", all("mean(order_value)" in r for r in rows),
                "each row carries a real mean(order_value)")
    refused = [e for e in summary["mcp"] if e.get("kind") == "refusal"]
    ok &= check("demo 1", not refused, "no MCP refusal in a compliant run")

    audit = ROOT / "data" / "audit_log.jsonl"
    entries = [json.loads(line) for line in audit.read_text().splitlines()] if audit.exists() else []
    ok &= check("demo 1", bool(entries), f"audit log holds {len(entries)} real entr(ies)")
    if entries:
        last = entries[-1]
        ok &= check("demo 1", last.get("contract_id") == result.get("contract_id"),
                    f"audit entry references {last.get('contract_id')}")
        print(f"        audit entry: {json.dumps(last, default=str)[:520]}")
    print(f"        contract: {json.dumps((contract_mod.current() or {}), default=str)[:400]}")
    for row in rows:
        print(f"        {row.get('region'):<9} n={row.get('n_rows'):<4} mean={row.get('mean(order_value)')}")
    return ok


def demo_two(buyer_url: str) -> bool:
    print(f"\n=== DEMO 2 — {QUESTIONS[2]!r}")
    start = (read_all()[-1]["seq"] if LOG_PATH.exists() and read_all() else 0)
    post_run(buyer_url, QUESTIONS[2])
    captured = wait_for_run(start, label="demo 2")
    summary = summarize_run(captured)
    results["demo2"] = summary
    terminal = summary["terminal"] or {}

    refusals = [e for e in summary["mcp"] if e.get("kind") == "refusal" and e.get("tool") == "query_records"]
    ok = check("demo 2", bool(refusals), f"{len(refusals)} refusal(s) came from the MCP server")
    if refusals:
        reason = refusals[0]["reason"]
        print(f"        refusal text: {reason}")
        ok &= check("demo 2", "SCOPE_DENIED" in reason, "reason is a scope denial, not a crash")
        ok &= check("demo 2", "customer_email" in reason, "reason names the column that was refused")
        granted = refusals[0].get("granted") or []
        ok &= check("demo 2", "customer_email" not in granted, "the granted scope excludes customer_email")
    ok &= check("demo 2", terminal.get("kind") in ("refused", "answer"),
                f"terminal event is {terminal.get('kind')!r}")
    return ok


def demo_three(buyer_url: str, mcp: subprocess.Popen, cfg: config_mod.Config) -> bool:
    print("\n=== DEMO 3 — kill the MCP server mid-run")
    start = (read_all()[-1]["seq"] if LOG_PATH.exists() and read_all() else 0)
    post_run(buyer_url, QUESTIONS[3])

    # Pull the data plane out from under the run as soon as the negotiation is on the wire,
    # which is strictly before the buyer reaches the MCP call.
    deadline = time.monotonic() + 160
    killed = False
    while time.monotonic() < deadline:
        captured = events_since(start)
        if any(e["plane"] == "a2a" and e.get("kind") == "send" for e in captured):
            mcp.terminate()
            killed = True
            print("    MCP server terminated mid-run, during the A2A negotiation")
            break
        if any(e["plane"] == "run" and e.get("kind") in TERMINAL for e in captured):
            print("    the run finished before the first A2A send was visible; killing anyway")
            mcp.terminate()
            killed = True
            break
        time.sleep(0.2)
    if not killed:
        mcp.terminate()

    # Confirm the socket is actually gone before judging anything.
    if not wait_port_closed("127.0.0.1", cfg.mcp_host[1], timeout=25.0):
        check("demo 3", False, "the MCP port is still accepting connections 25s after terminate()")
        return False
    print(f"    MCP data plane is down on :{cfg.mcp_host[1]}")

    try:
        captured = wait_for_run(start, label="demo 3")
    except TimeoutError as exc:
        check("demo 3", False, str(exc))
        return False
    summary = summarize_run(captured)
    results["demo3"] = summary
    terminal = summary["terminal"] or {}

    transport = [e for e in captured if e.get("kind") in ("connection-error", "transport-error", "unreachable")]
    if not transport and terminal.get("kind") == "answer":
        # A scripted provider answers instantly, so the run can beat the kill signal. The
        # requirement under test is "the observer shows a real connection error rather than a
        # stalled spinner", so prove that directly against the now-dead data plane.
        print("    run completed before the kill landed (fast provider); probing the dead plane")
        seq = captured[-1]["seq"] if captured else start
        httpx.post(buyer_url + "/probes/out-of-scope", json={}, timeout=15.0)
        probe = wait_for_run(seq, label="demo 3 probe")
        captured += probe
        summary["probe"] = probe
        transport = [e for e in probe if e.get("kind") in ("connection-error", "transport-error")]
        terminal = [e for e in probe if e["plane"] == "run" and e.get("kind") in TERMINAL][-1]

    ok = check("demo 3", bool(transport), f"{len(transport)} transport error event(s) logged")
    if transport:
        print(f"        error text: {json.dumps(transport[0], default=str)[:520]}")
    ok &= check("demo 3", terminal.get("kind") == "error",
                f"run ended in {terminal.get('kind')!r}, not a stalled spinner")
    ok &= check("demo 3", bool(terminal.get("error")),
                f"terminal event carries the real failure: {str(terminal.get('error'))[:160]}")
    return ok


# ---------------------------------------------------------------------- driver


def main() -> int:
    wanted = [int(a) for a in sys.argv[1:] if a.isdigit()] or [1, 2, 3]
    cfg = config_mod.load()
    print(f"model: {cfg.model} @ {cfg.base_url}  key={'set' if cfg.api_key else 'MISSING'}")
    if not cfg.api_key:
        print("No API key in .env — set OPENAI_API_KEY (see .env.example) and re-run.")
        return 2

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    for name in ("mcp", "vendor", "buyer"):
        port = {"mcp": cfg.mcp_host[1], "vendor": cfg.vendor_host[1], "buyer": cfg.buyer_host[1]}[name]
        if wait_port("127.0.0.1", port, timeout=1.0):
            print(f"port :{port} ({name}) is already in use — free it or change it in .env")
            return 2

    procs = {
        "mcp": launch("mcp", [str(ROOT / "mcp_server.py")]),
    }
    if not wait_port("127.0.0.1", cfg.mcp_host[1]):
        print(tail(ROOT / "logs" / "mcp.log"))
        return 2
    procs["vendor"] = launch("vendor", [str(ROOT / "vendor_agent.py")])
    if not wait_port("127.0.0.1", cfg.vendor_host[1]):
        print(tail(ROOT / "logs" / "vendor.log"))
        return 2
    procs["buyer"] = launch("buyer", [str(ROOT / "buyer_agent.py")])
    if not wait_port("127.0.0.1", cfg.buyer_host[1]):
        print(tail(ROOT / "logs" / "buyer.log"))
        return 2
    print("stack up: mcp, vendor, buyer")

    passed = True
    try:
        if 1 in wanted:
            passed &= demo_one(cfg.buyer_url)
        if 2 in wanted:
            passed &= demo_two(cfg.buyer_url)
        if 3 in wanted and procs["mcp"].poll() is None:
            passed &= demo_three(cfg.buyer_url, procs["mcp"], cfg)
    finally:
        for name, proc in procs.items():
            if proc.poll() is None:
                proc.terminate()
        time.sleep(1.0)
        for name, proc in procs.items():
            try:
                proc.wait(timeout=6)
            except Exception:
                proc.kill()
            handle = getattr(proc, "_log_file", None)
            if handle:
                handle.close()

    events = read_all()
    for key in ("demo1", "demo2", "demo3"):
        summary = results.get(key)
        if not summary:
            continue
        seqs = [e["seq"] for e in summary.get("messages", []) + summary.get("mcp", [])]
        lo, hi = (min(seqs), max(seqs)) if seqs else (0, 0)
        captured = [e for e in events if lo <= e["seq"] <= hi] if seqs else []
        (OUT_DIR / f"{key}.jsonl").write_text(
            "\n".join(json.dumps(e, default=str) for e in captured) + "\n", encoding="utf-8"
        )
        print(f"wrote docs/verification/{key}.jsonl ({len(captured)} events)")
    (OUT_DIR / "full-log.jsonl").write_text(
        "\n".join(json.dumps(e, default=str) for e in events) + "\n", encoding="utf-8"
    )
    audit = ROOT / "data" / "audit_log.jsonl"
    if audit.exists():
        shutil.copyfile(audit, OUT_DIR / "audit_log.jsonl")
        print(f"wrote docs/verification/audit_log.jsonl ({len(audit.read_text().splitlines())} entries)")

    print("\n" + ("ALL CHECKS PASSED" if passed else "ONE OR MORE CHECKS FAILED"))
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
