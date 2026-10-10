"""Runtime configuration: model provider settings + service endpoints, persisted to `.env`."""

from __future__ import annotations

import json
import os
import re
from dataclasses import asdict, dataclass, replace
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ENV_PATH = ROOT / ".env"

DEFAULT_BASE_URL = "https://api.particle.ai/v1"
DEFAULT_MODEL = "deepseek-v4.1-flash"

# Provider presets offered in the observer sidebar. `model` is only a suggestion; the
# field stays editable because what is installed locally differs per machine.
PRESETS: dict[str, dict[str, str]] = {
    "Particle.ai": {
        "base_url": "https://api.particle.ai/v1",
        "api_key": "",
        "model": "deepseek-v4.1-flash",
        "hint": "OpenAI-compatible. Needs a key. Only deepseek-v4.1-flash and glm5.3flash resolve on it.",
    },
    "LM Studio": {
        "base_url": "http://localhost:1234/v1",
        "api_key": "lm-studio",
        "model": "",
        "hint": "Local. Leave the key as anything; LM Studio ignores it. Pick a loaded model id.",
    },
    "Ollama": {
        "base_url": "http://localhost:11434/v1",
        "api_key": "ollama",
        "model": "llama3.1",
        "hint": "Local. Key is ignored.",
    },
    "Gemini": {
        "base_url": "https://generativelanguage.googleapis.com/v1beta/openai/",
        "api_key": "",
        "model": "gemini-2.5-flash",
        "hint": "Google AI Studio key, OpenAI-compatible surface.",
    },
}

_KEYS = ("base_url", "api_key", "model", "reasoning_effort", "vendor_url", "buyer_url", "mcp_url")
_ENV_VARS = {
    "base_url": "OPENAI_BASE_URL",
    "api_key": "OPENAI_API_KEY",
    "model": "OPENAI_MODEL",
    "reasoning_effort": "OPENAI_REASONING_EFFORT",
    "vendor_url": "VENDOR_A2A_URL",
    "buyer_url": "BUYER_A2A_URL",
    "mcp_url": "VENDOR_MCP_URL",
}
_DEFAULTS = {
    "base_url": DEFAULT_BASE_URL,
    "api_key": "",
    "model": DEFAULT_MODEL,
    "reasoning_effort": "none",
    "vendor_url": "http://127.0.0.1:8101",
    "buyer_url": "http://127.0.0.1:8102",
    "mcp_url": "http://127.0.0.1:8123/mcp",
}


@dataclass(frozen=True)
class Config:
    base_url: str = DEFAULT_BASE_URL
    api_key: str = ""
    model: str = DEFAULT_MODEL
    reasoning_effort: str = "none"
    vendor_url: str = "http://127.0.0.1:8101"
    buyer_url: str = "http://127.0.0.1:8102"
    mcp_url: str = "http://127.0.0.1:8123/mcp"

    @property
    def vendor_host(self) -> tuple[str, int]:
        return _host_port(self.vendor_url, 8101)

    @property
    def buyer_host(self) -> tuple[str, int]:
        return _host_port(self.buyer_url, 8102)

    @property
    def mcp_host(self) -> tuple[str, int]:
        return _host_port(self.mcp_url, 8123)

    def with_overrides(self, **values: str) -> Config:
        clean = {k: v for k, v in values.items() if k in _KEYS and v is not None}
        return replace(self, **clean)

    def redacted(self) -> dict[str, str]:
        data = asdict(self)
        data["api_key"] = mask(self.api_key)
        return data


def mask(secret: str) -> str:
    if not secret:
        return "(unset)"
    if len(secret) <= 8:
        return "*" * len(secret)
    return f"{secret[:4]}…{secret[-4:]} ({len(secret)} chars)"


def _host_port(url: str, fallback: int) -> tuple[str, int]:
    match = re.match(r"https?://([^:/]+)(?::(\d+))?", url)
    if not match:
        return "127.0.0.1", fallback
    return match.group(1), int(match.group(2) or fallback)


def parse_env_file(path: Path | None = None) -> dict[str, str]:
    """Minimal `.env` reader (KEY=value, optional quotes, `#` comments).

    `path` resolves at call time, so tests (and anything else) can redirect `ENV_PATH`.
    """
    target = path or ENV_PATH
    values: dict[str, str] = {}
    if not target.exists():
        return values
    for raw in target.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1]:
            if value[0] == '"':
                try:  # `_quote` escapes exactly the two characters a JSON string escapes.
                    value = json.loads(value)
                except Exception:
                    value = value[1:-1]
            elif value[0] == "'":
                value = value[1:-1]
        values[key.strip()] = value
    return values


def load() -> Config:
    """`.env` first, then a real environment variable of the same name, then the spec default.

    `.env` wins over the ambient environment on purpose: this machine exports `OPENAI_API_KEY`
    for other projects, and a globally exported key must not silently override what the
    observer sidebar was configured with.
    """
    file_values = parse_env_file()
    resolved: dict[str, str] = {}
    for key in _KEYS:
        var = _ENV_VARS[key]
        value = file_values.get(var) or os.environ.get(var)
        resolved[key] = value if value not in (None, "") else _DEFAULTS[key]
    # A key left as the placeholder in .env.example is not a key.
    if resolved["api_key"].startswith("paste-your"):
        resolved["api_key"] = ""
    return Config(**resolved)


def save(cfg: Config) -> Path:
    """Write the known keys to `.env`, preserving comments and any unrelated lines."""
    existing = ENV_PATH.read_text(encoding="utf-8").splitlines() if ENV_PATH.exists() else []
    field_for = {var: field for field, var in _ENV_VARS.items()}
    seen: set[str] = set()
    lines: list[str] = []
    for line in existing:
        var = line.split("=", 1)[0].strip() if "=" in line and not line.strip().startswith("#") else None
        if var in field_for:
            lines.append(f"{var}={_quote(getattr(cfg, field_for[var]))}")
            seen.add(var)
        else:
            lines.append(line)
    for field, var in _ENV_VARS.items():
        if var not in seen:
            lines.append(f"{var}={_quote(getattr(cfg, field))}")
    ENV_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")
    ENV_PATH.chmod(0o600)
    return ENV_PATH


def _quote(value: str) -> str:
    if value == "" or re.search(r"""[\s#"'$`\\]""", value):
        return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'
    return value
