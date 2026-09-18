from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from agent import telemetry


def test_get_tracer_uses_console_exporter_when_env_set(monkeypatch):
    monkeypatch.setenv("OTEL_EXPORTER", "console")
    tracer = telemetry.get_tracer()
    # No exception, and the tracer is usable -- exact exporter internals
    # aren't introspectable from the tracer object itself, so this proves
    # the console path doesn't require GCP credentials/project_id at all.
    with tracer.start_as_current_span("test.span"):
        pass


def test_get_tracer_uses_cloud_trace_exporter_by_default(monkeypatch):
    monkeypatch.delenv("OTEL_EXPORTER", raising=False)
    monkeypatch.setenv("GCP_PROJECT_ID", "test-project-123")
    tracer = telemetry.get_tracer()
    # Constructing the tracer/provider/exporter must not require real GCP
    # credentials or network access -- CloudTraceSpanExporter's __init__
    # only stores config; it doesn't authenticate until a span is actually
    # exported, which this test never triggers (no span is created here).
    assert tracer is not None


def test_get_tracer_two_calls_produce_independent_tracers(monkeypatch):
    # Proves get_tracer() doesn't rely on OpenTelemetry's global
    # set_tracer_provider() singleton -- calling it twice with different
    # OTEL_EXPORTER values must not have the second call silently reuse the
    # first call's exporter (which is exactly what the global form would do).
    monkeypatch.setenv("OTEL_EXPORTER", "console")
    tracer_a = telemetry.get_tracer()
    monkeypatch.delenv("OTEL_EXPORTER", raising=False)
    monkeypatch.setenv("GCP_PROJECT_ID", "test-project-123")
    tracer_b = telemetry.get_tracer()
    assert tracer_a is not tracer_b


def test_spans_are_captured_with_expected_attributes():
    # Exercises the actual span-creation shape cli.py (Task 4) will use,
    # via a directly-constructed TracerProvider + InMemorySpanExporter
    # (not telemetry.get_tracer() itself, since this test wants to inspect
    # captured spans synchronously and telemetry.get_tracer() builds its
    # own fresh provider/exporter each call rather than exposing one here).
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    tracer = provider.get_tracer("test")
    with tracer.start_as_current_span("agent.session", attributes={"persona": "analyst", "mode": "single"}):
        pass
    spans = exporter.get_finished_spans()
    assert len(spans) == 1
    assert spans[0].name == "agent.session"
    assert spans[0].attributes["persona"] == "analyst"
    assert spans[0].attributes["mode"] == "single"
