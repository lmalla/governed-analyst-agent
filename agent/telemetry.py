"""OpenTelemetry tracer setup: a console exporter for offline dev, the real
Cloud Trace exporter otherwise. Authenticates as the human's own default
credentials (ADC), never persona impersonation -- trace/span writing is
operational observability, not governed customer data, so it sits outside
the warehouse's permission boundary (BUILD_SPEC: "the warehouse enforces
permissions, not the prompt" -- traces aren't warehouse data).

Spans themselves are created by callers (agent/cli.py), never here or
inside agent/agent.py, which stays free of any OpenTelemetry dependency.

get_tracer() returns a tracer from a freshly built TracerProvider via that
provider's own .get_tracer() method, rather than going through the global
opentelemetry.trace.set_tracer_provider()/get_tracer() singleton -- the
global provider can only be set once per process, so using it here would
make repeated get_tracer() calls (e.g. across multiple pytest tests in the
same process) silently reuse whichever exporter was configured first.

NOTE (deliberate, not an oversight): opentelemetry-exporter-gcp-trace's
CloudTraceSpanExporter is deprecated as of this writing in favor of the
generic OTLPSpanExporter pointed at Cloud Trace's OTLP endpoint, which
needs explicit gRPC credential wiring (AuthMetadataPlugin + composite
credentials) and three more dependencies. For this 3-day prototype, whose
"Done when" bar (BUILD_SPEC §6 Phase 2) is just "a visible trace in Cloud
Trace" -- not a specific exporter architecture -- the deprecated exporter's
simple one-line construction was chosen over that added complexity. See
https://github.com/GoogleCloudPlatform/opentelemetry-operations-python/blob/main/MIGRATION.md
if this project ever needs to migrate off it.
"""
import os

from opentelemetry.exporter.cloud_trace import CloudTraceSpanExporter
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, ConsoleSpanExporter

from agent import config


def get_tracer(tracer_name: str = "governed-agent"):
    if os.environ.get("OTEL_EXPORTER") == "console":
        exporter = ConsoleSpanExporter()
    else:
        exporter = CloudTraceSpanExporter(project_id=config.get_project_id())
    provider = TracerProvider()
    provider.add_span_processor(BatchSpanProcessor(exporter))
    return provider.get_tracer(tracer_name)
