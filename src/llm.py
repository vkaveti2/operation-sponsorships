"""One small interface over two model providers (Anthropic Claude, Google Gemini).

The rest of the code only calls LLMClient.generate(system, prompt) and handles the errors from
config.errors, so switching providers is a one-word change in .env.
"""
from __future__ import annotations

import logging
import subprocess
import sys
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any

import anthropic
import httpx

from config.errors import LLMAuthError, LLMError, LLMRefusal, LLMTransientError
from config.settings import Settings

log = logging.getLogger(__name__)

ANTHROPIC_MAX_TOKENS = 16000  # thinking tokens count toward this, so leave generous room
ANTHROPIC_EFFORT = "medium"
FALLBACK_BETA = "server-side-fallback-2026-07-01"
FALLBACK_MODELS = frozenset({"claude-fable-5-1", "claude-opus-5-5", "claude-opus-5", "claude-sonnet-5-5"})
EFFORT_MODEL_PREFIXES = (
    "claude-opus-5", "claude-opus-4-8", "claude-opus-4-7", "claude-opus-4-6", "claude-sonnet-5",
    "claude-sonnet-4-6", "claude-fable", "claude-mythos", "claude-haiku-5",
)
GEMINI_MAX_OUTPUT_TOKENS = 8192  # thinking tokens count here too
GEMINI_BLOCK_FINISH = frozenset({"SAFETY", "PROHIBITED_CONTENT", "BLOCKLIST", "SPII", "RECITATION", "IMAGE_SAFETY"})


@dataclass(frozen=True)
class LLMResult:
    text: str
    provider: str
    model: str
    input_tokens: int = 0
    output_tokens: int = 0


class LLMClient(ABC):
    provider = ""
    model = ""

    @abstractmethod
    def generate(self, system: str, prompt: str) -> LLMResult:
        """One request, one answer. Raises LLMAuthError / LLMTransientError / LLMRefusal / LLMError."""

    @abstractmethod
    def check(self) -> str:
        """Free check that the key works and the model name exists. Returns a one-line status."""


# --------------------------------------------------------------------------- Anthropic
def _anthropic_message(exc: Exception) -> str:
    return str(getattr(exc, "message", "") or exc)


def map_anthropic_error(exc: Exception, model: str) -> LLMError:
    message = _anthropic_message(exc)
    if isinstance(exc, anthropic.AuthenticationError):
        return LLMAuthError("Anthropic rejected ANTHROPIC_API_KEY. Create a new key at "
                            "https://platform.claude.com/settings/keys and run: python main.py configure")
    if isinstance(exc, anthropic.PermissionDeniedError):
        return LLMAuthError(f"This Anthropic key is not allowed to use {model} ({message}).")
    if isinstance(exc, anthropic.NotFoundError):
        return LLMAuthError(f"Anthropic has no model called '{model}'. Fix ANTHROPIC_MODEL in .env "
                            "(for example claude-opus-5-5 or claude-sonnet-5-5).")
    if isinstance(exc, anthropic.BadRequestError):
        if "credit balance" in message.lower():
            return LLMAuthError("The Anthropic account has no credit left. Add some at "
                                "https://platform.claude.com/settings/billing")
        return LLMError(f"Anthropic rejected the request: {message}")
    if isinstance(exc, (anthropic.RateLimitError, anthropic.OverloadedError, anthropic.ServiceUnavailableError,
                        anthropic.InternalServerError, anthropic.APITimeoutError, anthropic.APIConnectionError)):
        return LLMTransientError(f"Anthropic is busy or unreachable ({exc.__class__.__name__}). Try again in a minute.")
    status = int(getattr(exc, "status_code", 0) or 0)
    if status >= 500:
        return LLMTransientError(f"Anthropic had a server problem ({status}). Try again in a minute.")
    return LLMError(f"Anthropic error: {message}")


class AnthropicLLM(LLMClient):
    provider = "anthropic"

    def __init__(self, api_key: str, model: str, *, refusal_fallback: bool = True, client: Any = None,
                 max_tokens: int = ANTHROPIC_MAX_TOKENS):
        self.model = model
        self.max_tokens = max_tokens
        self._fallback_active = bool(refusal_fallback) and model in FALLBACK_MODELS
        self.client = client or anthropic.Anthropic(api_key=api_key, max_retries=3, timeout=180.0)

    def _request(self, system: str, prompt: str) -> dict[str, Any]:
        kwargs: dict[str, Any] = {
            "model": self.model,
            "max_tokens": self.max_tokens,
            "system": system,
            "messages": [{"role": "user", "content": prompt}],
        }
        if self.model.startswith(EFFORT_MODEL_PREFIXES):
            kwargs["output_config"] = {"effort": ANTHROPIC_EFFORT}
        return kwargs

    def _create(self, kwargs: dict[str, Any]) -> Any:
        if self._fallback_active:
            try:
                return self.client.beta.messages.create(betas=[FALLBACK_BETA], fallbacks="default", **kwargs)
            except anthropic.BadRequestError as exc:
                text = _anthropic_message(exc).lower()
                if "fallback" in text or "beta" in text:
                    log.warning("Anthropic did not accept the refusal-fallback option (%s). "
                                "Continuing without it.", _anthropic_message(exc))
                    self._fallback_active = False
                else:
                    raise
        return self.client.messages.create(**kwargs)

    def generate(self, system: str, prompt: str) -> LLMResult:
        try:
            response = self._create(self._request(system, prompt))
        except anthropic.APIError as exc:
            raise map_anthropic_error(exc, self.model) from exc

        if getattr(response, "stop_reason", None) == "refusal":
            details = getattr(response, "stop_details", None)
            category = getattr(details, "category", None)
            raise LLMRefusal("The model declined to write this email" + (f" (category: {category})." if category else "."))
        # If the request fell back to another model part-way, only the text written after the
        # switch is the answer; anything before it came from the model that declined.
        pieces: list[str] = []
        for block in getattr(response, "content", None) or []:
            kind = getattr(block, "type", "")
            if kind == "fallback":
                pieces.clear()
            elif kind == "text":
                pieces.append(getattr(block, "text", ""))
        text = "".join(pieces).strip()
        if getattr(response, "stop_reason", None) == "max_tokens":
            raise LLMError("The model ran out of room before finishing the email. Run it again.")
        if not text:
            raise LLMError("The model returned an empty answer.")
        usage = getattr(response, "usage", None)
        return LLMResult(
            text=text,
            provider=self.provider,
            model=str(getattr(response, "model", "") or self.model),
            input_tokens=int(getattr(usage, "input_tokens", 0) or 0),
            output_tokens=int(getattr(usage, "output_tokens", 0) or 0),
        )

    def check(self) -> str:
        try:
            self.client.models.retrieve(self.model)
        except anthropic.APIError as exc:
            raise map_anthropic_error(exc, self.model) from exc
        return f"Anthropic key accepted and model {self.model} exists"


# --------------------------------------------------------------------------- Gemini
def map_gemini_error(exc: Exception, model: str) -> LLMError:
    code = int(getattr(exc, "code", 0) or 0)
    message = str(getattr(exc, "message", "") or exc)
    low = message.lower()
    if code in (401, 403) or "api key not valid" in low or "api_key_invalid" in low:
        return LLMAuthError("Google rejected GEMINI_API_KEY. Create one at https://aistudio.google.com/apikey "
                            "and run: python main.py configure")
    if code == 404:
        return LLMAuthError(f"Gemini has no model called '{model}'. Fix GEMINI_MODEL in .env.")
    if code == 429 or code >= 500:
        return LLMTransientError(f"Gemini is rate limiting or busy ({code}). Try again in a minute.")
    return LLMError(f"Gemini error {code}: {message}")


class GeminiLLM(LLMClient):
    provider = "gemini"

    def __init__(self, api_key: str, model: str, *, client: Any = None,
                 max_output_tokens: int = GEMINI_MAX_OUTPUT_TOKENS, timeout_seconds: int = 90):
        self.model = model
        self.max_output_tokens = max_output_tokens
        if client is None:
            from google import genai
            from google.genai import types

            client = genai.Client(
                api_key=api_key,
                http_options=types.HttpOptions(
                    timeout=timeout_seconds * 1000,
                    retry_options=types.HttpRetryOptions(attempts=4, initial_delay=2.0, max_delay=30.0),
                ),
            )
        self.client = client

    def generate(self, system: str, prompt: str) -> LLMResult:
        from google.genai import errors, types

        config = types.GenerateContentConfig(system_instruction=system, max_output_tokens=self.max_output_tokens)
        try:
            response = self.client.models.generate_content(model=self.model, contents=prompt, config=config)
        except errors.APIError as exc:
            raise map_gemini_error(exc, self.model) from exc
        except (httpx.TimeoutException, httpx.NetworkError) as exc:
            raise LLMTransientError("Could not reach Gemini. Check your internet connection.") from exc

        feedback = getattr(response, "prompt_feedback", None)
        if feedback is not None and getattr(feedback, "block_reason", None):
            raise LLMRefusal(f"Gemini blocked the request ({getattr(feedback.block_reason, 'name', feedback.block_reason)}).")
        candidates = getattr(response, "candidates", None) or []
        if not candidates:
            raise LLMError("Gemini returned no answer.")
        reason = getattr(candidates[0], "finish_reason", None)
        reason_name = getattr(reason, "name", str(reason or ""))
        if reason_name in GEMINI_BLOCK_FINISH:
            raise LLMRefusal(f"Gemini stopped the answer ({reason_name}).")
        text = (response.text or "").strip()
        if reason_name == "MAX_TOKENS":
            raise LLMError("The model ran out of room before finishing the email. Run it again.")
        if not text:
            raise LLMError("Gemini returned an empty answer.")
        usage = getattr(response, "usage_metadata", None)
        return LLMResult(
            text=text,
            provider=self.provider,
            model=self.model,
            input_tokens=int(getattr(usage, "prompt_token_count", 0) or 0),
            output_tokens=int((getattr(usage, "candidates_token_count", 0) or 0)
                              + (getattr(usage, "thoughts_token_count", 0) or 0)),
        )

    def check(self) -> str:
        from google.genai import errors

        try:
            self.client.models.get(model=self.model)
        except errors.APIError as exc:
            raise map_gemini_error(exc, self.model) from exc
        except (httpx.TimeoutException, httpx.NetworkError) as exc:
            raise LLMTransientError("Could not reach Gemini. Check your internet connection.") from exc
        return f"Gemini key accepted and model {self.model} exists"


# --------------------------------------------------------------------------- factory
def build_llm(settings: Settings, *, client: Any = None) -> LLMClient:
    key = settings.require_llm_key()
    if settings.llm_provider == "anthropic":
        return AnthropicLLM(key, settings.anthropic_model, refusal_fallback=settings.anthropic_refusal_fallback,
                            client=client)
    return GeminiLLM(key, settings.gemini_model, client=client)


# --------------------------------------------------------------------------- manual / clipboard
_SEPARATOR = "-" * 72


def _copy_to_clipboard(text: str) -> bool:
    """Copy *text* to the macOS clipboard via pbcopy. Returns True on success."""
    try:
        result = subprocess.run(
            ["pbcopy"], input=text.encode(), check=False, capture_output=True
        )
        return result.returncode == 0
    except FileNotFoundError:
        return False


def _read_multiline(prompt_header: str) -> str:
    """Read until the user enters a blank line or EOF, returning all lines joined."""
    print(prompt_header, flush=True)
    lines: list[str] = []
    try:
        while True:
            line = input()
            if line.strip() == "" and lines:
                break
            lines.append(line)
    except EOFError:
        pass
    return "\n".join(lines).strip()


class ManualLLM(LLMClient):
    """Clipboard-assisted manual provider.

    generate() assembles the full prompt (system + user), copies it to the macOS
    clipboard, waits for the user to paste Gemini's reply back, and returns it as
    an LLMResult.  Token counts are estimated from character length.
    """

    provider = "manual"
    model = "gemini-web"

    def generate(self, system: str, prompt: str) -> LLMResult:
        full_prompt = (
            "SYSTEM INSTRUCTIONS:\n"
            + _SEPARATOR + "\n"
            + system.strip()
            + "\n" + _SEPARATOR + "\n\n"
            + "USER REQUEST:\n"
            + _SEPARATOR + "\n"
            + prompt.strip()
            + "\n" + _SEPARATOR
        )

        copied = _copy_to_clipboard(full_prompt)
        print("\n" + "=" * 72, flush=True)
        print("MANUAL MODE — prompt for Gemini ready", flush=True)
        if copied:
            print("✓  Prompt copied to clipboard. Paste it into Gemini, run it, then", flush=True)
            print("   copy Gemini's full reply.", flush=True)
        else:
            print("  (pbcopy not available — copy the prompt below manually)", flush=True)
            print(_SEPARATOR, flush=True)
            print(full_prompt, flush=True)
            print(_SEPARATOR, flush=True)
        print("=" * 72 + "\n", flush=True)

        text = _read_multiline(
            "Paste Gemini's reply here, then press Enter on a blank line to continue:\n"
        )

        if not text:
            raise LLMError("No reply was pasted. Run the command again and paste Gemini's output.")

        # Rough token estimates (1 token ≈ 4 chars)
        return LLMResult(
            text=text,
            provider=self.provider,
            model=self.model,
            input_tokens=len(full_prompt) // 4,
            output_tokens=len(text) // 4,
        )

    def check(self) -> str:
        return "Manual mode active — no API key required, prompts go to Gemini Web"


def build_manual_llm() -> ManualLLM:
    return ManualLLM()
