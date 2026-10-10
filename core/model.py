"""Model access: an OpenAI-compatible `/chat/completions` endpoint, wrapped by Agent Framework.

Both agents build their Agent from here, so provider switching is a config change only.

Two provider quirks are handled explicitly (each one silently produced empty replies in
testing, so they are surfaced as events rather than papered over):

* `api.particle.ai` returns `metadata` as an **object** on some replies, while the OpenAI SDK
  types it as `str | None`; the SDK then rejects the whole response. A `response` event hook
  rewrites that field to null before the SDK parses it.
* `response_format: {"type": "json_object"}` is rejected with HTTP 400 by LM Studio. If the
  first JSON-mode call fails that way, JSON mode is turned off for the rest of the run and the
  reply is parsed tolerantly instead.
"""

from __future__ import annotations

import json
import re
import time
from typing import Any

import httpx
from agent_framework import Agent
from agent_framework.openai import OpenAIChatCompletionClient
from openai import AsyncOpenAI

from .config import Config
from .events import emit

#: Flipped once a provider proves it does not support `response_format: json_object`.
JSON_MODE_SUPPORTED = True

_FENCE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$", re.IGNORECASE)


class ProtocolError(RuntimeError):
    """The model did not produce a usable negotiation message after the mandated retry."""


async def _particle_metadata_fixup(response: httpx.Response) -> None:
    """Null out a non-string top-level `metadata` so the OpenAI SDK can parse the reply.

    Must be a coroutine: httpx awaits response event hooks.
    """
    if "application/json" not in response.headers.get("content-type", ""):
        return  # never touch an SSE stream
    try:
        await response.aread()
        data = json.loads(response.content)
    except Exception:
        return
    if not isinstance(data, dict):
        return
    if isinstance(data.get("metadata"), (dict, list)):
        data["metadata"] = None
        body = json.dumps(data).encode("utf-8")
        response._content = body  # noqa: SLF001 - httpx exposes no setter
        response.headers["content-length"] = str(len(body))


def build_agent(cfg: Config, *, name: str, instructions: str, temperature: float = 0.2) -> Agent:
    """An Agent Framework agent on the configured OpenAI-compatible endpoint."""
    http_client = httpx.AsyncClient(
        event_hooks={"response": [_particle_metadata_fixup]},
        timeout=httpx.Timeout(120.0, connect=15.0),
    )
    client = OpenAIChatCompletionClient(
        model=cfg.model,
        api_key=cfg.api_key or "not-needed",
        base_url=cfg.base_url,
        async_client=AsyncOpenAI(
            base_url=cfg.base_url,
            api_key=cfg.api_key or "not-needed",
            http_client=http_client,
            max_retries=1,
        ),
    )
    return Agent(client=client, name=name, instructions=instructions, id=name.lower().replace(" ", "-"))


_AGENTS: dict[str, tuple[tuple, Agent]] = {}


def get_agent(cfg: Config, *, name: str, instructions: str) -> Agent:
    """A per-process agent, rebuilt only when the provider settings actually change.

    Lets the observer's sidebar swap base URL / key / model and have the very next run use it,
    without restarting either agent process.
    """
    signature = (cfg.base_url, cfg.api_key, cfg.model, cfg.reasoning_effort)
    cached = _AGENTS.get(name)
    if cached and cached[0] == signature:
        return cached[1]
    agent = build_agent(cfg, name=name, instructions=instructions)
    _AGENTS[name] = (signature, agent)
    if cached is not None:
        emit("model", name.lower(), "client-rebuilt", base_url=cfg.base_url, model=cfg.model,
             note="provider settings changed; a new client was built for subsequent calls")
    return agent


def _looks_like_json_mode_rejection(exc: Exception) -> bool:
    text = str(exc).lower()
    return "response_format" in text and ("json_object" in text or "must be" in text or "400" in text)


def _options(cfg: Config, *, json_mode: bool, max_tokens: int) -> dict[str, Any]:
    opts: dict[str, Any] = {"max_tokens": max_tokens, "temperature": 0.2}
    if cfg.reasoning_effort not in ("", "off"):
        # Without this, deepseek-v4.1-flash spends the entire completion budget on
        # hidden reasoning and returns an empty `content` string.
        opts["reasoning_effort"] = cfg.reasoning_effort
    if json_mode:
        opts["response_format"] = {"type": "json_object"}
    return opts


async def ask_json(
    cfg: Config,
    agent: Agent,
    prompt: str,
    *,
    side: str,
    turn: str,
    max_tokens: int = 900,
) -> dict[str, Any]:
    """One model round-trip that must yield a parseable JSON object.

    Parse failure is retried exactly once (per the negotiation schema rule). A provider that
    rejects `response_format` is downgraded to prompt-only JSON mode and the downgrade is logged.
    """
    global JSON_MODE_SUPPORTED
    started = time.monotonic()
    attempts = 0
    text = prompt
    last_error = ""

    while True:
        attempts += 1
        used_json_mode = JSON_MODE_SUPPORTED
        try:
            response = await agent.run(
                text,
                options=_options(cfg, json_mode=used_json_mode, max_tokens=max_tokens),
            )
        except Exception as exc:
            if used_json_mode and attempts == 1 and _looks_like_json_mode_rejection(exc):
                JSON_MODE_SUPPORTED = False
                emit(
                    "model",
                    side,
                    "capability-fallback",
                    turn=turn,
                    detail=f"{type(exc).__name__}: {_short(exc)}",
                    note="provider rejected response_format=json_object; retrying without JSON mode",
                )
                continue
            last_error = f"{type(exc).__name__}: {_short(exc)}"
            emit("model", side, "error", turn=turn, attempts=attempts, error=last_error)
            raise ProtocolError(f"{side} model call failed: {last_error}") from exc

        raw = _reply_text(response)
        try:
            parsed = extract_json(raw)
        except ValueError as exc:
            last_error = f"{exc}"
            if attempts > 1:
                emit("model", side, "parse-failed", turn=turn, attempts=attempts, error=last_error, raw=raw[:800])
                raise ProtocolError(f"{side} reply was not valid JSON after one retry: {last_error}") from exc
            emit("model", side, "parse-retry", turn=turn, attempts=attempts, error=last_error, raw=raw[:400])
            text = (
                prompt
                + "\n\nYour previous reply could not be parsed as a single JSON object"
                + f" ({last_error}). Reply again with ONLY the JSON object: no prose, no code fence."
            )
            continue

        usage = getattr(response, "usage_details", None)
        emit(
            "model",
            side,
            "call",
            turn=turn,
            model=cfg.model,
            base_url=cfg.base_url,
            json_mode=used_json_mode,
            attempts=attempts,
            latency_ms=int((time.monotonic() - started) * 1000),
            usage=dict(usage) if usage else None,
        )
        return parsed


async def ask_text(cfg: Config, agent: Agent, prompt: str, *, side: str, turn: str) -> str:
    """One model round-trip with a free-text reply (used by the peer-facing A2A endpoints)."""
    started = time.monotonic()
    try:
        response = await agent.run(prompt, options=_options(cfg, json_mode=False, max_tokens=400))
    except Exception as exc:
        last_error = f"{type(exc).__name__}: {_short(exc)}"
        emit("model", side, "error", turn=turn, error=last_error)
        raise ProtocolError(f"{side} model call failed: {last_error}") from exc
    text = _reply_text(response).strip()
    usage = getattr(response, "usage_details", None)
    emit("model", side, "call", turn=turn, model=cfg.model, base_url=cfg.base_url, json_mode=False,
         latency_ms=int((time.monotonic() - started) * 1000), usage=dict(usage) if usage else None)
    return text


def _reply_text(response: Any) -> str:
    messages = getattr(response, "messages", None) or []
    for message in reversed(list(messages)):
        text = getattr(message, "text", None)
        if text:
            return text if isinstance(text, str) else str(text)
    return str(getattr(response, "text", "") or "")


def extract_json(text: str) -> dict[str, Any]:
    """Parse a JSON object out of a model reply, tolerating prose and ``` fences."""
    stripped = _FENCE.sub("", text or "").strip()
    if not stripped:
        raise ValueError("empty reply")
    try:
        obj = json.loads(stripped)
    except json.JSONDecodeError:
        start, end = stripped.find("{"), stripped.rfind("}")
        if start == -1 or end <= start:
            raise ValueError(f"no JSON object found in {len(stripped)} chars") from None
        try:
            obj = json.loads(stripped[start : end + 1])
        except json.JSONDecodeError as exc:
            raise ValueError(f"invalid JSON ({exc.msg} at line {exc.lineno})") from None
    if not isinstance(obj, dict):
        raise ValueError(f"expected a JSON object, got {type(obj).__name__}")
    return obj


def _short(exc: Exception, limit: int = 400) -> str:
    return " ".join(str(exc).split())[:limit]


async def test_connection(cfg: Config) -> dict[str, Any]:
    """One real `/chat/completions` call with the current settings. Returns the truth, not a guess."""
    url = cfg.base_url.rstrip("/") + "/chat/completions"
    body = {
        "model": cfg.model,
        "messages": [{"role": "user", "content": "Reply with exactly: ok"}],
        "max_completion_tokens": 60,
    }
    if cfg.reasoning_effort not in ("", "off"):
        body["reasoning_effort"] = cfg.reasoning_effort
    headers = {"Authorization": f"Bearer {cfg.api_key}"} if cfg.api_key else {}
    started = time.monotonic()
    out: dict[str, Any] = {"url": url, "model": cfg.model}
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(45.0, connect=10.0)) as client:
            response = await client.post(url, json=body, headers=headers)
    except Exception as exc:
        out.update(ok=False, error=f"{type(exc).__name__}: {_short(exc)}", status=None)
        return out
    elapsed = int((time.monotonic() - started) * 1000)
    out["status"] = response.status_code
    out["latency_ms"] = elapsed
    if response.status_code >= 400:
        out["ok"] = False
        out["error"] = f"HTTP {response.status_code}: {_short(response.text, 500)}"
        return out
    try:
        payload = response.json()
    except Exception:
        out.update(ok=False, error=f"HTTP {response.status_code} but the body was not JSON: {_short(response.text)}")
        return out
    try:
        content = payload["choices"][0]["message"]["content"]
    except Exception:
        out.update(ok=False, error=f"no choices[0].message.content in reply: {json.dumps(payload)[:400]}")
        return out
    usage = payload.get("usage") or {}
    out.update(
        ok=bool(content),
        content=(content or "")[:200],
        usage=usage,
        **({"error": "the model replied but `content` was empty"} if not content else {}),
    )
    if not content and usage.get("completion_tokens_details", {}).get("reasoning_tokens"):
        out["hint"] = (
            "All completion tokens went to hidden reasoning. Set OPENAI_REASONING_EFFORT=none "
            "in the sidebar config."
        )
    return out
