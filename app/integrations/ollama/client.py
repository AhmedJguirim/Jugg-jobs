"""Client for a local Ollama server.

**GPU safety.** This is the only code in the project that occupies the GPU. It
does not serialise itself - callers must go through the `llm` DBOS queue, which
is registered with `global_concurrency=1` and `worker_concurrency=1`. See
`app/workflows/llm_queue.py` and ADR-0007. Calling these functions directly
from a script bypasses that guarantee, so scripts enqueue work instead.

**Why the options are explicit.** `ollama show qwen3.5:4b` reports the model's
own defaults as `temperature=1`, `presence_penalty=1.5` and thinking *on*.
Those are wrong for this workload:

- `temperature=1` invents wording; a low temperature keeps the output faithful
  to the text it was given.
- `presence_penalty=1.5` pushes the model away from repeating words it has
  already used - but quoting the advert verbatim is exactly what the grader
  asks for, so it is zeroed.
- thinking cost 4547 output tokens and 51s versus 163 tokens and 29s for the
  same quality when measured on this corpus, so it is off by default and
  opt-in per call.

Every request therefore sends its options explicitly rather than inheriting the
model's.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Literal, Optional

import httpx

from app.config import get_settings
from app.logging_setup import log

Role = Literal["system", "user", "assistant"]


class OllamaError(RuntimeError):
    """Any failure talking to the Ollama server."""


class OllamaUnavailable(OllamaError):
    """The server could not be reached at all."""


@dataclass
class LlmResult:
    """One completion plus the accounting needed to record and cost it."""

    content: str
    model: str
    thinking: str = ""
    prompt_tokens: int = 0
    output_tokens: int = 0
    duration_s: float = 0.0
    load_s: float = 0.0
    raw: dict = field(default_factory=dict, repr=False)

    @property
    def output_tokens_per_second(self) -> float:
        eval_ns = self.raw.get("eval_duration") or 0
        if not eval_ns or not self.output_tokens:
            return 0.0
        return self.output_tokens / (eval_ns / 1e9)


def _options(
    *,
    temperature: float,
    num_ctx: int,
    num_predict: Optional[int],
    extra: Optional[dict] = None,
) -> dict:
    options = {
        "temperature": temperature,
        "num_ctx": num_ctx,
        # Neutralise the model's own defaults (see module docstring).
        "presence_penalty": 0.0,
        "frequency_penalty": 0.0,
        "top_k": 40,
        "top_p": 0.9,
    }
    if num_predict is not None:
        options["num_predict"] = num_predict
    if extra:
        options.update(extra)
    return options


def chat(
    messages: list[dict[str, str]],
    *,
    model: Optional[str] = None,
    temperature: float = 0.2,
    num_ctx: Optional[int] = None,
    num_predict: Optional[int] = None,
    think: bool = False,
    json_format: bool = False,
    timeout: Optional[float] = None,
    options: Optional[dict] = None,
    retries: int = 2,
) -> LlmResult:
    """Send a chat completion and return the reply with its token accounting.

    `json_format=True` asks Ollama to constrain output to valid JSON, which is
    far more reliable on a small model than instructing it to "reply with JSON".

    Retries cover transport failures and 5xx responses - a cold model load can
    time out the first attempt - but never a 4xx, which would just fail again.
    """
    settings = get_settings()
    model = model or settings.llm_model
    num_ctx = num_ctx or settings.llm_num_ctx
    timeout = timeout or settings.llm_timeout_seconds

    payload: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "stream": False,
        "think": think,
        "keep_alive": settings.llm_keep_alive,
        "options": _options(
            temperature=temperature,
            num_ctx=num_ctx,
            num_predict=num_predict,
            extra=options,
        ),
    }
    if json_format:
        payload["format"] = "json"

    url = f"{settings.ollama_base_url.rstrip('/')}/api/chat"
    last_error: Optional[Exception] = None

    for attempt in range(1, retries + 2):
        started = time.monotonic()
        try:
            with httpx.Client(timeout=timeout) as client:
                response = client.post(url, json=payload)
            if response.status_code >= 500:
                raise OllamaError(f"ollama returned {response.status_code}: {response.text[:200]}")
            response.raise_for_status()
        except httpx.HTTPStatusError as error:
            # A 4xx is our fault (bad model name, malformed body); do not retry.
            raise OllamaError(
                f"ollama rejected the request ({error.response.status_code}): "
                f"{error.response.text[:200]}"
            ) from error
        except (httpx.TransportError, OllamaError) as error:
            last_error = error
            log.warning(
                "ollama call failed (attempt %s/%s): %s", attempt, retries + 1, error
            )
            if attempt > retries:
                break
            time.sleep(min(2 ** attempt, 10))
            continue

        body = response.json()
        message = body.get("message") or {}
        result = LlmResult(
            content=(message.get("content") or "").strip(),
            thinking=(message.get("thinking") or ""),
            model=body.get("model", model),
            prompt_tokens=body.get("prompt_eval_count") or 0,
            output_tokens=body.get("eval_count") or 0,
            duration_s=time.monotonic() - started,
            load_s=(body.get("load_duration") or 0) / 1e9,
            raw=body,
        )
        log.debug(
            "ollama %s: %s prompt + %s output tokens in %.1fs (%.0f tok/s)",
            result.model,
            result.prompt_tokens,
            result.output_tokens,
            result.duration_s,
            result.output_tokens_per_second,
        )
        return result

    raise OllamaUnavailable(
        f"ollama unreachable at {url} after {retries + 1} attempt(s): {last_error}"
    ) from last_error


def list_models(timeout: float = 10.0) -> list[str]:
    """Model names the server has available."""
    settings = get_settings()
    url = f"{settings.ollama_base_url.rstrip('/')}/api/tags"
    try:
        with httpx.Client(timeout=timeout) as client:
            body = client.get(url).json()
    except httpx.TransportError as error:
        raise OllamaUnavailable(f"ollama unreachable at {url}: {error}") from error
    return [model["name"] for model in body.get("models", [])]


def health() -> tuple[bool, str]:
    """Check the server is up and the configured model is present.

    Called before a long enrichment run so a typo in the model name fails in a
    second rather than after the first queued job.
    """
    settings = get_settings()
    try:
        models = list_models()
    except OllamaUnavailable as error:
        return False, str(error)

    if settings.llm_model not in models:
        return False, (
            f"model {settings.llm_model!r} is not installed "
            f"(available: {', '.join(models) or 'none'}); run: ollama pull {settings.llm_model}"
        )
    return True, f"ollama ready with {settings.llm_model}"
