"""BuyerCorp's agent: an A2A endpoint on :8102, plus the negotiation and execution driver.

BuyerCorp wants one aggregate statistic that only VendorCorp can compute. This process:

1. discovers the peer by fetching `/.well-known/agent-card.json` and reads the schema off the card,
2. negotiates scope with the vendor's agent as a sequence of A2A messages — one JSON message per
   protocol turn, every wire event logged as it arrives,
3. once VendorCorp grants a contract, calls the *vendor's* MCP tools over the data plane the grant
   names, and
4. answers the business question with what comes back — including when it comes back as a refusal.

The driver, not the framework's orchestration, owns the loop, so every proposal, counter and
acceptance is a separate, observable A2A message.

Run:  .venv/bin/python buyer_agent.py
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from typing import Any

import uvicorn
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.routes import add_a2a_routes_to_fastapi, create_agent_card_routes, create_jsonrpc_routes
from a2a.server.tasks import InMemoryTaskStore
from a2a.types import AgentCapabilities, AgentCard, AgentInterface, AgentSkill
from agent_framework import AgentResponse, AgentSession, MCPStreamableHTTPTool, Message
from agent_framework.exceptions import ToolExecutionException
from agent_framework.a2a import A2AExecutor, A2AAgent, A2AServiceSessionId
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from google.protobuf import json_format

from core import config as config_mod
from core import contract as contract_mod
from core.dataset import AGGREGATES
from core.events import emit, set_active_run
from core.model import ProtocolError, ask_json, ask_text, get_agent
from core.negotiation import SchemaError, schema_block_for, validate_message

ORG = "BuyerCorp"
PEER = "VendorCorp"
MAX_TURNS = 6

INSTRUCTIONS = f"""You are Insight-1, the analytics-procurement agent for {ORG}.

Your principal asked a business question. You do not hold the data; {PEER} does. Before any query
runs you must agree a data-sharing contract with {PEER}'s steward agent, over A2A.

Policy:
1. Propose the smallest column set that can answer the question: the column to group by, the
   column to measure, and any column the question filters on.
2. Request a personal-data column (customer_name, customer_email) only if the question itself
   names it. {PEER} decides whether to allow it and will refuse personal data.
3. When {PEER}'s latest scope is enough to answer the question, reply intent="accept" and echo
   exactly the columns {PEER} offered. An accept that adds a column is treated as a new proposal.
4. If the offered scope cannot answer the question, counter once with what you would need; if
   {PEER} refuses again, reply intent="reject".
5. Use only column names from the schema in the prompt. Never invent one.
6. `rationale` is one sentence.

Answer with one JSON object and no other text."""

PLAN_INSTRUCTIONS = f"""You are the query planner inside {ORG}'s analytics agent.

You will be given a business question and the dataset schema. Emit the single aggregate query that
answers it, for a tool named `query_records`. Pick the columns the QUESTION asks for. Reply with
one JSON object only:

{{"filters": {{"<column>": "<value>"}} or {{"<column>": {{"gte": "..", "lte": ".."}} }},
  "group_by": "<column or null>",
  "aggregate": {{"function": "{"|".join(AGGREGATES)}", "column": "<column or *>"}}}}

Filters on dates use ISO YYYY-MM-DD with gte/lte. "Q3" means July 1 to September 30.
No other keys, no prose, no code fence."""


def _jsonish(text: str) -> dict[str, Any] | None:
    try:
        from core.model import extract_json

        value = extract_json(text)
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


class Driver:
    """Owns one negotiation + execution at a time."""

    def __init__(self, cfg: config_mod.Config) -> None:
        self.cfg = cfg
        self.lock = asyncio.Lock()
        self.busy = False
        self.state: dict[str, Any] = {"status": "idle", "question": None, "contract": None,
                                      "answer": None, "refusal": None}
        self.card: AgentCard | None = None
        self.card_raw: dict[str, Any] = {}
        self.schema: list[str] = []
        self._peer: A2AAgent | None = None

    def refresh(self) -> None:
        """Re-read `.env`, so a sidebar change applies to the next run without a restart."""
        self.cfg = config_mod.load()

    @property
    def agent(self):
        return get_agent(self.cfg, name=f"{ORG} Insight-1", instructions=INSTRUCTIONS)

    @property
    def planner(self):
        return get_agent(self.cfg, name=f"{ORG} Query Planner", instructions=PLAN_INSTRUCTIONS)

    # ------------------------------------------------------------------------ discovery

    async def discover(self) -> dict[str, Any]:
        url = self.cfg.vendor_url.rstrip("/") + "/.well-known/agent-card.json"
        import httpx

        from a2a.client.card_resolver import parse_agent_card

        async with httpx.AsyncClient(timeout=15.0) as client:
            response = await client.get(url)
            response.raise_for_status()
            raw = response.json()
        card = parse_agent_card(raw)
        known = [tag for skill in card.skills if skill.id == "vendor-dataset-schema" for tag in skill.tags]
        self.card, self.card_raw, self.schema = card, raw, known

        emit(
            "a2a", ORG.lower(), "agent-card",
            from_org=PEER, to_org=ORG, url=url, http_status=response.status_code,
            card={
                "name": card.name,
                "version": card.version,
                "provider": card.provider.organization,
                "interfaces": [{"url": i.url, "binding": i.protocol_binding} for i in card.supported_interfaces],
                "streaming": card.capabilities.streaming,
                "skills": [{"id": s.id, "name": s.name, "tags": list(s.tags)[:12]} for s in card.skills],
            },
            note="discovered the peer by fetching its agent card; the dataset schema comes from the card",
        )
        if not known:
            raise RuntimeError(f"{PEER}'s card advertises no negotiable columns")
        self._peer = A2AAgent(name=f"{PEER} Data Steward", agent_card=card, url=self.cfg.vendor_url)
        await self._peer.__aenter__()
        return {"card": raw, "columns": known}

    async def _close_peer(self) -> None:
        if self._peer is not None:
            try:
                await self._peer.__aexit__(None, None, None)
            except Exception:
                pass
            self._peer = None

    # ---------------------------------------------------------------------- negotiation

    async def negotiate(self, question: str) -> dict[str, Any]:
        assert self._peer is not None and self.schema
        known = set(self.schema)
        conversation = f"consortium-{time.strftime('%H%M%S')}-{uuid.uuid4().hex[:6]}"
        transcript: list[dict[str, Any]] = []
        grant: dict[str, Any] | None = None
        outcome = "no-agreement"

        prompt = (
            schema_block_for(known)
            + f"\n\nBusiness question from your principal: {question!r}\n\n"
            "Open the negotiation: send your first message as the buyer with intent=\"propose\"."
        )
        for turn in range(1, MAX_TURNS + 1):
            message = await ask_json(self.cfg, self.agent, prompt, side=ORG.lower(), turn=f"turn-{turn}")
            message = validate_message(message, expect_from="buyer", known_columns=known)
            transcript.append({"role": "buyer", "message": message})

            reply_text = await self._send(message, conversation, direction="send")
            peer = _jsonish(reply_text)
            if peer is None:
                emit("a2a", ORG.lower(), "protocol-error", from_org=PEER, received=reply_text[:400],
                     note="peer reply was not a JSON object")
                raise ProtocolError(f"{PEER} replied with a non-JSON body: {reply_text[:200]!r}")

            if message["intent"] == "reject":
                outcome = "buyer-rejected"
                break

            if "contract_id" in peer:
                grant = peer
                emit("contract", ORG.lower(), "received-grant", from_org=PEER, to_org=ORG,
                     contract=peer, note="stored as the live view of the agreement")
                outcome = "granted"
                break

            counterparty = validate_message(peer, expect_from="vendor", known_columns=known)
            emit("a2a", ORG.lower(), "peer-message", from_org=PEER, to_org=ORG,
                 intent=counterparty["intent"], message=counterparty,
                 note="validated against the negotiation schema")
            transcript.append({"role": "vendor", "message": counterparty})

            if counterparty["intent"] == "reject":
                outcome = "rejected"
                break

            prompt = (
                schema_block_for(known)
                + f"\n\nBusiness question: {question!r}\n\nNegotiation so far, oldest first:\n"
                + json.dumps([t["message"] for t in transcript], indent=1)
                + f"\n\n{PEER}'s latest message is the last one above. Send your next move as the buyer."
            )
            if counterparty["intent"] == "accept":
                # VendorCorp accepted the buyer's scope; it should have answered with a grant
                # instead. Stop rather than accepting our own proposal back to them.
                outcome = "vendor-accepted-without-grant"
                break

        return {"conversation_id": conversation, "transcript": transcript, "grant": grant, "outcome": outcome}

    async def _send(self, message: dict[str, Any], conversation: str, *, direction: str) -> str:
        """One A2A turn: a single `message/send` request, one peer reply.

        Deliberately not SSE streaming: with `A2AExecutor(stream=False)` the streamed reply comes
        back empty in agent-framework 1.21/a2a-sdk 1.2, and every negotiation message is already
        its own protocol turn, so one request/response per message keeps the whole exchange visible.
        """
        assert self._peer is not None
        session = AgentSession(service_session_id=A2AServiceSessionId(context_id=conversation))
        body = json.dumps(message, separators=(",", ":"))
        emit(
            "a2a", ORG.lower(), direction,
            from_org=ORG, to_peer=PEER, context_id=conversation,
            intent=message["intent"], message=message,
            note="sent to the VendorCorp A2A endpoint",
        )
        try:
            final = await self._peer.run(body, session=session)
        except Exception as exc:
            reason = f"{type(exc).__name__}: {str(exc)[:400]}"
            emit("a2a", ORG.lower(), "transport-error", context_id=conversation, error=reason,
                 note="the A2A call to VendorCorp failed")
            raise ConnectionError(reason) from exc

        raw = getattr(final, "raw_representation", None)
        details: dict[str, Any] = {}
        for item in raw if isinstance(raw, (list, tuple)) else [raw]:
            data = _protobuf_dict(item)
            if not data:
                continue
            for key in ("task_id", "context_id", "message_id"):
                if data.get(key):
                    details[f"a2a_{key}"] = data[key]
            status = data.get("status")
            if isinstance(status, dict) and status.get("state"):
                details["task_state"] = status["state"]

        text = _messages_text(final)
        emit("a2a", ORG.lower(), "response", from_org=PEER, to_org=ORG, context_id=conversation,
             text=text[:2000], note="complete reply received over A2A", **details)
        return text

    # ----------------------------------------------------------------------- execution

    async def plan(self, question: str) -> dict[str, Any]:
        base = (
            f"Business question: {question!r}\n\nDataset schema: {sorted(self.schema)}\n\n"
            "Emit the query that answers the question."
        )
        prompt, last = base, ""
        for attempt in (1, 2):
            raw = await ask_json(self.cfg, self.planner, prompt, side=ORG.lower(),
                                 turn=f"plan-{attempt}", max_tokens=700)
            try:
                return normalise_plan(raw)
            except SchemaError as exc:
                last = str(exc)
                emit("model", ORG.lower(), "plan-rejected", attempt=attempt, error=last, produced=raw,
                     note="plan shape is checked before any data-plane call; retried once")
                prompt = f"{base}\n\nYour previous plan was invalid: {last}. Emit a corrected plan."
        raise SchemaError(f"query plan still invalid after one retry: {last}")

    async def execute(self, question: str, grant: dict[str, Any]) -> dict[str, Any]:
        args = await self.plan(question)
        endpoint = grant.get("data_plane", {}).get("url") or self.cfg.mcp_url
        contract_id = grant.get("contract_id")
        granted = sorted(grant.get("granted", {}).get("columns", []))

        emit("mcp", ORG.lower(), "connect", from_org=ORG, to_org=PEER, url=endpoint,
             contract_id=contract_id, granted=granted,
             note="BuyerCorp now talks to VendorCorp's data plane directly")
        tool = MCPStreamableHTTPTool(name="vendorcorp-data", url=endpoint)
        try:
            await tool.connect()
        except Exception as exc:
            reason = f"{type(exc).__name__}: {str(exc)[:500]}"
            emit("mcp", ORG.lower(), "connection-error", tool="connect", url=endpoint, error=reason,
                 note="VendorCorp's MCP server could not be reached")
            raise ConnectionError(reason) from exc

        try:
            emit("mcp", ORG.lower(), "call", tool="query_records", arguments=args,
                 contract_id=contract_id, granted=granted,
                 note="arguments chosen by BuyerCorp's planner, not filtered by the UI")
            try:
                out = await tool.call_tool("query_records", **args)
            except ToolExecutionException as exc:
                # The refusal text comes from the MCP server; the buyer only relays it.
                reason = str(exc)
                emit("mcp", ORG.lower(), "refusal", tool="query_records", arguments=args,
                     contract_id=contract_id, granted=granted, reason=reason,
                     note="refused by the VendorCorp MCP server")
                await self._audit(tool, contract_id, args, status="refused", reason=reason, granted=granted)
                return {"ok": False, "refused": True, "reason": reason, "arguments": args}
            result = _parse_tool_json(out)
            emit("mcp", ORG.lower(), "result", tool="query_records", result=result,
                 contract_id=contract_id, note="real aggregate returned by the MCP server")
            await self._audit(tool, contract_id, args, status="allowed", result=result, granted=granted)
            return {"ok": True, "refused": False, "result": result, "arguments": args}
        finally:
            try:
                await tool.__aexit__(None, None, None)
            except Exception:
                pass

    async def _audit(self, tool, contract_id: str | None, args: dict[str, Any], *, status: str,
                     granted: list[str], reason: str = "", result: dict[str, Any] | None = None) -> None:
        entry = {
            "actor": ORG,
            "purpose": (self.state.get("purpose") or ""),
            "contract_id": contract_id,
            "columns": referenced_in(args),
            "query": args,
            "status": status,
            "refusal_reason": reason or None,
            "rows_matched": (result or {}).get("rows_matched"),
            "granted_columns": granted,
        }
        try:
            out = await tool.call_tool("write_audit_log", entry=entry)
            emit("mcp", ORG.lower(), "result", tool="write_audit_log", arguments={"entry": entry},
                 result=_parse_tool_json(out), contract_id=contract_id)
        except Exception as exc:
            emit("mcp", ORG.lower(), "refusal", tool="write_audit_log", arguments={"entry": entry},
                 reason=str(exc)[:400], contract_id=contract_id)

    async def probe_out_of_scope(self) -> dict[str, Any]:
        """Deliberate, no-LLM probe for a personal-data column outside the granted scope."""
        contract = contract_mod.current()
        if contract is None:
            return {"ok": False, "error": "no contract on file; complete a negotiation first"}
        endpoint = self.cfg.mcp_url
        args = {"filters": {}, "group_by": "customer_email", "aggregate": {"function": "count", "column": "*"}}
        emit("mcp", ORG.lower(), "call", tool="query_records", arguments=args,
             contract_id=contract["contract_id"], granted=sorted(contract["scope"]["columns"]),
             note="deliberate out-of-scope probe requested from the observer")
        tool = MCPStreamableHTTPTool(name="vendorcorp-data", url=endpoint)
        try:
            await tool.connect()
            try:
                out = await tool.call_tool("query_records", **args)
                emit("mcp", ORG.lower(), "result", tool="query_records", arguments=args,
                     result=_parse_tool_json(out),
                     note="UNEXPECTED: the server allowed a column outside the contract")
                return {"ok": True, "unexpected": True, "result": _parse_tool_json(out)}
            except ToolExecutionException as exc:
                reason = str(exc)
                emit("mcp", ORG.lower(), "refusal", tool="query_records", arguments=args,
                     contract_id=contract["contract_id"], granted=sorted(contract["scope"]["columns"]),
                     reason=reason, note="refused by the VendorCorp MCP server, not by the UI")
                await self._audit(tool, contract["contract_id"], args, status="refused",
                                  reason=reason, granted=sorted(contract["scope"]["columns"]))
                return {"ok": False, "refused": True, "reason": reason, "arguments": args}
        except Exception as exc:
            reason = f"{type(exc).__name__}: {str(exc)[:400]}"
            emit("mcp", ORG.lower(), "connection-error", tool="query_records", url=endpoint, error=reason)
            return {"ok": False, "error": reason}
        finally:
            try:
                await tool.__aexit__(None, None, None)
            except Exception:
                pass

    # --------------------------------------------------------------------------- runs

    async def start_run(self, question: str) -> str:
        run_id = f"run-{time.strftime('%H%M%S')}-{uuid.uuid4().hex[:6]}"
        set_active_run(run_id, question=question, buyer=ORG, vendor=PEER)
        emit("run", ORG.lower(), "start", question=question, model=self.cfg.model,
             base_url=self.cfg.base_url, run_id=run_id)
        return run_id

    async def run(self, question: str) -> dict[str, Any]:
        async with self.lock:
            self.busy = True
            self.refresh()
            self.state = {"status": "negotiating", "question": question, "contract": None,
                          "answer": None, "refusal": None}
            run_id = await self.start_run(question)
            try:
                await self.discover()
                negotiation = await self.negotiate(question)
                self.state["transcript"] = negotiation["transcript"]
                grant = negotiation["grant"]
                if grant is None:
                    self.state["status"] = "no-agreement"
                    emit("run", ORG.lower(), "no-agreement", outcome=negotiation["outcome"], run_id=run_id)
                    return {"run_id": run_id, "ok": False, "outcome": negotiation["outcome"],
                            "negotiation": negotiation}

                self.state["contract"] = grant
                self.state["purpose"] = grant.get("granted", {}).get("purpose", "")
                self.state["status"] = "executing"
                execution = await self.execute(question, grant)
                if execution.get("refused"):
                    self.state.update(status="refused", refusal=execution["reason"])
                    emit("run", ORG.lower(), "refused", reason=execution["reason"], run_id=run_id)
                else:
                    self.state.update(status="complete", answer=execution["result"])
                    emit("run", ORG.lower(), "answer", result=execution["result"],
                         arguments=execution["arguments"], run_id=run_id)
                return {"run_id": run_id, "ok": not execution.get("refused"),
                        "negotiation": negotiation, "execution": execution}
            except Exception as exc:
                reason = f"{type(exc).__name__}: {str(exc)[:600]}"
                self.state.update(status="error", error=reason)
                emit("run", ORG.lower(), "error", error=reason, question=question,
                     note="the run stopped; the message above is the real failure")
                return {"run_id": run_id, "ok": False, "error": reason}
            finally:
                await self._close_peer()
                self.busy = False

    async def run_probe(self) -> dict[str, Any]:
        async with self.lock:
            self.busy = True
            self.refresh()
            run_id = f"probe-{time.strftime('%H%M%S')}-{uuid.uuid4().hex[:6]}"
            set_active_run(run_id, question="(out-of-scope probe)", kind="probe")
            emit("run", ORG.lower(), "start", question="probe customer_email", run_id=run_id,
                 note="direct MCP probe: no model call, no negotiation")
            try:
                self.state["status"] = "executing"
                out = await self.probe_out_of_scope()
                if out.get("refused"):
                    self.state.update(status="refused", refusal=out["reason"])
                    emit("run", ORG.lower(), "refused", reason=out["reason"], run_id=run_id)
                elif out.get("error"):
                    self.state.update(status="error", error=out["error"])
                    emit("run", ORG.lower(), "error", error=out["error"], run_id=run_id,
                         note="the MCP data plane could not be reached")
                else:
                    self.state.update(status="complete", answer=out.get("result") or out.get("error"))
                    emit("run", ORG.lower(), "answer", result=out.get("result"), error=out.get("error"),
                         run_id=run_id)
                return {"run_id": run_id, **out}
            except Exception as exc:
                reason = f"{type(exc).__name__}: {str(exc)[:600]}"
                self.state.update(status="error", error=reason)
                emit("run", ORG.lower(), "error", error=reason, run_id=run_id)
                return {"run_id": run_id, "ok": False, "error": reason}
            finally:
                self.busy = False


def normalise_plan(raw: dict[str, Any]) -> dict[str, Any]:
    """Shape-check the planner's output without deciding what the answer should be."""
    filters = raw.get("filters", {})
    if isinstance(filters, str):
        filters = _jsonish(filters) or {}
    if not isinstance(filters, dict):
        raise SchemaError(f"filters must be an object, got {type(filters).__name__}")

    group_by = raw.get("group_by")
    if isinstance(group_by, str):
        group_by = group_by.strip() or None
        if group_by and group_by.lower() in {"null", "none"}:
            group_by = None
    if group_by is not None and not isinstance(group_by, str):
        raise SchemaError(f"group_by must be a column name or null, got {group_by!r}")

    aggregate = raw.get("aggregate")
    if isinstance(aggregate, str):
        aggregate = _jsonish(aggregate)
    if not isinstance(aggregate, dict):
        raise SchemaError("aggregate must be {\"function\": ..., \"column\": ...}")
    function = str(aggregate.get("function", "")).strip().lower()
    column = str(aggregate.get("column", "")).strip()
    if function not in AGGREGATES:
        raise SchemaError(f"aggregate.function {function!r} is not one of {list(AGGREGATES)}")
    if not column:
        raise SchemaError("aggregate.column is required (use \"*\" to count rows)")
    return {"filters": filters, "group_by": group_by, "aggregate": {"function": function, "column": column}}


def referenced_in(args: dict[str, Any]) -> list[str]:
    """Columns a query plan touches — the same arithmetic the MCP server enforces."""
    cols = set(args.get("filters") or {})
    if args.get("group_by"):
        cols.add(args["group_by"])
    column = (args.get("aggregate") or {}).get("column")
    if column and column != "*":
        cols.add(str(column))
    return sorted(cols)


def _messages_text(response: Any) -> str:
    messages = getattr(response, "messages", None)
    if messages is None:
        update = getattr(response, "contents", None)
        if update is not None:
            return " ".join(str(getattr(c, "text", "") or "") for c in update).strip()
        return ""
    chunks = []
    for message in messages:
        text = getattr(message, "text", None)
        if text:
            chunks.append(text if isinstance(text, str) else str(text))
    return "\n".join(chunks).strip()


def _protobuf_dict(raw: Any) -> dict[str, Any]:
    """A protobuf A2A object as a plain dict; {} for anything that isn't one."""
    if raw is None or not hasattr(raw, "DESCRIPTOR"):
        return {}
    try:
        return json_format.MessageToDict(raw, preserving_proto_field_name=True)
    except Exception:
        return {}


def _parse_tool_json(out: Any) -> Any:
    text = out if isinstance(out, str) else "\n".join(
        str(getattr(c, "text", "") or "") for c in (out or [])
    )
    try:
        return json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return {"raw": text[:2000]}


class BuyerService:
    """BuyerCorp's own A2A-facing agent, so the peer can ask this side what it agreed to."""

    def __init__(self, driver: Driver) -> None:
        self._driver = driver

    def create_session(self, *, session_id: str | None = None):
        return self.agent.create_session(session_id=session_id)

    @property
    def agent(self):
        return get_agent(self._driver.cfg, name=f"{ORG} Peer Agent", instructions=INSTRUCTIONS)

    async def run(self, messages: Any = None, *, stream: bool = False, session: Any = None,
                  **kwargs: Any) -> AgentResponse:
        if stream:
            raise NotImplementedError("BuyerCorp answers peer questions without streaming.")
        question = _messages_text(messages) or str(messages)
        facts = {
            "status": self._driver.state.get("status"),
            "question": self._driver.state.get("question"),
            "contract": self._driver.state.get("contract"),
            "refusal": self._driver.state.get("refusal"),
        }
        prompt = (
            "You are Insight-1, the analytics-procurement agent for BuyerCorp, answering a peer "
            "organisation over A2A. Describe BuyerCorp's side of the current data-sharing "
            "deal in two or three sentences: the business question, the scope actually granted, "
            "and anything that was refused. Only use the facts given; do not invent status.\n\n"
            f"FACTS: {json.dumps(facts, default=str)}\n\nPeer question: {question!r}"
        )
        text = await ask_text(self._driver.cfg, self.agent, prompt, side=ORG.lower(), turn="peer-question")
        emit("a2a", ORG.lower(), "answered-peer", from_org=ORG, question=question[:300], answer=text[:600])
        return AgentResponse(messages=[Message("assistant", [text])], response_id="buyer-peer-answer")


def build_card(cfg: config_mod.Config) -> AgentCard:
    host, port = cfg.buyer_host
    url = f"http://{host}:{port}/"
    return AgentCard(
        name=f"{ORG} Insight Agent",
        description=(
            "Requests aggregate statistics from peer organisations. Publishes its side of an "
            "agreed data-sharing contract to anyone that asks over A2A."
        ),
        version="1.0.0",
        provider={"url": url, "organization": ORG},
        default_input_modes=["text"],
        default_output_modes=["text"],
        capabilities=AgentCapabilities(streaming=False),
        supported_interfaces=[AgentInterface(url=url, protocol_binding="JSONRPC")],
        skills=[
            AgentSkill(
                id="buyer-status",
                name="Current negotiation status",
                description="Ask what BuyerCorp requested, what was granted, and what was refused.",
                tags=["status", "contract"],
                input_modes=["text"],
                output_modes=["text"],
            )
        ],
    )


def create_app(cfg: config_mod.Config) -> tuple[FastAPI, Driver]:
    driver = Driver(cfg)
    service = BuyerService(driver)
    card = build_card(cfg)
    handler = DefaultRequestHandler(
        agent_executor=A2AExecutor(service, stream=False),
        task_store=InMemoryTaskStore(),
        agent_card=card,
    )

    app = FastAPI(title="consortium :: BuyerCorp A2A", version="1.0.0")
    add_a2a_routes_to_fastapi(
        app,
        agent_card_routes=create_agent_card_routes(card),
        jsonrpc_routes=create_jsonrpc_routes(handler, rpc_url="/"),
    )

    @app.get("/health")
    async def health() -> JSONResponse:
        return JSONResponse(
            {
                "service": "consortium-buyer-agent",
                "org": ORG,
                "a2a": "ready",
                "model": cfg.model,
                "busy": driver.busy,
                "state": driver.state.get("status"),
            }
        )

    @app.get("/state")
    async def state() -> JSONResponse:
        return JSONResponse(driver.state)

    @app.post("/runs")
    async def start_run(payload: dict[str, Any]) -> JSONResponse:
        question = str(payload.get("question", "")).strip()
        if not question:
            return JSONResponse({"error": "question is required"}, status_code=422)
        if driver.busy:
            return JSONResponse({"error": "a negotiation is already running"}, status_code=409)
        task = asyncio.create_task(driver.run(question))
        return JSONResponse({"started": True, "question": question, "task": id(task)}, status_code=202)

    @app.post("/probes/out-of-scope")
    async def start_probe() -> JSONResponse:
        if driver.busy:
            return JSONResponse({"error": "a negotiation is already running"}, status_code=409)
        task = asyncio.create_task(driver.run_probe())
        return JSONResponse({"started": True, "task": id(task)}, status_code=202)

    emit(
        "run", ORG.lower(), "listening", role="buyer",
        a2a_url=f"http://{cfg.buyer_host[0]}:{cfg.buyer_host[1]}",
        agent_card_path="/.well-known/agent-card.json",
        vendor_url=cfg.vendor_url, model=cfg.model, base_url=cfg.base_url,
    )
    return app, driver


if __name__ == "__main__":
    cfg = config_mod.load()
    host, port = cfg.buyer_host
    app, _ = create_app(cfg)
    print(f"[buyer] {ORG} A2A on http://{host}:{port}/  card at /.well-known/agent-card.json")
    print(f"[buyer] model {cfg.model} @ {cfg.base_url}")
    print(f"[buyer] will discover the peer at {cfg.vendor_url}/.well-known/agent-card.json")
    uvicorn.run(app, host=host, port=port, log_level="warning")
