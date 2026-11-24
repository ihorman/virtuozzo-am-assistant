"""OpenAI-compatible LLM client (env-configured, no hardcoded provider).

One method: ``complete(messages) -> str``. The endpoint, key and model come
from :class:`agent.config.Settings` (``VAP_LLM_*`` env vars). An ``httpx``
transport can be injected so tests never touch the network.

Design notes:
- Plain ``POST {base}/chat/completions`` with ``{"model", "messages", ...}``;
  no provider-specific fields are sent unless explicitly configured.
- Failures raise :class:`LLMError` with an HTTP status and a truncated body;
  the API key is never included in any exception message.
"""

from __future__ import annotations

from typing import Any, List, Mapping, Optional

import httpx

from .config import Settings


class LLMError(Exception):
    """The LLM endpoint failed (HTTP error or unusable response)."""

    def __init__(self, message: str, *, status: Optional[int] = None):
        super().__init__(message)
        self.status = status


class LLMNotConfigured(LLMError):
    """No endpoint configured (``VAP_LLM_BASE_URL`` / ``VAP_LLM_MODEL``)."""


class LLMClient:
    """Minimal chat-completions client for any OpenAI-compatible service."""

    def __init__(self, base_url: str, api_key: Optional[str], model: str, *,
                 timeout: float = 60.0, reasoning_effort: Optional[str] = None,
                 transport: Optional[httpx.AsyncBaseTransport] = None):
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout = timeout
        self.reasoning_effort = reasoning_effort
        self._api_key = api_key
        self._http = httpx.AsyncClient(timeout=timeout, transport=transport)

    @classmethod
    def from_settings(cls, settings: Settings, *,
                      transport: Optional[httpx.AsyncBaseTransport] = None
                      ) -> "LLMClient":
        if not settings.llm_configured:
            raise LLMNotConfigured(
                "no LLM endpoint configured: set VAP_LLM_BASE_URL and "
                "VAP_LLM_MODEL (and VAP_LLM_API_KEY if the endpoint needs it)")
        return cls(settings.llm_base_url or "", settings.llm_api_key,
                   settings.llm_model or "", timeout=settings.llm_timeout,
                   reasoning_effort=settings.llm_reasoning_effort,
                   transport=transport)

    async def aclose(self) -> None:
        await self._http.aclose()

    async def complete(self, messages: List[Mapping[str, str]], *,
                       temperature: float = 0.2) -> str:
        """Run one chat completion and return the assistant text."""
        body: dict[str, Any] = {
            "model": self.model,
            "messages": [dict(m) for m in messages],
            "temperature": temperature,
        }
        if self.reasoning_effort:
            body["reasoning_effort"] = self.reasoning_effort
        headers = {"Content-Type": "application/json"}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        try:
            response = await self._http.post(
                f"{self.base_url}/chat/completions", json=body,
                headers=headers)
        except httpx.TimeoutException as exc:
            raise LLMError(f"LLM request timed out after {self.timeout}s",
                           status=None) from exc
        except httpx.HTTPError as exc:
            raise LLMError(f"LLM endpoint unreachable: {exc}") from exc
        if response.status_code >= 400:
            raise LLMError(
                f"LLM endpoint returned HTTP {response.status_code}: "
                f"{response.text[:300]}", status=response.status_code)
        try:
            payload = response.json()
            content = payload["choices"][0]["message"]["content"]
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise LLMError("LLM endpoint returned an unusable response "
                           "(no choices[0].message.content)") from exc
        if not isinstance(content, str):
            raise LLMError("LLM response content is not text")
        return content
