"""Environment configuration for the agent backend.

The LLM endpoint is a *deployment-time* choice: nothing is hardcoded here.
Everything comes from ``VAP_LLM_*`` environment variables and works with any
OpenAI-compatible ``/chat/completions`` endpoint:

- ``VAP_LLM_BASE_URL`` — e.g. ``https://token-plan-sgp.xiaomimimo.com/v1``
  (no provider is assumed; the value is used verbatim as the API base).
- ``VAP_LLM_API_KEY`` — bearer key; never logged, never echoed anywhere.
- ``VAP_LLM_MODEL`` — model name sent in the request body.
- ``VAP_LLM_REASONING_EFFORT`` — optional; passed through only when it is one
  of ``none`` / ``low`` / ``medium`` / ``high`` (stricter providers reject
  anything else; a bad value is ignored rather than breaking the endpoint).

The API key is read here and handed to the HTTP client; it never appears in
``/healthz``, logs, error messages or conversation state.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Mapping, Optional

VERSION = "0.1.0"

_ALLOWED_REASONING_EFFORT = ("none", "low", "medium", "high")


@dataclass(frozen=True)
class Settings:
    """Runtime settings; built from the environment via :meth:`from_env`."""

    llm_base_url: Optional[str] = None
    llm_api_key: Optional[str] = None
    llm_model: Optional[str] = None
    llm_reasoning_effort: Optional[str] = None
    llm_timeout: float = 60.0
    keepalive_seconds: float = 15.0
    verify_timeout: float = 10.0
    tool_timeout: float = 30.0
    poll_interval: float = 0.25

    @classmethod
    def from_env(cls, env: Optional[Mapping[str, str]] = None) -> "Settings":
        env = os.environ if env is None else env

        def _get(name: str) -> Optional[str]:
            value = (env.get(name) or "").strip()
            return value or None

        effort = (_get("VAP_LLM_REASONING_EFFORT") or "").lower() or None
        if effort not in _ALLOWED_REASONING_EFFORT:
            effort = None  # invalid values are dropped, never sent

        def _float(name: str, default: float) -> float:
            raw = _get(name)
            try:
                return float(raw) if raw else default
            except ValueError:
                return default

        return cls(
            llm_base_url=_get("VAP_LLM_BASE_URL"),
            llm_api_key=_get("VAP_LLM_API_KEY"),
            llm_model=_get("VAP_LLM_MODEL"),
            llm_reasoning_effort=effort,
            llm_timeout=_float("VAP_LLM_TIMEOUT", 60.0),
            keepalive_seconds=_float("VAP_KEEPALIVE_SECONDS", 15.0),
            verify_timeout=_float("VAP_VERIFY_TIMEOUT", 10.0),
            tool_timeout=_float("VAP_TOOL_TIMEOUT", 30.0),
            poll_interval=_float("VAP_POLL_INTERVAL", 0.25),
        )

    @property
    def llm_configured(self) -> bool:
        """A usable endpoint needs at least a base URL and a model name."""
        return bool(self.llm_base_url and self.llm_model)

    def healthz(self) -> dict:
        """``/healthz`` payload — never echoes keys or tokens."""
        return {"status": "ok", "llm_configured": self.llm_configured,
                "version": VERSION}
