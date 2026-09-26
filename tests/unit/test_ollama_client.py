"""Unit tests for the Ollama client.

httpx is stubbed, so no server or GPU is needed. The option-override tests
matter: the model's own defaults (temperature=1, presence_penalty=1.5, thinking
on) are actively wrong for this workload, so the client must always send its own.
"""

from __future__ import annotations

import httpx
import pytest

from app.integrations.ollama import client as ollama


def response(status: int = 200, body: dict | None = None) -> httpx.Response:
    return httpx.Response(
        status_code=status,
        json=body
        if body is not None
        else {
            "model": "qwen3.5:4b",
            "message": {"content": "reply"},
            "prompt_eval_count": 100,
            "eval_count": 50,
            "eval_duration": 500_000_000,
            "load_duration": 1_000_000_000,
        },
    )


class StubTransport:
    """Captures requests and returns queued responses."""

    def __init__(self, *responses: httpx.Response):
        self.responses = list(responses) or [response()]
        self.requests: list[httpx.Request] = []

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        result = self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]
        if isinstance(result, Exception):
            raise result
        return result


@pytest.fixture
def stub(monkeypatch):
    def install(*responses):
        transport = StubTransport(*responses)
        real_client = httpx.Client

        def factory(*args, **kwargs):
            kwargs["transport"] = httpx.MockTransport(transport.handle_request)
            return real_client(*args, **kwargs)

        monkeypatch.setattr(ollama.httpx, "Client", factory)
        return transport

    return install


def sent_payload(transport: StubTransport, index: int = 0) -> dict:
    import json

    return json.loads(transport.requests[index].content)


class TestChat:
    def test_returns_the_reply(self, stub):
        stub()

        result = ollama.chat([{"role": "user", "content": "hi"}])

        assert result.content == "reply"
        assert result.model == "qwen3.5:4b"

    def test_reports_token_counts(self, stub):
        stub()

        result = ollama.chat([{"role": "user", "content": "hi"}])

        assert result.prompt_tokens == 100
        assert result.output_tokens == 50

    def test_computes_throughput(self, stub):
        stub()

        result = ollama.chat([{"role": "user", "content": "hi"}])

        assert result.output_tokens_per_second == pytest.approx(100.0)

    def test_strips_whitespace_from_content(self, stub):
        stub(response(body={"model": "m", "message": {"content": "  padded  "}}))

        assert ollama.chat([{"role": "user", "content": "hi"}]).content == "padded"

    def test_captures_thinking_when_present(self, stub):
        stub(response(body={"model": "m", "message": {"content": "c", "thinking": "hmm"}}))

        assert ollama.chat([{"role": "user", "content": "hi"}]).thinking == "hmm"


class TestRequestOptions:
    def test_thinking_is_off_by_default(self, stub):
        """Measured: thinking cost 4547 tokens vs 163 for the same output."""
        transport = stub()

        ollama.chat([{"role": "user", "content": "hi"}])

        assert sent_payload(transport)["think"] is False

    def test_thinking_can_be_enabled(self, stub):
        transport = stub()

        ollama.chat([{"role": "user", "content": "hi"}], think=True)

        assert sent_payload(transport)["think"] is True

    def test_streaming_is_off(self, stub):
        transport = stub()

        ollama.chat([{"role": "user", "content": "hi"}])

        assert sent_payload(transport)["stream"] is False

    def test_presence_penalty_is_neutralised(self, stub):
        """The model ships with 1.5, which fights faithful translation."""
        transport = stub()

        ollama.chat([{"role": "user", "content": "hi"}])

        assert sent_payload(transport)["options"]["presence_penalty"] == 0.0

    def test_temperature_is_sent_explicitly(self, stub):
        transport = stub()

        ollama.chat([{"role": "user", "content": "hi"}], temperature=0.15)

        assert sent_payload(transport)["options"]["temperature"] == 0.15

    def test_context_size_is_sent(self, stub):
        transport = stub()

        ollama.chat([{"role": "user", "content": "hi"}], num_ctx=4096)

        assert sent_payload(transport)["options"]["num_ctx"] == 4096

    def test_keep_alive_is_sent_to_avoid_reloading(self, stub):
        transport = stub()

        ollama.chat([{"role": "user", "content": "hi"}])

        assert "keep_alive" in sent_payload(transport)

    def test_json_mode_sets_format(self, stub):
        transport = stub()

        ollama.chat([{"role": "user", "content": "hi"}], json_format=True)

        assert sent_payload(transport)["format"] == "json"

    def test_json_mode_is_off_by_default(self, stub):
        transport = stub()

        ollama.chat([{"role": "user", "content": "hi"}])

        assert "format" not in sent_payload(transport)

    def test_extra_options_are_merged(self, stub):
        transport = stub()

        ollama.chat([{"role": "user", "content": "hi"}], options={"seed": 7})

        assert sent_payload(transport)["options"]["seed"] == 7


class TestErrorHandling:
    def test_retries_a_server_error_then_succeeds(self, stub):
        transport = stub(response(status=500, body={"error": "busy"}), response())

        result = ollama.chat([{"role": "user", "content": "hi"}], retries=1)

        assert result.content == "reply"
        assert len(transport.requests) == 2

    def test_client_error_is_not_retried(self, stub):
        transport = stub(response(status=404, body={"error": "model not found"}))

        with pytest.raises(ollama.OllamaError):
            ollama.chat([{"role": "user", "content": "hi"}], retries=2)

        assert len(transport.requests) == 1

    def test_transport_failure_raises_unavailable(self, stub):
        stub(httpx.ConnectError("refused"))

        with pytest.raises(ollama.OllamaUnavailable):
            ollama.chat([{"role": "user", "content": "hi"}], retries=0)

    def test_exhausted_retries_raise_unavailable(self, stub):
        stub(httpx.ConnectError("refused"))

        with pytest.raises(ollama.OllamaUnavailable) as excinfo:
            ollama.chat([{"role": "user", "content": "hi"}], retries=1)

        assert "unreachable" in str(excinfo.value)


class TestHealth:
    def test_reports_ready_when_the_model_is_present(self, stub, monkeypatch):
        monkeypatch.setattr(ollama, "list_models", lambda **_: ["qwen3.5:4b"])

        ok, message = ollama.health()

        assert ok is True
        assert "ready" in message

    def test_reports_a_missing_model_with_the_pull_command(self, monkeypatch):
        monkeypatch.setattr(ollama, "list_models", lambda **_: ["llama3:8b"])

        ok, message = ollama.health()

        assert ok is False
        assert "ollama pull" in message

    def test_reports_an_unreachable_server(self, monkeypatch):
        def explode(**_):
            raise ollama.OllamaUnavailable("connection refused")

        monkeypatch.setattr(ollama, "list_models", explode)

        ok, message = ollama.health()

        assert ok is False
        assert "refused" in message


class TestListModels:
    def test_returns_model_names(self, stub):
        stub(response(body={"models": [{"name": "qwen3.5:4b"}, {"name": "llama3:8b"}]}))

        assert ollama.list_models() == ["qwen3.5:4b", "llama3:8b"]

    def test_unreachable_server_raises(self, stub):
        stub(httpx.ConnectError("refused"))

        with pytest.raises(ollama.OllamaUnavailable):
            ollama.list_models()
