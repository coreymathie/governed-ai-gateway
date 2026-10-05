# Corey Mathie, 2026
"""
OpenTelemetry GenAI spans for provider calls, if OpenTelemetry is installed.

One CLIENT span per provider attempt (so fallbacks show up as sibling spans),
named "{gen_ai.operation.name} {gen_ai.request.model}", e.g. "chat gpt-4.1-mini",
with the GenAI semantic-convention attributes:

  gen_ai.operation.name        "chat"
  gen_ai.provider.name         openai | anthropic | gcp.gemini | ollama
  gen_ai.request.model         model requested from the provider
  gen_ai.response.model        model reported back (when available)
  gen_ai.usage.input_tokens    prompt tokens
  gen_ai.usage.output_tokens   completion tokens
  error.type                   exception class on failure

plus gateway attributes: router.alias, router.attempt, router.strategy,
router.cost_usd, router.circuit.state.

Without the opentelemetry-api package every function here is a no-op. Nothing
is exported unless the host process configures a TracerProvider/exporter
(e.g. opentelemetry-instrument or the OTLP exporter). Prompt and completion
text are never put on spans.
"""

from __future__ import annotations

from typing import Any

try:  # optional dependency
    from opentelemetry import trace as _otel_trace
    from opentelemetry.trace import SpanKind as _SpanKind
    from opentelemetry.trace import Status as _Status
    from opentelemetry.trace import StatusCode as _StatusCode
except ImportError:  # pragma: no cover - exercised by monkeypatching in tests
    _otel_trace = None

TRACER_NAME = "governed-ai-gateway"
PROVIDER_NAMES = {"openai": "openai", "anthropic": "anthropic", "gemini": "gcp.gemini", "ollama": "ollama"}

_provider_override = None  # tests can inject a TracerProvider without touching the global one


def enabled() -> bool:
    return _otel_trace is not None


def configure(tracer_provider) -> None:
    global _provider_override
    _provider_override = tracer_provider


def _tracer():
    if _otel_trace is None:
        return None
    if _provider_override is not None:
        return _provider_override.get_tracer(TRACER_NAME)
    return _otel_trace.get_tracer(TRACER_NAME)


class GenAISpan:
    """Thin wrapper so call sites never need to check whether OTel is present."""

    def __init__(self, span: Any | None):
        self._span = span

    def set(self, name: str, value: Any) -> None:
        if self._span is not None and value is not None:
            self._span.set_attribute(name, value)

    def set_usage(self, input_tokens: int, output_tokens: int, cost_usd: float, response_model: str | None = None):
        self.set("gen_ai.usage.input_tokens", int(input_tokens))
        self.set("gen_ai.usage.output_tokens", int(output_tokens))
        self.set("router.cost_usd", float(cost_usd))
        self.set("gen_ai.response.model", response_model)

    def fail(self, err: BaseException) -> None:
        if self._span is None:
            return
        self._span.set_attribute("error.type", type(err).__name__)
        self._span.set_status(_Status(_StatusCode.ERROR, type(err).__name__))

    def end(self) -> None:
        if self._span is not None:
            self._span.end()
            self._span = None


def start_chat_span(
    provider: str, model: str, alias: str, attempt: int = 1, strategy: str = "ordered", stream: bool = False
) -> GenAISpan:
    tracer = _tracer()
    if tracer is None:
        return GenAISpan(None)
    span = tracer.start_span(
        f"chat {model}",
        kind=_SpanKind.CLIENT,
        attributes={
            "gen_ai.operation.name": "chat",
            "gen_ai.provider.name": PROVIDER_NAMES.get(provider, provider),
            "gen_ai.request.model": model,
            "router.alias": alias,
            "router.attempt": attempt,
            "router.strategy": strategy,
            "router.stream": stream,
        },
    )
    return GenAISpan(span)
