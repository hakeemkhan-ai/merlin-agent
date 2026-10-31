"""Bounded operational telemetry without prompts, tool arguments, or credentials."""

from __future__ import annotations

import json
import logging
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from prometheus_client import Counter, Gauge, Histogram

TASKS = Counter("merlin_task_transitions_total", "Task transitions", ["state"])
OPERATIONS = Histogram("merlin_operation_seconds", "Operation latency", ["operation"])
ERRORS = Counter("merlin_errors_total", "Operational failures", ["component"])
TOKENS = Counter("merlin_model_tokens_total", "Model token consumption", ["kind"])
MODEL_COST = Counter("merlin_model_cost_usd_total", "Estimated completed model call cost")
QUEUE_SECONDS = Histogram("merlin_queue_seconds", "Time from submission to a worker claim")
TASK_STATES = Gauge("merlin_tasks", "Current durable task counts", ["state"])
SANDBOX_SECONDS = Counter("merlin_sandbox_seconds_total", "Reserved sandbox runtime seconds")
RECOVERIES = Counter("merlin_task_recoveries_total", "Task claims after the first generation")
COMPACTIONS = Counter("merlin_context_compactions_total", "Context summaries assembled")


class JSONFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        return json.dumps(
            {"level": record.levelname, "logger": record.name, "message": record.getMessage()}
        )


def configure(endpoint: str = "") -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(JSONFormatter())
    logging.basicConfig(level=logging.INFO, handlers=[handler])
    for name in ("httpx", "httpcore", "google.genai", "google_genai"):
        logging.getLogger(name).setLevel(logging.WARNING)
    if endpoint:
        from opentelemetry import trace
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor

        provider = TracerProvider(resource=Resource.create({"service.name": "merlin"}))
        provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoint)))
        trace.set_tracer_provider(provider)


@contextmanager
def span(name: str, **attributes: Any) -> Iterator[Any]:
    from opentelemetry import trace
    from opentelemetry.trace import Status, StatusCode

    # Provider exception strings can contain request data. Record a bounded
    # error status while keeping original exceptions inside the worker only.
    with trace.get_tracer("merlin").start_as_current_span(
        name, attributes=attributes, record_exception=False, set_status_on_exception=False
    ) as current:
        try:
            yield current
        except BaseException:
            current.set_status(Status(StatusCode.ERROR))
            raise
