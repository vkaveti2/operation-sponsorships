import anthropic
import httpx
import httpx2
import pytest
from google.genai import errors as gerrors
from google.genai import types

from config.errors import LLMAuthError, LLMError, LLMRefusal, LLMTransientError
from config.settings import Settings
from src import llm as L

REQ = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")


def a_err(cls, status, message="boom"):
    body = {"type": "error", "error": {"type": "x", "message": message}}
    return cls(message, response=httpx2.Response(status, request=REQ, json=body), body=body)


class Block:
    def __init__(self, type, text=""):
        self.type, self.text = type, text


class Resp:
    def __init__(self, blocks, stop="end_turn", details=None, model="claude-opus-5-5"):
        self.content, self.stop_reason, self.stop_details, self.model = blocks, stop, details, model
        self.usage = type("U", (), {"input_tokens": 11, "output_tokens": 22})()


class Namespace:
    def __init__(self, handler):
        self.calls = []
        self._handler = handler

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return self._handler(kwargs)


class FakeAnthropic:
    def __init__(self, plain=None, beta=None, models=None):
        self.messages = Namespace(plain or (lambda kw: Resp([Block("text", "plain answer")])))
        self.beta = type("B", (), {})()
        self.beta.messages = Namespace(beta or (lambda kw: Resp([Block("text", "beta answer")])))
        self.models = models


# --------------------------------------------------------------------------- Anthropic
@pytest.mark.parametrize("exc,kind,needle", [
    (a_err(anthropic.AuthenticationError, 401), LLMAuthError, "ANTHROPIC_API_KEY"),
    (a_err(anthropic.PermissionDeniedError, 403, "no access"), LLMAuthError, "not allowed"),
    (a_err(anthropic.NotFoundError, 404), LLMAuthError, "ANTHROPIC_MODEL"),
    (a_err(anthropic.BadRequestError, 400, "Your credit balance is too low to access the Anthropic API."), LLMAuthError, "no credit"),
    (a_err(anthropic.BadRequestError, 400, "max_tokens too large"), LLMError, "rejected the request"),
    (a_err(anthropic.RateLimitError, 429), LLMTransientError, "busy"),
    (a_err(anthropic.InternalServerError, 500), LLMTransientError, "busy"),
    (anthropic.APITimeoutError(REQ), LLMTransientError, "busy"),
    (anthropic.APIConnectionError(request=REQ), LLMTransientError, "busy"),
])
def test_anthropic_errors_are_mapped(exc, kind, needle):
    mapped = L.map_anthropic_error(exc, "claude-opus-5-5")
    assert type(mapped) is kind and needle in str(mapped)


def test_anthropic_request_shape_for_current_models():
    fake = FakeAnthropic()
    llm = L.AnthropicLLM("k", "claude-opus-5-5", client=fake, refusal_fallback=False)
    result = llm.generate("SYS", "USER")
    sent = fake.messages.calls[0]
    assert result.text == "plain answer" and (result.input_tokens, result.output_tokens) == (11, 22)
    assert sent["system"] == "SYS" and sent["messages"] == [{"role": "user", "content": "USER"}]
    assert sent["output_config"] == {"effort": "medium"} and sent["max_tokens"] == 16000
    for banned_param in ("temperature", "top_p", "top_k", "thinking", "betas", "fallbacks"):
        assert banned_param not in sent


def test_effort_is_omitted_for_models_that_do_not_take_it():
    fake = FakeAnthropic()
    L.AnthropicLLM("k", "claude-haiku-4-5", client=fake, refusal_fallback=False).generate("s", "u")
    assert "output_config" not in fake.messages.calls[0]


def test_refusal_fallback_uses_the_beta_endpoint_with_default_routing():
    fake = FakeAnthropic()
    result = L.AnthropicLLM("k", "claude-opus-5-5", client=fake, refusal_fallback=True).generate("s", "u")
    sent = fake.beta.messages.calls[0]
    assert result.text == "beta answer" and not fake.messages.calls
    assert sent["betas"] == ["server-side-fallback-2026-07-01"] and sent["fallbacks"] == "default"


def test_fallback_is_skipped_for_models_that_do_not_support_it():
    fake = FakeAnthropic()
    L.AnthropicLLM("k", "claude-haiku-5-5", client=fake, refusal_fallback=True).generate("s", "u")
    assert fake.messages.calls and not fake.beta.messages.calls


def test_fallback_option_rejected_by_the_api_degrades_to_plain_request():
    def beta_rejects(kw):
        raise a_err(anthropic.BadRequestError, 400, "unknown beta server-side-fallback")

    fake = FakeAnthropic(beta=beta_rejects)
    llm = L.AnthropicLLM("k", "claude-opus-5-5", client=fake, refusal_fallback=True)
    assert llm.generate("s", "u").text == "plain answer"
    llm.generate("s", "u")
    assert len(fake.beta.messages.calls) == 1 and len(fake.messages.calls) == 2  # asked once, then stopped asking


def test_an_unrelated_bad_request_is_not_swallowed_by_the_fallback_logic():
    fake = FakeAnthropic(beta=lambda kw: (_ for _ in ()).throw(a_err(anthropic.BadRequestError, 400, "prompt is too long")))
    with pytest.raises(LLMError, match="prompt is too long"):
        L.AnthropicLLM("k", "claude-opus-5-5", client=fake).generate("s", "u")


def test_refusal_stop_reason_raises():
    details = type("D", (), {"category": "cyber"})()
    fake = FakeAnthropic(plain=lambda kw: Resp([Block("text", "")], stop="refusal", details=details))
    with pytest.raises(LLMRefusal, match="cyber"):
        L.AnthropicLLM("k", "claude-opus-5-5", client=fake, refusal_fallback=False).generate("s", "u")


def test_only_text_after_a_fallback_switch_counts_and_thinking_is_ignored():
    blocks = [Block("thinking"), Block("text", "declined text"), Block("fallback"), Block("thinking"), Block("text", "real "), Block("text", "answer")]
    fake = FakeAnthropic(beta=lambda kw: Resp(blocks))
    assert L.AnthropicLLM("k", "claude-opus-5-5", client=fake).generate("s", "u").text == "real answer"


def test_truncated_or_empty_answers_are_errors():
    with pytest.raises(LLMError, match="ran out of room"):
        L.AnthropicLLM("k", "claude-opus-5-5", client=FakeAnthropic(plain=lambda kw: Resp([Block("text", "half")], stop="max_tokens")), refusal_fallback=False).generate("s", "u")
    with pytest.raises(LLMError, match="empty"):
        L.AnthropicLLM("k", "claude-opus-5-5", client=FakeAnthropic(plain=lambda kw: Resp([Block("thinking")])), refusal_fallback=False).generate("s", "u")


def test_anthropic_api_errors_surface_as_project_errors():
    fake = FakeAnthropic(plain=lambda kw: (_ for _ in ()).throw(a_err(anthropic.AuthenticationError, 401)))
    with pytest.raises(LLMAuthError):
        L.AnthropicLLM("k", "claude-opus-5-5", client=fake, refusal_fallback=False).generate("s", "u")


def test_check_is_free_and_maps_errors():
    ok = type("M", (), {"retrieve": lambda self, name: object()})()
    assert "claude-opus-5-5" in L.AnthropicLLM("k", "claude-opus-5-5", client=FakeAnthropic(models=ok)).check()
    bad = type("M", (), {"retrieve": lambda self, name: (_ for _ in ()).throw(a_err(anthropic.NotFoundError, 404))})()
    with pytest.raises(LLMAuthError, match="ANTHROPIC_MODEL"):
        L.AnthropicLLM("k", "nope", client=FakeAnthropic(models=bad)).check()


# --------------------------------------------------------------------------- Gemini
def g_err(code, message="boom"):
    return gerrors.ClientError(code, {"error": {"message": message, "status": "X"}}) if code < 500 else gerrors.ServerError(code, {"error": {"message": message, "status": "X"}})


def g_response(text="gemini answer", finish="STOP", block=None):
    part = types.Part(text=text) if text is not None else None
    candidate = types.Candidate(content=types.Content(role="model", parts=[part] if part else []), finish_reason=finish)
    feedback = types.GenerateContentResponsePromptFeedback(block_reason=block) if block else None
    return types.GenerateContentResponse(
        candidates=[candidate], prompt_feedback=feedback,
        usage_metadata=types.GenerateContentResponseUsageMetadata(prompt_token_count=5, candidates_token_count=6, thoughts_token_count=7),
    )


class FakeGemini:
    def __init__(self, handler=None, get=None):
        self.calls = []
        handler = handler or (lambda **kw: g_response())
        outer = self

        class Models:
            def generate_content(self, **kw):
                outer.calls.append(kw)
                return handler(**kw)

            def get(self, **kw):
                return (get or (lambda **k: object()))(**kw)

        self.models = Models()


def test_gemini_happy_path_and_request_shape():
    fake = FakeGemini()
    result = L.GeminiLLM("k", "gemini-3.8-flash", client=fake).generate("SYS", "USER")
    sent = fake.calls[0]
    assert result.text == "gemini answer" and (result.input_tokens, result.output_tokens) == (5, 13)
    assert sent["model"] == "gemini-3.8-flash" and sent["contents"] == "USER"
    assert sent["config"].system_instruction == "SYS" and sent["config"].max_output_tokens == 8192


@pytest.mark.parametrize("code,kind,needle", [
    (400, LLMAuthError, "GEMINI_API_KEY"), (403, LLMAuthError, "GEMINI_API_KEY"), (404, LLMAuthError, "GEMINI_MODEL"),
    (429, LLMTransientError, "rate limiting"), (503, LLMTransientError, "rate limiting"), (418, LLMError, "418"),
])
def test_gemini_errors_are_mapped(code, kind, needle):
    message = "API key not valid. Please pass a valid API key." if code == 400 else "boom"
    mapped = L.map_gemini_error(g_err(code, message), "gemini-x")
    assert type(mapped) is kind and needle in str(mapped)


def test_gemini_exceptions_and_network_failures_are_wrapped():
    with pytest.raises(LLMAuthError):
        L.GeminiLLM("k", "m", client=FakeGemini(lambda **kw: (_ for _ in ()).throw(g_err(403)))).generate("s", "u")
    with pytest.raises(LLMTransientError, match="internet"):
        L.GeminiLLM("k", "m", client=FakeGemini(lambda **kw: (_ for _ in ()).throw(httpx.ConnectError("down")))).generate("s", "u")


def test_gemini_blocks_truncation_and_empty_answers():
    with pytest.raises(LLMRefusal, match="SAFETY"):
        L.GeminiLLM("k", "m", client=FakeGemini(lambda **kw: g_response(finish="SAFETY"))).generate("s", "u")
    with pytest.raises(LLMRefusal, match="blocked"):
        L.GeminiLLM("k", "m", client=FakeGemini(lambda **kw: g_response(block="PROHIBITED_CONTENT"))).generate("s", "u")
    with pytest.raises(LLMError, match="ran out of room"):
        L.GeminiLLM("k", "m", client=FakeGemini(lambda **kw: g_response(finish="MAX_TOKENS"))).generate("s", "u")
    with pytest.raises(LLMError, match="empty"):
        L.GeminiLLM("k", "m", client=FakeGemini(lambda **kw: g_response(text="   "))).generate("s", "u")
    with pytest.raises(LLMError, match="no answer"):
        L.GeminiLLM("k", "m", client=FakeGemini(lambda **kw: types.GenerateContentResponse(candidates=[]))).generate("s", "u")


def test_gemini_check():
    assert "gemini-3.8-flash" in L.GeminiLLM("k", "gemini-3.8-flash", client=FakeGemini()).check()
    with pytest.raises(LLMAuthError, match="GEMINI_MODEL"):
        L.GeminiLLM("k", "nope", client=FakeGemini(get=lambda **kw: (_ for _ in ()).throw(g_err(404)))).check()


def test_gemini_client_builds_with_retries_and_timeout():
    llm = L.GeminiLLM("test-key", "gemini-3.8-flash")  # constructs a real client object; no network
    assert llm.client is not None


# --------------------------------------------------------------------------- factory
def test_factory_picks_the_provider_and_requires_a_key():
    a = L.build_llm(Settings.load(env={"ANTHROPIC_API_KEY": "k"}), client=FakeAnthropic())
    g = L.build_llm(Settings.load(env={"LLM_PROVIDER": "gemini", "GEMINI_API_KEY": "k"}), client=FakeGemini())
    assert (a.provider, a.model) == ("anthropic", "claude-opus-5-5") and (g.provider, g.model) == ("gemini", "gemini-3.8-flash")
    with pytest.raises(Exception, match="ANTHROPIC_API_KEY"):
        L.build_llm(Settings.load(env={}))
