"""Dataset access for VendorCorp: schema + the aggregation the MCP tool runs.

Only VendorCorp-side code imports this (the MCP server for queries, the vendor agent to publish
its schema in the agent card). BuyerCorp never reads `data/vendor_records.csv`; its only route to
the data is the MCP tool call, which is where the scope check happens.
"""

from __future__ import annotations

import csv
import statistics
from functools import lru_cache
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
DATA_PATH = ROOT / "data" / "vendor_records.csv"

AGGREGATES = ("mean", "sum", "count", "min", "max")


def _coerce(value: str) -> Any:
    """Column types come from the data itself, not a schema declaration."""
    if value == "":
        return None
    try:
        return int(value)
    except ValueError:
        pass
    try:
        return float(value)
    except ValueError:
        pass
    return value


@lru_cache(maxsize=4)
def _read(path_str: str, mtime: float, size: int) -> list[dict[str, Any]]:
    """Cached on (path, mtime, size) so regenerating the CSV is picked up without a restart."""
    path = Path(path_str)
    if not path.exists():
        raise FileNotFoundError(
            f"{path} is missing. Generate it once with: .venv/bin/python scripts/make_data.py"
        )
    with open(path, newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        fields = list(reader.fieldnames or [])
        return [{field: _coerce(record[field]) for field in fields} for record in reader]


def _rows() -> list[dict[str, Any]]:
    stat = DATA_PATH.stat()
    return _read(str(DATA_PATH), stat.st_mtime, stat.st_size)


def columns() -> list[str]:
    with open(DATA_PATH, newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle).fieldnames or [])


def _match(row: Any, spec: Any) -> bool:
    """Exact match, or {"in": [...]}, or {"gte": a, "lte": b} / {"between": [a, b]}."""
    if isinstance(spec, dict) or isinstance(spec, list):
        if isinstance(spec, dict) and "in" in spec:
            return row in {_coerce_value(v, row) for v in spec["in"]}
        bounds = spec if isinstance(spec, dict) else {"between": spec}
        if "between" in bounds:
            low, high = bounds["between"]
            return _cmp(row, _coerce_value(low, row)) >= 0 and _cmp(row, _coerce_value(high, row)) <= 0
        ok = True
        if "gte" in bounds:
            ok = ok and _cmp(row, _coerce_value(bounds["gte"], row)) >= 0
        if "gt" in bounds:
            ok = ok and _cmp(row, _coerce_value(bounds["gt"], row)) > 0
        if "lte" in bounds:
            ok = ok and _cmp(row, _coerce_value(bounds["lte"], row)) <= 0
        if "lt" in bounds:
            ok = ok and _cmp(row, _coerce_value(bounds["lt"], row)) < 0
        if "ne" in bounds:
            ok = ok and _cmp(row, _coerce_value(bounds["ne"], row)) != 0
        return ok
    return row == _coerce_value(spec, row)


def _coerce_value(value: Any, like: Any) -> Any:
    """Let "2025-07-01" filter a date string and "100" filter a number."""
    if isinstance(like, (int, float)) and isinstance(value, str):
        return _coerce(value)
    return value


def _cmp(a: Any, b: Any) -> int:
    try:
        return -1 if a < b else (1 if a > b else 0)
    except TypeError:
        a, b = str(a), str(b)
        return -1 if a < b else (1 if a > b else 0)


def filter_rows(rows: list[dict[str, Any]], filters: dict[str, Any] | None) -> list[dict[str, Any]]:
    if not filters:
        return list(rows)
    out = []
    for row in rows:
        if all(row.get(field, None) is not None and _match(row.get(field), spec) for field, spec in filters.items()):
            out.append(row)
    return out


def aggregate_rows(
    rows: list[dict[str, Any]],
    group_by: str | None,
    function: str,
    column: str,
) -> list[dict[str, Any]]:
    if function not in AGGREGATES:
        raise ValueError(f"unknown aggregate {function!r}; choose from {list(AGGREGATES)}")
    groups: dict[Any, list[dict[str, Any]]] = {}
    for row in rows:
        groups.setdefault(row.get(group_by) if group_by else "__all__", []).append(row)

    results: list[dict[str, Any]] = []
    for key, members in groups.items():
        values = [m.get(column) for m in members if isinstance(m.get(column), (int, float))]
        if function == "count":
            value: Any = len(members) if column == "*" else len(values)
        elif not values:
            value = None
        elif function == "mean":
            value = round(statistics.fmean(values), 4)
        elif function == "sum":
            value = round(sum(values), 4)
        elif function == "min":
            value = min(values)
        else:
            value = max(values)
        entry: dict[str, Any] = {"group": None if key == "__all__" else key, "n_rows": len(members)}
        if group_by:
            entry[group_by] = key
        entry[f"{function}({column})"] = value
        results.append(entry)

    results.sort(key=lambda e: (e["group"] is None, str(e["group"])))
    return results


def run_query(
    filters: dict[str, Any] | None,
    group_by: str | None,
    agg: dict[str, Any],
) -> dict[str, Any]:
    """Execute an agreed query. Scope enforcement happens in the caller (the MCP tool)."""
    rows = _rows()
    matched = filter_rows(rows, filters)
    function, column = agg["function"], agg["column"]
    return {
        "engine": "VendorCorp query_records",
        "source": DATA_PATH.name,
        "rows_in_dataset": len(rows),
        "rows_matched": len(matched),
        "filters": filters or {},
        "group_by": group_by,
        "aggregate": f"{function}({column})",
        "results": aggregate_rows(matched, group_by, function, column),
    }
