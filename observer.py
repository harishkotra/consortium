"""Consortium observer: watch the A2A negotiation and the MCP data plane, message by message.

Run last:  .venv/bin/streamlit run observer.py --server.port 8501 --server.headless true

Everything on screen comes from `data/events.jsonl`, which the MCP server and both agent
processes append to. An empty log renders an empty state — the UI never composes a message,
never pretends a call happened, and renders refusal text exactly as the MCP server produced it.
"""

from __future__ import annotations

import asyncio
import html
import json
import time
from typing import Any

import httpx
import streamlit as st

from core import config as config_mod
from core import contract as contract_mod
from core.events import active_run, read_all
from core.model import test_connection

BUYER, VENDOR = "BuyerCorp", "VendorCorp"

INTENT_STYLE = {
    "propose": ("#4c8dff", "PROPOSE"),
    "counter": ("#f0a92c", "COUNTER"),
    "accept": ("#2ecc8f", "ACCEPT"),
    "reject": ("#ff5c7a", "REJECT"),
    "request": ("#b57bff", "REQUEST"),
    "grant": ("#00c2c7", "GRANT"),
    "result": ("#2ecc8f", "RESULT"),
    "refusal": ("#ff5c7a", "REFUSED"),
    "error": ("#ff5c7a", "ERROR"),
    "card": ("#8f9bb3", "AGENT CARD"),
    "other": ("#8f9bb3", ""),
}

CSS = """
<style>
.cq-tl {display:flex; flex-direction:column; gap:.55rem;}
.cq-card {border:1px solid #2b3242; border-left-width:4px; border-radius:7px;
          padding:.5rem .65rem; background:#151a23;}
.cq-head {display:flex; justify-content:space-between; align-items:baseline; gap:.5rem;
          font-size:.74rem; letter-spacing:.05em; margin-bottom:.28rem;}
.cq-org {font-weight:700; text-transform:uppercase;}
.cq-chip {font-size:.66rem; font-weight:700; letter-spacing:.08em; padding:.08rem .42rem;
          border-radius:999px; color:#0b0e13; white-space:nowrap;}
.cq-why {font-size:.8rem; color:#c9d3e3; margin-bottom:.3rem;}
.cq-payload {font-family:ui-monospace,SFMono-Regular,Menlo,monospace; font-size:.7rem;
             line-height:1.45; white-space:pre-wrap; word-break:break-word; margin:0;
             background:#0d1117; border:1px solid #232a37; border-radius:5px;
             padding:.42rem .5rem; max-height:15rem; overflow:auto; color:#b8c4d9;}
.cq-meta {font-size:.66rem; color:#77839a; margin-top:.3rem;
          font-family:ui-monospace,SFMono-Regular,Menlo,monospace;}
.cq-state {display:flex; align-items:center; gap:.6rem; padding:.5rem .8rem; border-radius:8px;
           border:1px solid #2b3242; background:#121722; margin-bottom:.4rem;}
.cq-dot {width:.6rem; height:.6rem; border-radius:50%; flex:none;}
.cq-empty {border:1px dashed #39435a; border-radius:9px; padding:2rem 1.4rem; color:#93a0b8;
           text-align:center;}
.cq-empty code {color:#c9d3e3;}
</style>
"""

st.set_page_config(page_title="Consortium · agent negotiation", layout="wide", page_icon="⇄")
st.markdown(CSS, unsafe_allow_html=True)


# --------------------------------------------------------------------------- helpers


def jdump(value: Any, limit: int = 2400) -> str:
    try:
        text = json.dumps(value, indent=2, ensure_ascii=False, default=str)
    except Exception:
        text = str(value)
    return text if len(text) <= limit else text[:limit] + f"\n… truncated ({len(text)} chars)"


def card_html(*, side: str, intent: str, title: str, why: str = "", payload: Any = None,
              meta: str = "", color: str = "#8f9bb3", chip: str = "") -> str:
    label = chip or INTENT_STYLE.get(intent, INTENT_STYLE["other"])[1]
    chip_html = (
        f'<span class="cq-chip" style="background:{color};">{html.escape(label)}</span>'
        if label
        else ""
    )
    why_html = f'<div class="cq-why">{html.escape(why)}</div>' if why else ""
    body = (
        f'<pre class="cq-payload">{html.escape(payload if isinstance(payload, str) else jdump(payload))}</pre>'
        if payload not in (None, "", {}, [])
        else ""
    )
    meta_html = f'<div class="cq-meta">{html.escape(meta)}</div>' if meta else ""
    return (
        f'<div class="cq-card" style="border-left-color:{color};">'
        f'<div class="cq-head"><span class="cq-org" style="color:{color};">{html.escape(title)}</span>'
        f"{chip_html}</div>{why_html}{body}{meta_html}</div>"
    )


def side_of(event: dict[str, Any]) -> str:
    """Which column an event belongs in."""
    actor = (event.get("actor") or "").lower()
    org = (event.get("from_org") or event.get("from") or "").lower()
    if event["plane"] == "mcp" and "vendorcorp-mcp" in actor:
        return "vendor"
    if actor in ("buyercorp", "consortium-buyer-agent") or org == "buyer":
        return "buyer"
    if actor in ("vendorcorp", "consortium-vendor-agent", "vendorcorp-mcp") or org == "vendor":
        return "vendor"
    return "buyer" if "buyer" in actor else "vendor"


_ENVELOPE = {"seq", "ts", "plane", "actor", "kind", "run_id", "note", "from_org", "to_org", "to_peer"}

#: Housekeeping events, hidden when the timeline is set to "messages and tools only".
NOISE_KINDS = {"listening", "starting", "connect", "stopped", "unreachable", "fallback"}


def payload_of(event: dict[str, Any]) -> tuple[str, str, Any]:
    """(intent, one-line description, payload to print) for any event kind."""
    kind, plane = event.get("kind"), event.get("plane")
    message = event.get("message")
    if isinstance(message, dict):
        scope = message.get("scope", {}) or {}
        intent = str(message.get("intent") or kind)
        why = message.get("rationale") or event.get("note") or ""
        cols = scope.get("columns") or []
        head = f'{scope.get("purpose", "")} · {len(cols)} column(s): {", ".join(cols) or "none"}'
        return intent, (head if plane == "a2a" else why or head), message
    if kind == "agent-card":
        return "card", event.get("note", "peer discovered"), event.get("card")
    if kind == "call" and plane == "mcp":
        args = event.get("arguments") or {}
        return "request", f'tool_call {event.get("tool")} → {BUYER}→{VENDOR} data plane', args
    if kind == "result" and plane == "mcp":
        return "result", f'{event.get("tool")} returned', event.get("result")
    if kind == "refusal" and plane == "mcp":
        return "refusal", (event.get("reason") or "")[:300], {
            "tool": event.get("tool"),
            "arguments": event.get("arguments"),
            "granted_by_contract": event.get("granted"),
            "refusal_reason_from_mcp_server": event.get("reason"),
        }
    if kind in ("connection-error", "transport-error", "unreachable"):
        return "error", event.get("note", "transport failure"), {
            "error": event.get("error") or event.get("reason"),
            "target": event.get("url") or event.get("context_id"),
        }
    if plane == "contract":
        return ("grant" if kind == "grant" else "other"), event.get("note") or kind, \
            event.get("contract") or {"reason": event.get("reason"), "context_id": event.get("context_id")}
    if plane == "model":
        return ("other", f'model call · {event.get("turn")} · {event.get("latency_ms")}ms', {
            "model": event.get("model"), "json_mode": event.get("json_mode"),
            "attempts": event.get("attempts"), "usage": event.get("usage"),
            "error": event.get("error"), "raw": event.get("raw"),
        })
    # Everything else: show the event's own fields minus the envelope, so nothing is invented.
    text = event.get("text") or event.get("reason") or event.get("error") or ""
    rest = {k: v for k, v in event.items() if k not in _ENVELOPE}
    return "other", (event.get("note") or text or str(kind))[:220], rest or {"detail": text[:2000]}


def derive_state(events: list[dict[str, Any]]) -> tuple[str, str]:
    """One of: idle | negotiating | executing | complete | error — always derived from the log."""
    if not events:
        return "idle", "No event has been written yet. The timeline stays empty until a real run happens."
    starts = [i for i, e in enumerate(events) if e["plane"] == "run" and e.get("kind") == "start"]
    if not starts:
        return "idle", "The services logged start-up events but no negotiation has been requested."
    tail = events[starts[-1] :]
    run_id = tail[0].get("run_id")
    for e in tail:
        if e.get("kind") in ("error", "transport-error", "connection-error", "unreachable"):
            if e["plane"] in ("run", "a2a", "mcp"):
                return "error", f'{e.get("actor")} · {str(e.get("error") or e.get("reason"))[:300]}'
    for e in reversed(tail):
        if e["plane"] == "run" and e.get("kind") == "refused":
            return "complete", f"VendorCorp's MCP server refused the query · run {run_id}"
        if e["plane"] == "run" and e.get("kind") == "answer":
            return "complete", f"Answer computed from the dataset under the granted contract · run {run_id}"
        if e["plane"] == "run" and e.get("kind") == "no-agreement":
            return "complete", f"No contract was agreed ({e.get('outcome')}) · run {run_id}"
    if any(e["plane"] == "mcp" and e.get("kind") in ("call", "connect", "result", "refusal") for e in tail):
        return "executing", "Calling the VendorCorp MCP data plane"
    if any(e["plane"] == "contract" and e.get("kind") == "grant" for e in tail):
        return "executing", "Contract granted; query is being issued"
    return "negotiating", "A2A messages are exchanging between the two agents"


# ----------------------------------------------------------------------------- sidebar


def _apply_preset(name: str) -> None:
    preset = config_mod.PRESETS[name]
    st.session_state["cfg_base_url"] = preset["base_url"]
    if preset.get("api_key"):
        st.session_state["cfg_api_key"] = preset["api_key"]
    if preset.get("model"):
        st.session_state["cfg_model"] = preset["model"]
    st.session_state["preset_note"] = preset["hint"]


def _current_cfg() -> config_mod.Config:
    return config_mod.load().with_overrides(
        base_url=st.session_state.get("cfg_base_url", ""),
        api_key=st.session_state.get("cfg_api_key", ""),
        model=st.session_state.get("cfg_model", ""),
    )


def _save_cfg() -> None:
    path = config_mod.save(_current_cfg())
    st.session_state["saved_at"] = time.strftime("%H:%M:%S")
    st.session_state["save_note"] = str(path)


def sidebar() -> None:
    cfg = config_mod.load()
    st.sidebar.markdown("### Model provider")
    if "cfg_base_url" not in st.session_state:
        st.session_state["cfg_base_url"] = cfg.base_url
        st.session_state["cfg_api_key"] = cfg.api_key
        st.session_state["cfg_model"] = cfg.model

    st.sidebar.text_input("Base URL", key="cfg_base_url", on_change=_save_cfg)
    st.sidebar.text_input("API key", key="cfg_api_key", type="password", on_change=_save_cfg)
    st.sidebar.text_input("Model", key="cfg_model", on_change=_save_cfg)
    if st.session_state.get("preset_note"):
        st.sidebar.caption(st.session_state["preset_note"])

    cols = st.sidebar.columns(4)
    for index, name in enumerate(config_mod.PRESETS):
        cols[index].button(name, width='stretch', on_click=_apply_preset, args=(name,))

    if st.sidebar.button("Test connection", width='stretch'):
        attempt = _current_cfg()
        with st.sidebar.spinner(f"Calling {attempt.model} …", show="spinner"):
            result = asyncio.run(test_connection(attempt))
        st.session_state["connection_test"] = result
        if result.get("ok"):
            _save_cfg()

    test = st.session_state.get("connection_test")
    if test:
        if test.get("ok"):
            st.sidebar.success(
                f"real reply: {test.get('content')!r} · HTTP {test.get('status')} · "
                f"{test.get('latency_ms')} ms · {json.dumps(test.get('usage') or {})}"
            )
        else:
            st.sidebar.error(f"HTTP {test.get('status')} — {test.get('error')}")
            if test.get("hint"):
                st.sidebar.warning(test["hint"])

    services_block(cfg)
    contract = contract_mod.current()
    st.sidebar.markdown("### Live contract")
    if contract:
        st.sidebar.caption(
            f'{contract["contract_id"]} · {len(contract["scope"]["columns"])} columns · '
            f'expires {contract["expires_at"]}'
        )
    else:
        st.sidebar.caption("none — no negotiation has been accepted yet")

    st.sidebar.markdown("### Event log")
    st.sidebar.caption(f'{config_mod.ROOT / "data" / "events.jsonl"}')
    st.sidebar.selectbox(
        "Timeline",
        ("All events", "Messages and tools only"),
        key="timeline_mode",
    )
    if st.sidebar.button("Clear event log", width='stretch'):
        (config_mod.ROOT / "data" / "events.jsonl").write_text("")
        contract_mod.clear()
        st.session_state["question"] = ""


def _dot(colour: str, text: str) -> None:
    st.sidebar.markdown(
        f"<div style='font-size:.73rem;color:{colour};margin-bottom:.18rem'>● {html.escape(text)}"
        "</div>",
        unsafe_allow_html=True,
    )


def _get_json(url: str) -> dict[str, Any] | None:
    try:
        response = httpx.get(url, timeout=1.6)
    except Exception as exc:
        return {"_error": type(exc).__name__}
    if not str(response.headers.get("content-type", "")).startswith("application/json"):
        return {"_http": f"HTTP {response.status_code}"}
    try:
        return response.json()
    except Exception:
        return {"_http": "unparseable JSON"}


def _tcp_open(url: str) -> bool:
    import socket

    host, port = config_mod._host_port(url, 8123)
    with socket.socket() as probe:
        probe.settimeout(1.0)
        return probe.connect_ex((host, port)) == 0


def services_block(cfg: config_mod.Config) -> None:
    """Identity-checked status: a 2xx on a port only counts if *this* build answers on it."""
    st.sidebar.markdown("### Services")
    vendor = _get_json(cfg.vendor_url + "/health")
    buyer = _get_json(cfg.buyer_url + "/health")

    for label, body, marker in ((VENDOR, vendor, "consortium-vendor-agent"),
                                (BUYER, buyer, "consortium-buyer-agent")):
        if body and body.get("service") == marker:
            extra = f" · model {body.get('model')}" if body.get("model") else ""
            busy = " · busy" if body.get("busy") else ""
            _dot("#2ecc8f", f"{label} A2A up ({marker}{extra}{busy})")
        elif body and "_error" in body:
            _dot("#ff5c7a", f"{label} A2A down — {body['_error']} at {cfg.vendor_url if label == VENDOR else cfg.buyer_url}")
        elif body and body.get("service"):
            _dot("#f0a92c", f"{label} port answered by {body.get('service')} — not this build")
        else:
            _dot("#ff5c7a", f"{label} A2A unreachable")

    tools = (vendor or {}).get("mcp_tools")
    if tools:
        _dot("#2ecc8f", f"MCP data plane up · {', '.join(tools)} (discovered by {VENDOR})")
    elif _tcp_open(cfg.mcp_url):
        _dot("#f0a92c", f"MCP port open at {cfg.mcp_url} but {VENDOR} discovered no tools")
    else:
        _dot("#ff5c7a", f"MCP data plane down at {cfg.mcp_url}")


# --------------------------------------------------------------------------- run inputs


def run_controls(cfg: config_mod.Config) -> None:
    question = st.text_input(
        "Business question BuyerCorp must negotiate to answer",
        key="question",
        value=st.session_state.get(
            "question", "average order value by region for Q3 2025, no personal data"
        ),
        placeholder="e.g. average order value by region for Q3 2025, no personal data",
    )
    col1, col2, col3 = st.columns([1.5, 1.5, 2])
    if col1.button("Start negotiation", type="primary", width='stretch'):
        _post(cfg.buyer_url + "/runs", {"question": question}, "started run")
    if col2.button("Request out-of-scope field", width='stretch'):
        _post(cfg.buyer_url + "/probes/out-of-scope", {}, "probe queued")
    col3.markdown(
        "<div style='padding-top:1.35rem'><code>customer_email</code> is the column VendorCorp "
        "will not grant. The probe calls its MCP tool directly; the refusal text in the timeline "
        "is the server's, not this page's.</div>",
        unsafe_allow_html=True,
    )


def _post(url: str, payload: dict[str, Any], success: str) -> None:
    try:
        response = httpx.post(url, json=payload, timeout=8.0)
    except Exception as exc:
        st.error(f"{url} — {type(exc).__name__}: {exc}")
        return
    if response.status_code in (200, 202):
        st.toast(f"{success}: {json.dumps(response.json())}")
    else:
        st.error(f"HTTP {response.status_code}: {response.text[:400]}")


# ----------------------------------------------------------------------------- timeline


@st.fragment(run_every=1.5)
def timeline_fragment(events: list[dict[str, Any]], state: str) -> None:
    container = st.container(height=620, border=True)
    with container:
        if not events:
            st.markdown(
                "<div class='cq-empty'><b>No messages yet.</b><br><br>The timeline renders only "
                "what <code>data/events.jsonl</code> contains. Start <code>mcp_server.py</code>, "
                "<code>vendor_agent.py</code> and <code>buyer_agent.py</code>, then post a "
                "business question above — every A2A message and every MCP call will land here "
                "as it happens.</div>",
                unsafe_allow_html=True,
            )
            return
        left, right = st.columns(2, gap="medium")
        left.markdown(f"**{BUYER}**")
        right.markdown(f"**{VENDOR}**")
        columns = {"buyer": left, "vendor": right}
        for event in events:
            if event.get("kind") == "corrupt":
                left.error(event.get("text"))
                continue
            side = side_of(event)
            intent, why, payload = payload_of(event)
            colour, chip = INTENT_STYLE.get(intent, INTENT_STYLE["other"])
            title = _title_for(event, side)
            meta = _meta_for(event)
            body = card_html(side=side, intent=intent, title=title, why=why, payload=payload,
                             meta=meta, color=colour, chip=chip)
            columns[side].markdown(f"<div class='cq-tl'>{body}</div>", unsafe_allow_html=True)


def _title_for(event: dict[str, Any], side: str) -> str:
    org = VENDOR if side == "vendor" else BUYER
    kind = event.get("kind")
    actor = (event.get("actor") or "").lower()
    if kind == "inbound":
        return f"{org} · received"
    if kind == "outbound":
        return f"{org} · sending reply"
    if kind == "send":
        return f"{org} · sending"
    if kind == "response":
        return f"{org} · replied over A2A"
    if kind == "call" and event.get("plane") == "mcp":
        return f"{BUYER} → MCP · {event.get('tool')}"
    if kind == "result":
        return f"MCP · {event.get('tool')} result"
    if kind == "refusal":
        return f"MCP · {event.get('tool')} refused"
    if kind == "agent-card":
        return f"{BUYER} discovered {org}"
    if event.get("plane") == "model":
        return f"{org} · model ({html.escape(str(event.get('model'))[:22])})"
    if kind == "grant":
        return f"{org} · contract granted"
    if kind == "start":
        return f"{org} · run started"
    if kind in ("listening",):
        return f"{org} · service up"
    if kind == "call":
        return f"{org} · model call"
    return f"{org} · {kind}"


def _meta_for(event: dict[str, Any]) -> str:
    bits = [f"#{event.get('seq')}", str(event.get("ts", ""))[11:23], event.get("plane")]
    if event.get("context_id"):
        bits.append(f"ctx={str(event['context_id'])[:24]}")
    if event.get("contract_id"):
        bits.append(f"contract={event['contract_id']}")
    if event.get("a2a_task_id"):
        bits.append(f"task={str(event['a2a_task_id'])[:18]}")
    if event.get("run_id"):
        bits.append(f"run={event['run_id']}")
    return " · ".join(str(b) for b in bits if b)


# --------------------------------------------------------------------------- panels


def scope_panel(events: list[dict[str, Any]], contract: dict[str, Any] | None) -> None:
    st.markdown("**Scope granted vs refused**")
    asked: dict[str, list[str]] = {}
    for event in events:
        message = event.get("message")
        if not isinstance(message, dict):
            continue
        for column in (message.get("scope") or {}).get("columns", []) or []:
            asked.setdefault(column, []).append(f'{message["from"]}:{message["intent"]}')

    granted = set((contract or {}).get("scope", {}).get("columns", []))
    refused_rows: list[dict[str, str]] = []
    for event in events:
        if event.get("kind") != "refusal":
            continue
        args = event.get("arguments") or {}
        referenced = set(args.get("filters") or {})
        if args.get("group_by"):
            referenced.add(args["group_by"])
        column = (args.get("aggregate") or {}).get("column")
        if column and column != "*":
            referenced.add(column)
        for column in sorted(referenced - granted):
            refused_rows.append(
                {"column": column, "where": str(event.get("tool")), "reason": str(event.get("reason"))[:400]}
            )

    rows = []
    for column in sorted(set(asked) | granted):
        rows.append(
            {
                "column": column,
                "asked by": ", ".join(asked.get(column, [])) or "—",
                "granted": "yes" if column in granted else "no",
            }
        )
    st.dataframe(rows, width='stretch', hide_index=True)
    if refused_rows:
        for row in refused_rows:
            st.error(f"{row['column']} — refused by {row['where']}: {row['reason']}")
    elif granted:
        st.caption("No MCP refusal recorded in this log.")
    else:
        st.caption("Nothing granted yet.")


def contract_panel(contract: dict[str, Any] | None) -> None:
    st.markdown("**Agreed contract**")
    if not contract:
        st.caption("No contract has been published. It appears here once VendorCorp grants one.")
        return
    st.json(contract, expanded=True)


def answer_panel(events: list[dict[str, Any]]) -> None:
    st.markdown("**Computed answer**")
    for event in reversed(events):
        if event.get("plane") == "run" and event.get("kind") == "answer" and event.get("result"):
            result = event["result"]
            st.caption(
                f"{result.get('engine')} · {result.get('rows_matched', '?')} of "
                f"{result.get('rows_in_dataset', '?')} rows · aggregate {result.get('aggregate')}"
                f" · contract {result.get('contract_id')}"
            )
            st.json({"arguments": event.get("arguments"), "filters": result.get("filters"),
                     "group_by": result.get("group_by")}, expanded=False)
            st.dataframe(result.get("results") or [], width='stretch', hide_index=True)
            return
        if event.get("plane") == "run" and event.get("kind") == "refused":
            st.error(
                "VendorCorp's MCP server refused to answer:\n\n```\n"
                f"{event.get('reason')}\n```"
            )
            return
    st.caption("No answer has been produced yet.")


# ------------------------------------------------------------------------------- main


def main() -> None:
    cfg = config_mod.load()
    st.title("⇄ Consortium")
    st.caption(
        f"Two organisations, one aggregate. **{BUYER}** needs a statistic only **{VENDOR}** can "
        "compute. They negotiate scope over A2A, then the query runs through VendorCorp's MCP "
        "tools — and the MCP server, not the model, decides what is allowed."
    )

    sidebar()
    run_controls(cfg)

    events = read_all()
    state, detail = derive_state(events)
    colours = {"idle": "#8f9bb3", "negotiating": "#4c8dff", "executing": "#f0a92c",
               "complete": "#2ecc8f", "error": "#ff5c7a"}
    run_label = active_run().get("run_id")
    suffix = f" · run {run_label}" if run_label else ""
    st.markdown(
        f"<div class='cq-state'><span class='cq-dot' style='background:{colours[state]};'></span>"
        f"<b style='color:{colours[state]};text-transform:uppercase;letter-spacing:.08em;"
        f"font-size:.78rem;'>{state}</b><span style='color:#93a0b8;font-size:.8rem;'>"
        f"{html.escape(detail)}</span><span style='margin-left:auto;font-size:.7rem;color:#77839a;'>"
        f"{len(events)} events{suffix}</span></div>",
        unsafe_allow_html=True,
    )

    shown = events
    if st.session_state.get("timeline_mode", "All events") == "Messages and tools only":
        shown = [e for e in events if e.get("plane") != "model" and e.get("kind") not in NOISE_KINDS]
    timeline_fragment(shown, state)
    if state in ("negotiating", "executing"):
        st.caption("Auto-refreshing every 1.5 s while the run is live.")

    st.markdown("---")
    contract = contract_mod.current()
    col_a, col_b, col_c = st.columns([1.15, 1, 1.25], gap="large")
    with col_a:
        scope_panel(events, contract)
    with col_b:
        contract_panel(contract)
    with col_c:
        answer_panel(events)


main()
