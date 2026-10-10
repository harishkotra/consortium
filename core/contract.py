"""The negotiated contract: written by VendorCorp, enforced by the MCP server.

This file is the hand-off point between the control plane (A2A negotiation) and the data
plane (MCP tool calls). The vendor agent is the only writer; the MCP server re-reads it on
every call, so a scope change takes effect on the next request rather than needing a restart.
"""

from __future__ import annotations

import json
import secrets
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
CONTRACT_PATH = ROOT / "data" / "contract.json"

_cache: dict[str, Any] = {"mtime": None, "size": None, "contract": None}
_lock = threading.Lock()

#: VendorCorp's stewardship policy. The vendor agent is instructed with these numbers and the
#: MCP server refuses anything outside them, so the limit holds even if the model over-grants.
MAX_RETENTION_DAYS = 90
DEFAULT_RETENTION_DAYS = 30


def publish(
    *,
    columns: list[str],
    purpose: str,
    retention_days: int,
    buyer: str = "BuyerCorp",
    context_id: str | None = None,
) -> dict[str, Any]:
    contract_id = f"c-{secrets.token_hex(3)}"
    granted_at = datetime.now(timezone.utc)
    expires_at = granted_at + timedelta(days=retention_days)
    contract = {
        "contract_id": contract_id,
        "agreed": True,
        "buyer": buyer,
        "vendor": "VendorCorp",
        "scope": {
            "columns": sorted(set(columns)),
            "purpose": purpose,
            "retention_days": int(retention_days),
        },
        "granted_at": granted_at.isoformat(timespec="seconds"),
        "expires_at": expires_at.isoformat(timespec="seconds"),
        "negotiated_via": {"protocol": "A2A", "context_id": context_id},
    }
    CONTRACT_PATH.parent.mkdir(parents=True, exist_ok=True)
    CONTRACT_PATH.write_text(json.dumps(contract, indent=2) + "\n", encoding="utf-8")
    with _lock:
        _cache["mtime"] = None  # force a re-read on the next call
    return contract


def current() -> dict[str, Any] | None:
    """The live contract, or None. Cached on file mtime+size so a mid-run grant is picked up."""
    if not CONTRACT_PATH.exists():
        return None
    stat = CONTRACT_PATH.stat()
    with _lock:
        if _cache["mtime"] == stat.st_mtime and _cache["size"] == stat.st_size:
            return _cache["contract"]
        try:
            contract = json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            contract = None
        _cache.update(mtime=stat.st_mtime, size=stat.st_size, contract=contract)
        return contract


def clear() -> None:
    if CONTRACT_PATH.exists():
        CONTRACT_PATH.unlink()
    with _lock:
        _cache.update(mtime=None, size=None, contract=None)


def _expired(contract: dict[str, Any], now: datetime) -> bool:
    try:
        return datetime.fromisoformat(contract["expires_at"]) < now
    except Exception:
        return False


def evaluate(
    referenced: set[str],
    dataset_columns: set[str],
    *,
    now: datetime | None = None,
) -> tuple[bool, str, dict[str, Any] | None]:
    """Decide whether a tool call may touch `referenced` columns.

    Returns (allowed, reason, contract). Every reason is a specific, machine-readable sentence
    prefixed with a code, because the refusal has to come from the data plane, not the UI.
    """
    now = now or datetime.now(timezone.utc)
    contract = current()

    unknown = sorted(referenced - dataset_columns)
    if unknown:
        return False, (
            f"UNKNOWN_COLUMN: {unknown} is not a column of the VendorCorp dataset. "
            f"Available: {sorted(dataset_columns)}"
        ), contract

    if contract is None:
        return False, (
            "NO_CONTRACT: no negotiated contract has been published by VendorCorp, so no column "
            "is granted. Complete the A2A negotiation to an 'accept' before calling query_records."
        ), contract

    if _expired(contract, now):
        return False, (
            f"CONTRACT_EXPIRED: {contract['contract_id']} expired at {contract['expires_at']} "
            f"(retention {contract['scope']['retention_days']} days). Renegotiation required."
        ), contract

    granted = set(contract["scope"]["columns"])
    denied = sorted(referenced - granted)
    if denied:
        return False, (
            f"SCOPE_DENIED: {denied} was not granted by contract {contract['contract_id']}. "
            f"Granted columns: {sorted(granted)}. Purpose of record: "
            f"{contract['scope']['purpose']!r}. This call is refused at the MCP server and was "
            f"not read from the dataset."
        ), contract

    return True, "", contract
