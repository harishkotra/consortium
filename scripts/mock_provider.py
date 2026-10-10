"""DEV TOOL — a scripted OpenAI-compatible endpoint for exercising the stack without an API key.

    .venv/bin/python scripts/mock_provider.py        # listens on 127.0.0.1:8199/v1

Point `.env` at it (`OPENAI_BASE_URL=http://127.0.0.1:8199/v1`, any key) and the whole pipeline
runs: A2A negotiation, contract grant, MCP execution, the event log, the observer.

This is a test harness, not a demo. The replies are scripted from the prompt's section headers,
so nothing here is model output and no run performed against it is evidence for the README. The
shipped numbers come from a real provider (see scripts/verify.py with `.env` pointed at
api.particle.ai).

What it scripts is the *shape* a well-behaved pair of agents produces:
  buyer turn 1     → propose the columns the question needs
  vendor turn      → counter with a narrower, non-personal scope
  buyer turn 2     → accept the vendor's columns verbatim
  query planner    → the aggregate the question asks for (email included when named)
"""

from __future__ import annotations

import json
import re
import time
import uuid
from typing import Any

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

PII = ("customer_email", "customer_name")
app = FastAPI(title="consortium mock provider")


def _last_user(messages: list[dict[str, Any]]) -> str:
    for message in reversed(messages):
        if message.get("role") == "user":
            content = message.get("content")
            return content if isinstance(content, str) else json.dumps(content)
    return ""


def _last_columns(prompt: str) -> list[str]:
    """The `columns` array of the most recent message in the transcript embedded in the prompt."""
    arrays = re.findall(r'"columns":\s*\[([^\]]*)\]', prompt, re.S)
    if not arrays:
        return []
    return [item.strip().strip('"') for item in arrays[-1].split(",") if item.strip()]


def reply(prompt: str) -> str:
    if "Open the negotiation" in prompt:
        question = _quoted(prompt, "Business question from your principal") or ""
        # Deliberately over-ask by one column (order_id) so the vendor has something to narrow.
        columns = ["region", "order_date", "order_value", "order_id"]
        if "email" in question.lower():
            columns = ["region", "order_date", "order_value", "customer_email"]
        return json.dumps({
            "from": "buyer", "intent": "propose",
            "scope": {"columns": columns,
                      "purpose": question[:120] or "aggregate order value by region for Q3 2025",
                      "retention_days": 30},
            "rationale": "These are the columns the question groups by, measures and filters on.",
        })

    if "Answer it as VendorCorp" in prompt:
        # Counter narrower: drop the pseudonymous key and any personal data, keep what answers it.
        asked = _last_columns(prompt)
        keep = [c for c in asked if c not in PII and c != "order_id"] or ["region", "order_value"]
        return json.dumps({
            "from": "vendor", "intent": "counter",
            "scope": {"columns": sorted(keep),
                      "purpose": "aggregate order value by region for Q3 2025",
                      "retention_days": 30},
            "rationale": "Aggregates need only the grouping, measured and filtered columns, so "
                         "the pseudonymous key and any personal data come out of the grant.",
        })

    if "Send your next move as the buyer" in prompt:
        return json.dumps({
            "from": "buyer", "intent": "accept",
            "scope": {"columns": _last_columns(prompt),
                      "purpose": "aggregate order value by region for Q3 2025",
                      "retention_days": 30},
            "rationale": "The countered scope still answers the question, so we accept it.",
        })

    if "Emit the query that answers the question" in prompt:
        question = _quoted(prompt, "Business question") or ""
        group = "customer_email" if "email" in question.lower() else "region"
        return json.dumps({
            "filters": {"order_date": {"gte": "2025-07-01", "lte": "2025-09-30"}},
            "group_by": group,
            "aggregate": {"function": "mean", "column": "order_value"},
        })

    if "answering a peer organisation" in prompt:
        return "BuyerCorp requested an aggregate by region; VendorCorp granted a non-personal scope."

    return json.dumps({"from": "buyer", "intent": "reject",
                       "scope": {"columns": [], "purpose": "unknown", "retention_days": 1},
                       "rationale": "Unrecognised prompt in the mock provider."})


def _quoted(prompt: str, marker: str) -> str | None:
    index = prompt.find(marker)
    if index == -1:
        return None
    match = re.search(r":\s*'([^']*)'", prompt[index:]) or re.search(r":\s*\"([^\"]*)\"", prompt[index:])
    return match.group(1) if match else None



@app.post("/v1/chat/completions")
async def completions(request: Request) -> JSONResponse:
    body = await request.json()
    prompt = _last_user(body.get("messages", []))
    content = reply(prompt)
    return JSONResponse(
        {
            "id": f"chatcmpl-{uuid.uuid4().hex[:12]}",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": body.get("model", "mock"),
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": content},
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": len(prompt) // 4, "completion_tokens": len(content) // 4,
                      "total_tokens": (len(prompt) + len(content)) // 4},
        }
    )


@app.get("/v1/models")
async def models() -> JSONResponse:
    return JSONResponse({"object": "list", "data": [{"id": "mock", "object": "model",
                                                     "owned_by": "consortium"}]})


if __name__ == "__main__":
    print("[mock] scripted provider on http://127.0.0.1:8199/v1 — DEV ONLY, not demo evidence")
    uvicorn.run(app, host="127.0.0.1", port=8199, log_level="warning")
