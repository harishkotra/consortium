"""VendorCorp's agent, served as an A2A endpoint on :8101.

VendorCorp holds the dataset. BuyerCorp's agent negotiates with this process over A2A for the
right to ask for one aggregate statistic. Three things are deliberately true here:

1. Every negotiation turn is authored by the model, constrained to the negotiation JSON schema
   (`core/negotiation.py`), and rejected-and-retried if it will not parse.
2. When the buyer sends `accept`, the contract is published *without* consulting the model, from
   the scope VendorCorp itself last offered. A buyer cannot widen the grant by echoing a broader
   "accepted" scope, and personal-data columns are removed by code, not by prompting.
3. Granting a contract does not release data. It only configures the MCP server, which enforces
   the scope independently on every later call.

Run:  .venv/bin/python vendor_agent.py
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import uvicorn
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.routes import add_a2a_routes_to_fastapi, create_agent_card_routes, create_jsonrpc_routes
from a2a.server.tasks import InMemoryTaskStore
from a2a.types import AgentCapabilities, AgentCard, AgentInterface, AgentSkill
from agent_framework import AgentResponse, Message
from agent_framework.a2a import A2AExecutor
from fastapi import FastAPI
from fastapi.responses import JSONResponse

from core import config as config_mod
from core import contract as contract_mod
from core.dataset import columns
from core.events import emit
from core.model import ProtocolError, ask_json, get_agent
from core.negotiation import SchemaError, schema_block_for, validate_message

ORG = "VendorCorp"
PEER = "BuyerCorp"

#: Identity-bearing columns. Refusing these is a code-level policy, not a suggestion to the model.
PII_COLUMNS = {"customer_name", "customer_email"}

INSTRUCTIONS = f"""You are DataSteward-1, the outbound data-sharing agent for {ORG}.

{ORG} holds an order dataset. An agent from {PEER} wants one aggregate statistic computed from it.
You negotiate the data-sharing contract. You never compute the statistic and you never release
rows; you only decide which COLUMNS the other side may reference in an aggregate query.

Policy, in priority order. These are firm:
1. Never grant {sorted(PII_COLUMNS)}. They are personal data. If the buyer's scope contains one,
   counter with it removed and say so in `rationale`. If the purpose cannot be served without
   personal data, intent="reject".
2. Grant the smallest column set that can serve the stated purpose: the grouping column, the
   measured column, and any column the buyer filters on. Nothing extra.
3. Prefer intent="counter" with a narrower scope over intent="reject". A narrower scope that still
   answers the question is the good outcome.
4. `retention_days`: offer 30. Never exceed {contract_mod.MAX_RETENTION_DAYS}.
5. Use only column names from the schema given in the prompt. Never invent one.
6. `rationale` is one sentence on why this scope is the right size.

The usual shape is: buyer proposes, you counter narrower, they accept. Answer with one JSON
object and no other text."""


class VendorService:
    """VendorCorp's A2A-facing agent.

    Implements the framework's agent surface (`run` + `create_session`) so `A2AExecutor` can drive
    it, and does the protocol bookkeeping around the model call: inbound validation, the
    deterministic grant, and the event log both sides of the wire are observed from.
    """

    def __init__(self, cfg: config_mod.Config, tools: list[dict[str, Any]]) -> None:
        self.cfg = cfg
        self._tools = tools
        # context_id -> last scope VendorCorp itself offered. An `accept` is honoured against this,
        # never against whatever the buyer claims to accept.
        self._offers: dict[str, dict[str, Any]] = {}
        self._transcripts: dict[str, list[dict[str, Any]]] = {}

    @property
    def agent(self):
        """Resolved per turn, so a sidebar model change applies to the next reply."""
        self.cfg = config_mod.load()
        return get_agent(self.cfg, name=f"{ORG} DataSteward-1", instructions=INSTRUCTIONS)

    def create_session(self, *, session_id: str | None = None):
        return self.agent.create_session(session_id=session_id)

    async def run(
        self, messages: Any = None, *, stream: bool = False, session: Any = None, **kwargs: Any
    ) -> AgentResponse:
        if stream:
            raise NotImplementedError("VendorCorp answers negotiation turns without streaming.")

        context_id = getattr(session, "session_id", None) or "anonymous"
        text = _inbound_text(messages)
        known = set(columns())

        try:
            inbound = self._parse(text, known)
        except SchemaError as exc:
            emit(
                "a2a", ORG.lower(), "protocol-error",
                context_id=context_id, error=str(exc), received=text[:400],
                note="message rejected; a sender that cannot produce valid JSON is required to "
                     "retry once before sending",
            )
            raise ValueError(f"VendorCorp rejected this message: {exc}") from exc

        emit(
            "a2a", ORG.lower(), "inbound",
            from_org=PEER, to_org=ORG, context_id=context_id,
            intent=inbound["intent"], message=inbound,
            note="received at the VendorCorp A2A endpoint",
        )

        if inbound["intent"] == "accept":
            reply = self._grant(inbound, context_id)
            kind_note = "deterministic contract grant; not model-authored"
        else:
            try:
                reply = await self._turn(inbound, context_id, known)
            except ProtocolError as exc:
                emit("a2a", ORG.lower(), "protocol-error", context_id=context_id,
                     error=str(exc), note="VendorCorp could not produce a valid turn")
                raise ValueError(str(exc)) from exc
            self._offers[context_id] = reply["scope"]
            kind_note = "model-authored turn"
            if reply["intent"] == "accept":
                # VendorCorp accepted the buyer's proposal outright: grant from its own scope.
                reply = self._grant(reply, context_id)
                kind_note = "VendorCorp accepted the buyer's scope and published the contract"

        emit(
            "a2a", ORG.lower(), "outbound",
            from_org=ORG, to_peer=PEER, context_id=context_id,
            intent=reply.get("contract_id") and "grant" or reply.get("intent"),
            message=reply, note=kind_note,
        )
        return AgentResponse(messages=[Message("assistant", [json.dumps(reply, separators=(",", ":"))])],
                             response_id=f"vendor-{context_id}")

    # ------------------------------------------------------------------ model-authored turns

    def _parse(self, text: str, known: set[str]) -> dict[str, Any]:
        if not text.strip().startswith("{"):
            raise SchemaError(f"expected a negotiation JSON object, got: {text[:160]!r}")
        try:
            raw = json.loads(text)
        except json.JSONDecodeError as exc:
            raise SchemaError(f"body is not valid JSON ({exc.msg} at char {exc.pos})") from exc
        return validate_message(raw, expect_from="buyer", known_columns=known)

    async def _turn(self, inbound: dict[str, Any], context_id: str, known: set[str]) -> dict[str, Any]:
        transcript = self._transcripts.setdefault(context_id, [])
        transcript.append({"role": "buyer", "message": inbound})
        prompt = (
            schema_block_for(known)
            + "\n\nNegotiation so far in this conversation, oldest first:\n"
            + json.dumps([t["message"] for t in transcript], indent=1)
            + f"\n\nThe buyer's latest message is the last one above. Answer it as {ORG}."
        )
        reply = await ask_json(self.cfg, self.agent, prompt, side=ORG.lower(), turn=inbound["intent"])
        reply = validate_message(reply, expect_from="vendor", known_columns=known)
        reply = self._apply_policy(reply)
        transcript.append({"role": "vendor", "message": reply})
        return reply

    def _apply_policy(self, message: dict[str, Any]) -> dict[str, Any]:
        """Caps that hold even if the model over-grants."""
        kept = [c for c in message["scope"]["columns"] if c not in PII_COLUMNS]
        removed = sorted(set(message["scope"]["columns"]) - set(kept))
        if removed:
            emit(
                "contract", ORG.lower(), "policy-filter",
                removed=removed,
                reason="PII_COLUMNS is enforced in vendor_agent.py after the model replies",
                model_scope=message["scope"]["columns"],
            )
            if message["intent"] != "reject":
                message["intent"] = "counter"
                message["rationale"] = (
                    f"{message['rationale']} VendorCorp policy removed personal-data column(s) "
                    f"{removed}."
                )
        if not kept and message["intent"] != "reject":
            return {
                "from": "vendor",
                "intent": "reject",
                "scope": {"columns": [], "purpose": message["scope"]["purpose"],
                          "retention_days": message["scope"]["retention_days"]},
                "rationale": "The only columns requested were personal data, so there is no "
                             "compliant scope to counter with.",
            }
        message["scope"]["columns"] = kept
        message["scope"]["retention_days"] = min(
            message["scope"]["retention_days"], contract_mod.MAX_RETENTION_DAYS
        )
        return message

    # -------------------------------------------------------------------- contract handling

    def _grant(self, accepted: dict[str, Any], context_id: str) -> dict[str, Any]:
        """Honour an `accept` against VendorCorp's own last offer, then publish the contract."""
        offered = self._offers.get(context_id)
        if offered is None:
            emit("contract", ORG.lower(), "accept-rejected", context_id=context_id,
                 reason="no offer on file for this conversation")
            return {
                "from": "vendor", "intent": "reject",
                "scope": {"columns": [], "purpose": accepted["scope"]["purpose"], "retention_days": 1},
                "rationale": "VendorCorp has no open offer in this conversation, so there is nothing to accept.",
            }

        buyer_cols, offered_cols = set(accepted["scope"]["columns"]), set(offered["columns"])
        widening = sorted(buyer_cols - offered_cols)
        if widening:
            emit("contract", ORG.lower(), "accept-rejected", context_id=context_id,
                 reason=f"accept widened the scope to {widening}", attempted=sorted(buyer_cols))
            return {
                "from": "vendor", "intent": "reject",
                "scope": {"columns": sorted(offered_cols), "purpose": offered["purpose"],
                          "retention_days": offered["retention_days"]},
                "rationale": (
                    f"An accept may only confirm the scope VendorCorp offered; {widening} was never "
                    "offered, so this reads as a new proposal rather than an acceptance."
                ),
            }

        published = contract_mod.publish(
            columns=sorted(offered_cols - PII_COLUMNS),
            purpose=offered["purpose"],
            retention_days=offered["retention_days"],
            buyer=PEER,
            context_id=context_id,
        )
        emit("contract", ORG.lower(), "grant", contract=published, context_id=context_id)
        return {
            "contract_id": published["contract_id"],
            "granted": published["scope"],
            "expires_at": published["expires_at"],
            "data_plane": {"transport": "mcp-streamable-http", "url": self.cfg.mcp_url,
                           "tools": self._tools},
            "enforcement": (
                "Scope is checked by the VendorCorp MCP server on every call. This agent does not "
                "vouch for the caller and cannot widen the grant."
            ),
        }


def _inbound_text(messages: Any) -> str:
    if isinstance(messages, str):
        return messages
    if isinstance(messages, list):
        return "\n".join(str(getattr(item, "text", None) or item) for item in messages)
    return str(messages or "")


def _tool_schema(fn: Any) -> Any:
    """The input schema of an MCP-backed FunctionTool, however this version exposes it."""
    for getter in (
        lambda: fn.to_json_schema_spec(),
        lambda: fn.input_model,
        lambda: fn.parameters,
    ):
        try:
            value = getter()
        except Exception:
            continue
        if isinstance(value, dict) and value:
            return json.loads(json.dumps(value, default=str))
        if value is not None and hasattr(value, "model_json_schema"):
            return value.model_json_schema()
    return None


async def fetch_tool_schemas(cfg: config_mod.Config) -> list[dict[str, Any]]:
    """VendorCorp reads its own MCP server's tool list so the grant can carry real schemas."""
    from agent_framework import MCPStreamableHTTPTool

    try:
        async with MCPStreamableHTTPTool(name="vendorcorp-data", url=cfg.mcp_url) as tool:
            return [
                {
                    "name": fn.name,
                    "description": (fn.description or "")[:900],
                    "input_schema": _tool_schema(fn),
                }
                for fn in tool.functions
            ]
    except Exception as exc:
        emit(
            "mcp", ORG.lower(), "unreachable",
            url=cfg.mcp_url, error=f"{type(exc).__name__}: {str(exc)[:300]}",
            note="VendorCorp could not read its own MCP tool list. Start mcp_server.py first.",
        )
        return []


def build_card(cfg: config_mod.Config, tools: list[dict[str, Any]]) -> AgentCard:
    host, port = cfg.vendor_host
    url = f"http://{host}:{port}/"
    skills = [
        AgentSkill(
            id="vendor-dataset-schema",
            name="Negotiable dataset schema",
            description=(
                "Column names VendorCorp will negotiate access to. Names only: no rows and no "
                "values are exposed before a contract is granted."
            ),
            tags=columns(),
            input_modes=["text"],
            output_modes=["application/json"],
        )
    ] + [
        AgentSkill(
            id=f"mcp-{tool['name']}",
            name=f"MCP tool {tool['name']}",
            description=(
                f"VendorCorp data-plane tool at {cfg.mcp_url}, callable once a contract is granted. "
                f"{tool['description']}"
            )[:900],
            tags=["mcp", "data-plane", cfg.mcp_url],
            input_modes=["application/json"],
            output_modes=["application/json"],
        )
        for tool in tools
    ]
    return AgentCard(
        name=f"{ORG} Data Steward",
        description=(
            "Negotiates data-sharing scope with peer organisations over A2A, then enforces that "
            "scope through an MCP data plane. Personal-data columns are refused in code."
        ),
        version="1.0.0",
        provider={"url": url, "organization": ORG},
        default_input_modes=["text"],
        default_output_modes=["text"],
        capabilities=AgentCapabilities(streaming=False),
        supported_interfaces=[AgentInterface(url=url, protocol_binding="JSONRPC")],
        skills=skills,
    )


def create_app(cfg: config_mod.Config, tools: list[dict[str, Any]]) -> FastAPI:
    service = VendorService(cfg, tools)
    card = build_card(cfg, tools)
    handler = DefaultRequestHandler(
        agent_executor=A2AExecutor(service, stream=False),
        task_store=InMemoryTaskStore(),
        agent_card=card,
    )

    app = FastAPI(title="consortium :: VendorCorp A2A", version="1.0.0")
    add_a2a_routes_to_fastapi(
        app,
        agent_card_routes=create_agent_card_routes(card),
        jsonrpc_routes=create_jsonrpc_routes(handler, rpc_url="/"),
    )

    @app.get("/health")
    async def health() -> JSONResponse:
        return JSONResponse(
            {
                "service": "consortium-vendor-agent",  # identifies THIS app on the port
                "org": ORG,
                "a2a": "ready",
                "model": cfg.model,
                "mcp_tools": [t["name"] for t in tools],
                "conversations": len(service._offers),
            }
        )

    emit(
        "run", ORG.lower(), "listening",
        role="vendor",
        a2a_url=f"http://{cfg.vendor_host[0]}:{cfg.vendor_host[1]}",
        agent_card_path="/.well-known/agent-card.json",
        mcp_url=cfg.mcp_url,
        mcp_tools=[t["name"] for t in tools] or None,
        model=cfg.model,
        base_url=cfg.base_url,
    )
    return app


if __name__ == "__main__":
    cfg = config_mod.load()
    tools = asyncio.run(fetch_tool_schemas(cfg))
    host, port = cfg.vendor_host
    print(f"[vendor] {ORG} A2A on http://{host}:{port}/  card at /.well-known/agent-card.json")
    print(f"[vendor] model {cfg.model} @ {cfg.base_url}")
    print(f"[vendor] data plane {cfg.mcp_url} ({len(tools)} tools discovered)")
    uvicorn.run(create_app(cfg, tools), host=host, port=port, log_level="warning")
