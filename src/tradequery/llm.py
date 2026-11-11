"""One small wrapper around the language model provider, so models can be swapped and compared.

Providers:
  gemini  Google's Gemini API (google-genai SDK). Needs GEMINI_API_KEY; GEMINI_MODEL
          picks the model, otherwise the newest "flash-lite" model the key can use
          (the largest free daily quota) is chosen from the model list.
  fake    Scripted replies, for tests: no network, no quota.

What is sent to the provider: the schema description, the account's date range,
symbols and magic numbers, the question, the previous spec, and (for the
explanation) the result table. Never trade comments, never credentials. Note that
on Gemini's free tier Google may use prompts to improve its products.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field


class LLMError(RuntimeError):
    """The provider could not answer (missing key, network, bad reply)."""


class QuotaError(LLMError):
    """The provider's rate limit or daily quota was reached."""


@dataclass
class Usage:
    requests: int = 0
    input_tokens: int = 0
    output_tokens: int = 0

    def add(self, other: "Usage") -> None:
        self.requests += other.requests
        self.input_tokens += other.input_tokens
        self.output_tokens += other.output_tokens


class Provider:
    name = "provider"
    model = ""

    def generate(self, system: str, prompt: str, *, json_mode: bool = False) -> tuple[str, Usage]:
        raise NotImplementedError


@dataclass
class FakeProvider(Provider):
    """Returns the scripted replies in order and records every prompt (tests)."""

    replies: list[str] = field(default_factory=list)
    calls: list[tuple[str, str, bool]] = field(default_factory=list)
    name: str = "fake"
    model: str = "fake-model"

    def generate(self, system: str, prompt: str, *, json_mode: bool = False) -> tuple[str, Usage]:
        self.calls.append((system, prompt, json_mode))
        if not self.replies:
            raise LLMError("the fake provider has no reply left")
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply, Usage(1, len(system + prompt) // 4, len(reply) // 4)


def _version_key(name: str) -> tuple:
    return tuple(int(n) for n in re.findall(r"\d+", name)) or (0,)


def choose_model(names: list[str]) -> str | None:
    """The newest flash-lite model, else the newest flash model (stable names before previews)."""
    usable = [n.removeprefix("models/") for n in names]
    for family in ("flash-lite", "flash"):
        candidates = [n for n in usable if family in n and (family != "flash" or "lite" not in n)
                      and not any(w in n for w in ("tts", "image", "audio", "live", "embedding", "thinking"))]
        if candidates:
            stable = [n for n in candidates if "preview" not in n and "exp" not in n] or candidates
            return max(stable, key=lambda n: (_version_key(n), -len(n)))
    return None


class GeminiProvider(Provider):
    name = "gemini"
    # pauses before each retry on a server error (503 "high demand" usually clears within seconds)
    retry_pauses = (1.0, 3.0)

    def __init__(self, api_key: str, model: str | None = None, *, timeout_seconds: float = 60.0, sleep=None):
        import time

        self._sleep = sleep or time.sleep
        try:
            from google import genai
            from google.genai import errors, types
        except ImportError:
            raise LLMError("the google-genai package is missing: install the llm extra (pip install '.[llm]')") from None
        self._types, self._errors = types, errors
        self._client = genai.Client(api_key=api_key, http_options=types.HttpOptions(timeout=int(timeout_seconds * 1000)))
        self.model = model or self._auto_model()

    def list_models(self) -> list[str]:
        try:
            return sorted(m.name for m in self._client.models.list()
                          if not m.supported_actions or "generateContent" in m.supported_actions)
        except self._errors.APIError as error:
            raise self._translate(error) from None

    def _auto_model(self) -> str:
        chosen = choose_model(self.list_models())
        if not chosen:
            raise LLMError("no Gemini text model is available to this key; set GEMINI_MODEL in .env")
        return chosen

    def _translate(self, error) -> LLMError:
        code = getattr(error, "code", None)
        message = getattr(error, "message", None) or str(error)
        if code == 429:
            return QuotaError(f"Gemini rate limit or daily quota reached ({message}). Free-tier quotas reset at "
                              "midnight Pacific time.")
        if code in (401, 403):
            return LLMError(f"Gemini rejected the API key ({message}). Check GEMINI_API_KEY in .env.")
        return LLMError(f"Gemini error {code}: {message}")

    def generate(self, system: str, prompt: str, *, json_mode: bool = False) -> tuple[str, Usage]:
        config = self._types.GenerateContentConfig(
            system_instruction=system,
            temperature=0.0,
            max_output_tokens=4096,
            response_mime_type="application/json" if json_mode else None,
        )
        response = None
        for attempt in range(len(self.retry_pauses) + 1):
            try:
                response = self._client.models.generate_content(model=self.model, contents=prompt, config=config)
                break
            except self._errors.APIError as error:
                # 5xx ("high demand", overloaded) is usually brief: one retry after a short pause
                if attempt < len(self.retry_pauses) and (getattr(error, "code", 0) or 0) >= 500:
                    self._sleep(self.retry_pauses[attempt])
                    continue
                raise self._translate(error) from None
        text = response.text
        if not text:
            feedback = getattr(response, "prompt_feedback", None)
            raise LLMError(f"Gemini returned no text{f' ({feedback})' if feedback else ''}")
        meta = response.usage_metadata
        usage = Usage(1, getattr(meta, "prompt_token_count", 0) or 0,
                      (getattr(meta, "candidates_token_count", 0) or 0) + (getattr(meta, "thoughts_token_count", 0) or 0))
        return text, usage


def provider_from_env() -> Provider:
    """The configured provider: Gemini with GEMINI_API_KEY (and optional GEMINI_MODEL)."""
    key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not key:
        raise LLMError("No GEMINI_API_KEY. Add a line GEMINI_API_KEY=... to .env (see .env.example).")
    return GeminiProvider(key, os.environ.get("GEMINI_MODEL", "").strip() or None)
