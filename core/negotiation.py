"""The negotiation message schema, its validation, and the prompt block both agents share.

Every A2A message in this demo is one JSON object:

    {"from": "buyer|vendor", "intent": "propose|counter|accept|reject|request",
     "scope": {"columns": [...], "purpose": "...", "retention_days": N},
     "rationale": "one sentence"}

`validate_message` is the gate. A message that fails it is rejected and the sender retries
once, then the run errors out — there is no "best effort" path that would let a malformed
message into the contract.
"""

from __future__ import annotations

from typing import Any

from .contract import MAX_RETENTION_DAYS

INTENTS = ("propose", "counter", "accept", "reject", "request")
SIDES = ("buyer", "vendor")
TOP_LEVEL = {"from", "intent", "scope", "rationale"}
SCOPE_KEYS = {"columns", "purpose", "retention_days"}

SCHEMA_BLOCK = f"""Reply with ONE JSON object and nothing else. Exact shape:

{{"from": "<{SIDES[0]}|{SIDES[1]}>",
 "intent": "<{'|'.join(INTENTS)}>",
 "scope": {{"columns": ["<column name>", ...],
            "purpose": "<one phrase describing why the data is needed>",
            "retention_days": <integer>}},
 "rationale": "<one sentence>"}}

Hard rules:
- No prose before or after the JSON, no markdown code fence.
- "columns" may only contain names from the dataset schema given above.
- "retention_days" is an integer number of days.
- Exactly these four top-level keys, and exactly those three keys inside "scope".
- "intent" meanings: propose = offer a scope; counter = offer a different scope;
  accept = agree to the other side's last scope; reject = refuse it;
  request = ask for an additional field or a change after a contract exists.
"""


class SchemaError(ValueError):
    """The message is not a valid negotiation message."""


def validate_message(
    obj: Any,
    *,
    expect_from: str | None = None,
    known_columns: set[str] | None = None,
) -> dict[str, Any]:
    """Validate and normalise a negotiation message. Raises `SchemaError` on any violation."""
    if not isinstance(obj, dict):
        raise SchemaError(f"message must be a JSON object, got {type(obj).__name__}")

    extra = sorted(set(obj) - TOP_LEVEL)
    if extra:
        raise SchemaError(f"unexpected top-level key(s) {extra}; allowed: {sorted(TOP_LEVEL)}")
    missing = sorted(TOP_LEVEL - set(obj))
    if missing:
        raise SchemaError(f"missing top-level key(s) {missing}")

    side = obj["from"]
    if side not in SIDES:
        raise SchemaError(f'"from" must be one of {list(SIDES)}, got {side!r}')
    if expect_from and side != expect_from:
        raise SchemaError(f'expected "from": "{expect_from}", got "{side}" (a reply may not speak for the other party)')

    intent = obj["intent"]
    if intent not in INTENTS:
        raise SchemaError(f'"intent" must be one of {list(INTENTS)}, got {intent!r}')

    scope = obj["scope"]
    if not isinstance(scope, dict):
        raise SchemaError(f'"scope" must be an object, got {type(scope).__name__}')
    extra_scope = sorted(set(scope) - SCOPE_KEYS)
    if extra_scope:
        raise SchemaError(f"unexpected key(s) in scope: {extra_scope}")
    missing_scope = sorted(SCOPE_KEYS - set(scope))
    if missing_scope:
        raise SchemaError(f"missing key(s) in scope: {missing_scope}")

    columns = scope["columns"]
    if not isinstance(columns, list) or not all(isinstance(c, str) for c in columns):
        raise SchemaError('"scope.columns" must be a list of column-name strings')
    columns = [c.strip() for c in columns if c and c.strip()]
    seen: set[str] = set()
    columns = [c for c in columns if not (c in seen or seen.add(c))]
    if not columns and intent != "reject":
        raise SchemaError(f'"scope.columns" must not be empty for intent {intent!r}')
    if known_columns is not None:
        unknown = sorted(set(columns) - known_columns)
        if unknown:
            raise SchemaError(f"column(s) {unknown} are not in the dataset schema {sorted(known_columns)}")

    purpose = scope["purpose"]
    if not isinstance(purpose, str) or not purpose.strip():
        raise SchemaError('"scope.purpose" must be a non-empty string')

    retention = scope["retention_days"]
    if isinstance(retention, bool) or not isinstance(retention, int):
        raise SchemaError(f'"scope.retention_days" must be an integer, got {retention!r}')
    if retention < 1:
        raise SchemaError(f'"scope.retention_days" must be >= 1, got {retention}')

    rationale = obj["rationale"]
    if not isinstance(rationale, str) or not rationale.strip():
        raise SchemaError('"rationale" must be a non-empty sentence')

    return {
        "from": side,
        "intent": intent,
        "scope": {
            "columns": columns,
            "purpose": purpose.strip(),
            "retention_days": min(retention, MAX_RETENTION_DAYS),
        },
        "rationale": " ".join(rationale.split()),
    }


def schema_block_for(columns: set[str]) -> str:
    return (
        "Dataset schema the two parties are negotiating over (column names only; no rows are "
        f"exposed before a contract is agreed):\n{sorted(columns)}\n\n" + SCHEMA_BLOCK
    )
