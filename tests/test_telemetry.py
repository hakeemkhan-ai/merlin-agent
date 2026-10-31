import unittest
from unittest.mock import patch

from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import StatusCode

from merlin.telemetry import span


class TelemetryTests(unittest.TestCase):
    def test_provider_exceptions_do_not_export_sensitive_request_text(self):
        exporter = InMemorySpanExporter()
        provider = TracerProvider()
        provider.add_span_processor(SimpleSpanProcessor(exporter))
        tracer = provider.get_tracer("test")
        with patch("opentelemetry.trace.get_tracer", return_value=tracer):
            with self.assertRaises(ValueError), span("model.generate", task_id="task-1"):
                raise ValueError("sensitive-source-and-credentials")
        recorded = exporter.get_finished_spans()[0]
        self.assertEqual(recorded.status.status_code, StatusCode.ERROR)
        self.assertIsNone(recorded.status.description)
        self.assertEqual(len(recorded.events), 0)
        self.assertNotIn("sensitive", str(recorded.attributes))
        provider.shutdown()
