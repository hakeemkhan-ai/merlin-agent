"""Exercise provider retries through the installed SDK without remote credentials."""

import asyncio
import json
import logging
from unittest.mock import AsyncMock

import httpx
import pytest
from google import genai
from google.genai import types

from merlin.config import Settings
from merlin.contracts import Task, TaskSpec
from merlin.integrations.gemini import GeminiClient


@pytest.fixture
def make_adapter(monkeypatch):
    make_client = genai.Client
    monkeypatch.setattr("merlin.integrations.gemini.sleep", AsyncMock())

    def create(handler, **adapter_options):
        def client_with_transport(**kwargs):
            options = kwargs.get("http_options") or types.HttpOptions()
            # Keep the production attempt/status policy; shorten only the test's delay.
            if options.retry_options is not None:
                options = options.model_copy(
                    update={
                        "retry_options": options.retry_options.model_copy(
                            update={"initial_delay": 0.001, "max_delay": 0.001, "jitter": 0.001}
                        )
                    }
                )
            kwargs["http_options"] = options.model_copy(
                update={"async_client_args": {"transport": httpx.MockTransport(handler)}}
            )
            return make_client(**kwargs)

        monkeypatch.setattr(genai, "Client", client_with_transport)
        return GeminiClient(Settings(_env_file=None, gemini_api_key="test-key"), **adapter_options)

    return create


async def generate(adapter):
    return await adapter.generate(
        Task(spec=TaskSpec(prompt="Hello")), [{"role": "user", "parts": [{"text": "Hello"}]}], []
    )


def chunk(text):
    return (
        "data: "
        + json.dumps(
            {
                "candidates": [
                    {
                        "content": {"role": "model", "parts": [{"text": text}]},
                        "finishReason": "STOP",
                    }
                ]
            }
        )
        + "\n\n"
    )


@pytest.mark.parametrize("stage", ["count", "generate"])
@pytest.mark.parametrize("status", [429, 500, 503])
async def test_transient_gemini_http_failure_is_retried_before_response(
    make_adapter, stage, status
):
    attempts = {"count": 0, "generate": 0}

    def respond(request):
        endpoint = "count" if request.url.path.endswith(":countTokens") else "generate"
        attempts[endpoint] += 1
        if endpoint == stage and attempts[endpoint] == 1:
            return httpx.Response(status, json={"error": {"code": status, "message": "temporary"}})
        if endpoint == "count":
            return httpx.Response(200, json={"totalTokens": 10})
        return httpx.Response(
            200, headers={"Content-Type": "text/event-stream"}, text=chunk("Done")
        )

    adapter = make_adapter(respond)
    try:
        reply = await generate(adapter)
        assert reply.text == "Done"
        assert attempts[stage] == 2
        assert attempts["generate" if stage == "count" else "count"] == 1
    finally:
        await adapter.client.aio.aclose()
        adapter.client.close()


@pytest.mark.parametrize(
    "status,expected_attempts", [(400, 1), (401, 1), (403, 1), (429, 3), (503, 3)]
)
async def test_gemini_retries_are_bounded_and_error_body_is_private(
    make_adapter, status, expected_attempts
):
    from merlin.contracts import ModelServiceError

    attempts = 0

    def respond(request):
        nonlocal attempts
        attempts += 1
        return httpx.Response(
            status, json={"error": {"code": status, "message": "private-provider-payload"}}
        )

    adapter = make_adapter(respond)
    try:
        with pytest.raises(ModelServiceError) as caught:
            await generate(adapter)
        assert attempts == expected_attempts
        assert str(status) in str(caught.value)
        assert "private-provider-payload" not in str(caught.value)
        assert caught.value.retryable == (status in {429, 503})
    finally:
        await adapter.client.aio.aclose()
        adapter.client.close()


async def test_partial_stream_failure_is_not_automatically_replayed(make_adapter):
    from merlin.contracts import ModelServiceError

    attempts = 0
    deltas = []

    class BrokenStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield chunk("x" * 300).encode()
            raise httpx.ReadError("private-provider-payload")

    def respond(request):
        nonlocal attempts
        if request.url.path.endswith(":countTokens"):
            return httpx.Response(200, json={"totalTokens": 10})
        attempts += 1
        return httpx.Response(
            200, headers={"Content-Type": "text/event-stream"}, stream=BrokenStream()
        )

    adapter = make_adapter(respond, on_text=lambda task, text: deltas.append(text))
    try:
        with pytest.raises(ModelServiceError, match="connection"):
            await generate(adapter)
        assert attempts == 1
        assert deltas == ["x" * 300]
    finally:
        await adapter.client.aio.aclose()
        adapter.client.close()


async def test_gemini_http_retry_preserves_cancellation(make_adapter):
    started = asyncio.Event()
    attempts = 0

    async def respond(request):
        nonlocal attempts
        attempts += 1
        started.set()
        await asyncio.Event().wait()

    adapter = make_adapter(respond)
    generation = asyncio.create_task(generate(adapter))
    try:
        await asyncio.wait_for(started.wait(), timeout=2)
        generation.cancel()
        with pytest.raises(asyncio.CancelledError):
            await generation
        assert attempts == 1
    finally:
        generation.cancel()
        await adapter.client.aio.aclose()
        adapter.client.close()


async def test_sdk_retry_logging_does_not_expose_provider_body(make_adapter, caplog, monkeypatch):
    from merlin.telemetry import configure

    # Restore logger policy after exercising the real worker logging configuration.
    for name in ("httpx", "httpcore", "google.genai", "google_genai"):
        logger = logging.getLogger(name)
        monkeypatch.setattr(logger, "level", logger.level)
    attempts = 0

    def respond(request):
        nonlocal attempts
        if request.url.path.endswith(":countTokens"):
            attempts += 1
            if attempts == 1:
                return httpx.Response(
                    503, json={"error": {"code": 503, "message": "private-provider-payload"}}
                )
            return httpx.Response(200, json={"totalTokens": 10})
        return httpx.Response(
            200, headers={"Content-Type": "text/event-stream"}, text=chunk("Done")
        )

    adapter = make_adapter(respond)
    try:
        with caplog.at_level(logging.INFO):
            configure()
            assert (await generate(adapter)).text == "Done"
        assert attempts == 2
        assert "private-provider-payload" not in caplog.text
    finally:
        await adapter.client.aio.aclose()
        adapter.client.close()


@pytest.mark.parametrize("stage", ["count", "generate"])
async def test_unknown_transport_outcome_is_not_automatically_retried(make_adapter, stage):
    from merlin.contracts import ModelServiceError

    attempts = 0

    def respond(request):
        nonlocal attempts
        if stage == "generate" and request.url.path.endswith(":countTokens"):
            return httpx.Response(200, json={"totalTokens": 10})
        attempts += 1
        raise httpx.ReadTimeout("private-provider-payload")

    adapter = make_adapter(respond)
    try:
        with pytest.raises(ModelServiceError, match="connection"):
            await generate(adapter)
        assert attempts == 1
    finally:
        await adapter.client.aio.aclose()
        adapter.client.close()


async def test_transient_stream_error_before_first_chunk_is_retried(make_adapter):
    attempts = 0

    def respond(request):
        nonlocal attempts
        if request.url.path.endswith(":countTokens"):
            return httpx.Response(200, json={"totalTokens": 10})
        attempts += 1
        text = (
            'data: {"error": {"code": 503, "message": "private-provider-payload"}}\n\n'
            if attempts == 1
            else chunk("Done")
        )
        return httpx.Response(200, headers={"Content-Type": "text/event-stream"}, text=text)

    adapter = make_adapter(respond)
    try:
        assert (await generate(adapter)).text == "Done"
        assert attempts == 2
    finally:
        await adapter.client.aio.aclose()
        adapter.client.close()
