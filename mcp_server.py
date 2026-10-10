"""VendorCorp's MCP server: the data plane, and the place where the negotiated scope is enforced.

Run it first:  .venv/bin/python mcp_server.py        # streamable HTTP on the configured port

Two tools, both scope-checked against the contract VendorCorp published over A2A
(`core/contract.py`), re-read from disk on every call:

    query_records(filters, group_by, aggregate) -> aggregates only, never rows
    write_audit_log(entry)                      -> appends to data/audit_log.jsonl

A refusal is raised as an MCP tool error (`isError`), so the reason text originates here —
in the server that holds the data — and reaches BuyerCorp as a tool-call failure, not as a
UI-side judgement.
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import uvicorn
from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError

from core import config as cfg_mod
from core.contract import evaluate
from core.dataset import AGGREGATES, DATA_PATH, columns, run_query
from core.events import emit

AUDIT_PATH = Path(__file__).resolve().parent / "data" / "audit_log.jsonl"

_INSTRUCTIONS = (
    "VendorCorp data plane. Aggregates only. Every call is checked against the contract the two "
    "agents agreed over A2A; a call that touches a column outside that contract is refused here, "
    "in the server, and no row is read."
)

mcp = FastMCP(
    "vendorcorp-data",
    instructions=_INSTRUCTIONS,
    host="127.0.0.1",
    port=8123,
    streamable_http_path="/mcp",
    stateless_http=True,
    json_response=True,
)


def _referenced_columns(filters: Any, group_by: Any, aggregate: Any) -> set[str]:
    referenced: set[str] = set()
    if isinstance(filters, dict):
        referenced |= {str(key) for key in filters}
    if isinstance(group_by, str) and group_by.strip():
        referenced.add(group_by.strip())
    if isinstance(aggregate, dict) and isinstance(aggregate.get("column"), str):
        column = aggregate["column"].strip()
        if column and column != "*":
            referenced.add(column)
    return referenced


def _record_call(tool: str, arguments: dict[str, Any], *, decision: str, reason: str = "", result: Any = None,
                 contract: dict[str, Any] | None = None, ms: int = 0) -> None:
    emit(
        "mcp",
        "vendorcorp-mcp",
        decision,
        tool=tool,
        arguments=arguments,
        reason=reason or None,
        contract_id=(contract or {}).get("contract_id"),
        granted=sorted((contract or {}).get("scope", {}).get("columns", [])) or None,
        result=result if decision == "result" else None,
        error=reason or None,
        elapsed_ms=ms,
    )


@mcp.tool(
    name="query_records",
    description=(
        "Aggregate VendorCorp's order dataset. `filters` maps a column to an exact value, a list "
        "or {\"in\": [...]}, or {\"gte\": ..., \"lte\": ...} (ISO dates compare correctly as strings). "
        "`group_by` is a column name or null for a single total. `aggregate` is "
        "{\"function\": \"mean|sum|count|min|max\", \"column\": \"<name or *>\"}. "
        "Returns group-level results only — never row-level data. Refused unless every column "
        "referenced by the call is inside the contract VendorCorp granted over A2A."
    ),
)
async def query_records(filters: dict[str, Any], group_by: str | None, aggregate: dict[str, Any]) -> dict[str, Any]:
    """Scope-checked aggregation over `data/vendor_records.csv`."""
    started = time.monotonic()
    arguments = {"filters": filters, "group_by": group_by, "aggregate": aggregate}

    if not isinstance(aggregate, dict) or aggregate.get("function") not in AGGREGATES:
        reason = f"BAD_AGGREGATE: aggregate must be {{'function': one of {list(AGGREGATES)}, 'column': '<name|*>'}}."
        _record_call("query_records", arguments, decision="refusal", reason=reason, ms=_ms(started))
        raise ToolError(reason)

    dataset_columns = set(columns())
    referenced = _referenced_columns(filters, group_by, aggregate)
    allowed, reason, contract = evaluate(referenced, dataset_columns)
    if not allowed:
        _record_call("query_records", arguments, decision="refusal", reason=reason, contract=contract, ms=_ms(started))
        raise ToolError(reason)

    try:
        result = run_query(filters, group_by or None, aggregate)
    except FileNotFoundError as exc:
        reason = f"DATA_UNAVAILABLE: {exc}"
        _record_call("query_records", arguments, decision="refusal", reason=reason, contract=contract)
        raise ToolError(reason) from exc

    result["contract_id"] = contract["contract_id"] if contract else None
    result["columns_referenced"] = sorted(referenced)
    result["truncated"] = len(result["results"]) > 50
    result["results"] = result["results"][:50]
    _record_call("query_records", arguments, decision="result", result=result, contract=contract, ms=_ms(started))
    return result


@mcp.tool(
    name="write_audit_log",
    description=(
        "Append one access record to VendorCorp's audit log (data/audit_log.jsonl). "
        "`entry` should carry the buyer, purpose, contract id, columns used, the query issued and "
        "the row counts. Refused if it names a column outside the granted scope."
    ),
)
async def write_audit_log(entry: dict[str, Any]) -> dict[str, Any]:
    """VendorCorp's accountability record."""
    started = time.monotonic()
    if not isinstance(entry, dict) or not entry:
        reason = "BAD_ENTRY: write_audit_log needs a non-empty object."
        _record_call("write_audit_log", {"entry": entry}, decision="refusal", reason=reason)
        raise ToolError(reason)

    named = entry.get("columns") or entry.get("columns_used") or []
    referenced = {str(c) for c in named} if isinstance(named, (list, tuple, set)) else set()
    allowed, reason, contract = evaluate(referenced, set(columns()))
    if not allowed:
        _record_call("write_audit_log", {"entry": entry}, decision="refusal", reason=reason, contract=contract)
        raise ToolError(reason)

    record = {
        "logged_at": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
        "contract_id": contract["contract_id"] if contract else None,
        "entry": entry,
    }
    AUDIT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(AUDIT_PATH, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, default=str) + "\n")

    result = {"written": True, "path": str(AUDIT_PATH.relative_to(AUDIT_PATH.parent.parent)),
              "contract_id": record["contract_id"], "logged_at": record["logged_at"]}
    _record_call("write_audit_log", {"entry": entry}, decision="result", result=result, contract=contract,
                 ms=_ms(started))
    return result


def _ms(started: float) -> int:
    return int((time.monotonic() - started) * 1000)


def build_app():
    """The FastMCP app is rebuilt with the configured host/port before `streamable_http_app()`."""
    cfg = cfg_mod.load()
    host, port = cfg.mcp_host
    mcp.settings.host = host
    mcp.settings.port = port
    emit(
        "mcp",
        "vendorcorp-mcp",
        "starting",
        transport="streamable-http",
        url=f"http://{host}:{port}{mcp.settings.streamable_http_path}",
        dataset=str(DATA_PATH.name),
        columns=columns(),
        server_version="mcp " + _mcp_version(),
    )
    return mcp.streamable_http_app()


def _mcp_version() -> str:
    try:
        from importlib.metadata import version

        return version("mcp")
    except Exception:
        return "unknown"


if __name__ == "__main__":
    cfg = cfg_mod.load()
    host, port = cfg.mcp_host
    app = build_app()
    try:
        uvicorn.run(app, host=host, port=port, log_level="warning")
    finally:
        emit("mcp", "vendorcorp-mcp", "stopped", url=f"http://{host}:{port}/mcp")
