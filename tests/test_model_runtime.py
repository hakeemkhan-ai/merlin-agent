"""Model response completeness, deadlines, usage, and safe preflight reuse."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from google.genai import types

from merlin.config import Settings
from merlin.contracts import BudgetExceeded, Task, TaskSpec
from merlin.integrations.gemini import GeminiClient, _retry_delay
from merlin.integrations.model_policy import ModelRequestFailure


def response(
    text: str = "Done",
    *,
    finish: str | None = "STOP",
    parts: list[types.Part] | None = None,
    usage: bool = True,
) -> types.GenerateContentResponse:
    return types.GenerateContentResponse(
        candidates=[
            types.Candidate(
                content=types.Content(role="model", parts=parts or [types.Part(text=text)]),
                finish_reason=finish,
            )
        ],
        response_id="response-1",
        usage_metadata=types.GenerateContentResponseUsageMetadata(
            prompt_token_count=100,
            cached_content_token_count=40,
            candidates_token_count=20,
            thoughts_token_count=5,
        )
        if usage
        else None,
    )


def adapter_for(chunks: list[types.GenerateContentResponse], **settings: object) -> GeminiClient:
    async def stream(**kwargs: object) -> AsyncIterator[types.GenerateContentResponse]:
        for item in chunks:
            yield item

    models = SimpleNamespace(
        count_tokens=AsyncMock(return_value=types.CountTokensResponse(total_tokens=100)),
        generate_content_stream=AsyncMock(side_effect=stream),
    )
    return GeminiClient(
        Settings(_env_file=None, **settings),
        client=SimpleNamespace(aio=SimpleNamespace(models=models)),
    )


async def generate(adapter: GeminiClient, text: str = "Hello") -> object:
    return await adapter.generate(
        Task(spec=TaskSpec(prompt=text)), [{"role": "user", "parts": [{"text": text}]}], []
    )


@pytest.mark.parametrize(
    "finish,reason,retryable",
    [
        ("MAX_TOKENS", "model_output_limit", True),
        ("CONTINUATION", "model_output_limit", True),
        ("SAFETY", "model_blocked", False),
        ("RECITATION", "model_blocked", False),
        ("MALFORMED_FUNCTION_CALL", "model_invalid_response", True),
        ("OTHER", "model_invalid_response", True),
        (None, "model_incomplete_response", True),
    ],
)
async def test_nonterminal_or_blocked_response_never_completes(finish, reason, retryable):
    adapter = adapter_for([response("Partial answer", finish=finish)])
    with pytest.raises(ModelRequestFailure) as caught:
        await generate(adapter)
    assert caught.value.reason == reason
    assert caught.value.retryable is retryable
    assert caught.value.request_started
    assert adapter.client.aio.models.generate_content_stream.await_count == 1


async def test_truncated_native_call_is_never_returned_for_dispatch():
    adapter = adapter_for(
        [
            response(
                finish="MAX_TOKENS",
                parts=[
                    types.Part(
                        function_call=types.FunctionCall(name="write_file", args={"path": "a"}),
                        thought_signature=b"opaque-native-state",
                    )
                ],
            )
        ]
    )
    with pytest.raises(ModelRequestFailure, match="output limit"):
        await generate(adapter)


@pytest.mark.parametrize(
    "call",
    [
        types.FunctionCall(name="", args={}),
        types.FunctionCall(name="read_file", args={}, will_continue=True),
        types.FunctionCall(name="read_file", partial_args=[types.PartialArg(json_path="$.a")]),
    ],
)
async def test_invalid_or_partial_call_is_rejected(call):
    adapter = adapter_for([response(parts=[types.Part(function_call=call)])])
    with pytest.raises(ModelRequestFailure) as caught:
        await generate(adapter)
    assert caught.value.reason == "model_invalid_response"


async def test_duplicate_call_ids_are_not_dispatched_twice():
    part = types.Part(function_call=types.FunctionCall(id="same", name="read_file", args={}))
    adapter = adapter_for([response(parts=[part, part])])
    with pytest.raises(ModelRequestFailure) as caught:
        await generate(adapter)
    assert caught.value.reason == "model_invalid_response"


@pytest.mark.parametrize("canonical", [True, False])
async def test_historical_call_ids_cannot_be_reused_in_a_later_turn(canonical):
    call = types.FunctionCall(id="previous-call", name="write_file", args={"path": "app.py"})
    adapter = adapter_for([response(parts=[types.Part(function_call=call)])])
    previous = {
        "role": "model",
        "parts": [
            {
                "function_call": {
                    "id": "previous-call",
                    "name": "read_file",
                    "args": {"path": "app.py"},
                }
            }
        ],
    }
    task = Task(spec=TaskSpec(prompt="Continue"), history=[previous] if canonical else [])
    visible = [{"role": "user", "parts": [{"text": "Continue"}]}]
    if not canonical:
        visible.insert(0, previous)
    with pytest.raises(ModelRequestFailure) as caught:
        await adapter.generate(task, visible, [])
    assert caught.value.reason == "model_invalid_response"


async def test_empty_and_thought_only_streams_cannot_complete():
    for chunks in ([], [response(parts=[types.Part(text="private thought", thought=True)])]):
        adapter = adapter_for(chunks)
        with pytest.raises(ModelRequestFailure):
            await generate(adapter)


async def test_final_usage_separates_cached_input_and_thinking_and_uses_configured_rates():
    adapter = adapter_for(
        [response()],
        input_cost_per_million=2,
        cached_input_cost_per_million=0.5,
        output_cost_per_million=4,
    )
    reply = await generate(adapter)
    assert reply.input_tokens == 100
    assert reply.cached_input_tokens == 40
    assert reply.output_tokens == 25
    assert reply.thinking_tokens == 5
    assert reply.cost_usd == pytest.approx((60 * 2 + 40 * 0.5 + 25 * 4) / 1_000_000)
    assert reply.finish_reason == "STOP"
    assert reply.provider_response_id == "response-1"
    assert reply.generation_attempts == 1
    assert reply.usage_complete


async def test_missing_usage_is_explicit_and_not_reported_as_free():
    adapter = adapter_for([response(usage=False)])
    reply = await generate(adapter)
    assert not reply.usage_complete


async def test_continuous_small_chunks_obey_flush_interval_before_size_threshold():
    adapter = adapter_for([], model_stream_flush_seconds=0.02)
    deltas: list[str] = []
    adapter.on_text = lambda _task, text: deltas.append(text)

    async def stream(**kwargs):
        for index in range(30):
            await asyncio.sleep(0.005)
            yield response("x", finish="STOP" if index == 29 else None)

    adapter.client.aio.models.generate_content_stream.side_effect = stream
    await generate(adapter)
    assert "".join(deltas) == "x" * 30
    # All 30 characters fit in one size-bounded delta. Repeated flushes must
    # happen while chunks keep arriving faster than the wait's timer tick.
    assert len(deltas) >= 4


async def test_final_usage_is_available_on_output_limit_for_safe_settlement():
    adapter = adapter_for([response(finish="MAX_TOKENS")])
    with pytest.raises(ModelRequestFailure) as caught:
        await generate(adapter)
    assert caught.value.usage_complete
    assert caught.value.cost_usd is not None and caught.value.cost_usd > 0


async def test_preflight_cache_keys_include_model_tools_and_native_history_and_are_bounded():
    adapter = adapter_for([response()], model_token_cache_size=2)
    task = Task(spec=TaskSpec(prompt="Hi"))
    history = [{"role": "user", "parts": [{"text": "Hi"}]}]
    tools = [{"name": "read_file", "parameters": {"type": "object"}}]
    assert await adapter.count_context_tokens(task, history, tools) == 100
    assert await adapter.count_context_tokens(task, history, tools) == 100
    assert adapter.client.aio.models.count_tokens.await_count == 1
    await adapter.count_context_tokens(task, history, [])
    await adapter.count_context_tokens(
        task.model_copy(update={"spec": task.spec.model_copy(update={"model": "other"})}),
        history,
        [],
    )
    await adapter.count_context_tokens(task, history, tools)
    assert adapter.client.aio.models.count_tokens.await_count == 4


async def test_estimate_uses_prepared_input_then_generation_reuses_verified_count():
    adapter = adapter_for([response()])
    task = Task(spec=TaskSpec(prompt="Hi"))
    history = [{"role": "user", "parts": [{"text": "Hi"}]}]
    cost = await adapter.estimate_call_cost(task, history, [])
    assert cost < adapter.estimated_call_cost
    await adapter.generate(task, history, [])
    assert adapter.client.aio.models.count_tokens.await_count == 1


async def test_changed_input_is_never_covered_by_a_previous_cached_count():
    adapter = adapter_for([response()])
    await generate(adapter, "Hello")
    adapter.client.aio.models.count_tokens.return_value = types.CountTokensResponse(
        total_tokens=65536
    )
    with pytest.raises(BudgetExceeded):
        await generate(adapter, "Changed input")
    assert adapter.client.aio.models.generate_content_stream.await_count == 1


async def test_short_initial_text_and_buffered_text_flush_before_stream_finishes():
    observed: list[str] = []
    buffered = asyncio.Event()
    release = asyncio.Event()
    adapter = adapter_for([], model_stream_flush_seconds=0.01)

    def on_text(task: Task, text: str) -> None:
        observed.append(text)
        if text == "second":
            buffered.set()

    adapter.on_text = on_text

    async def stream(**kwargs: object) -> AsyncIterator[types.GenerateContentResponse]:
        yield response("first", finish=None)
        assert observed == ["first"]
        yield response("second", finish=None)
        await release.wait()
        yield response("third")

    adapter.client.aio.models.generate_content_stream.side_effect = stream
    task = asyncio.create_task(generate(adapter))
    try:
        await asyncio.wait_for(buffered.wait(), 1)
        assert not task.done()
        release.set()
        await task
        assert "".join(observed) == "firstsecondthird"
    finally:
        release.set()
        task.cancel()


async def test_first_chunk_timeout_is_bounded_and_does_not_retry_unknown_outcome():
    adapter = adapter_for([], model_first_chunk_timeout_seconds=0.01)
    closed = asyncio.Event()

    async def stream(**kwargs: object) -> AsyncIterator[types.GenerateContentResponse]:
        try:
            await asyncio.Event().wait()
            yield response()
        finally:
            closed.set()

    adapter.client.aio.models.generate_content_stream.side_effect = stream
    with pytest.raises(ModelRequestFailure) as caught:
        await generate(adapter)
    assert caught.value.reason == "model_first_chunk_timeout"
    assert caught.value.request_started
    assert caught.value.cost_usd is None
    assert closed.is_set()
    assert adapter.client.aio.models.generate_content_stream.await_count == 1


async def test_idle_timeout_closes_stream_without_replaying_partial_text():
    adapter = adapter_for([], model_idle_timeout_seconds=0.02, model_stream_flush_seconds=0.005)
    observed: list[str] = []
    closed = asyncio.Event()
    adapter.on_text = lambda task, text: observed.append(text)

    async def stream(**kwargs: object) -> AsyncIterator[types.GenerateContentResponse]:
        try:
            yield response("partial", finish=None)
            await asyncio.Event().wait()
        finally:
            closed.set()

    adapter.client.aio.models.generate_content_stream.side_effect = stream
    with pytest.raises(ModelRequestFailure) as caught:
        await generate(adapter)
    assert caught.value.reason == "model_idle_timeout"
    assert not caught.value.usage_complete
    assert observed == ["partial"]
    assert closed.is_set()
    assert adapter.client.aio.models.generate_content_stream.await_count == 1


async def test_total_deadline_covers_preflight_and_marks_generation_not_started():
    adapter = adapter_for([], model_total_timeout_seconds=0.01)

    async def count(**kwargs: object) -> object:
        await asyncio.Event().wait()

    adapter.client.aio.models.count_tokens.side_effect = count
    with pytest.raises(ModelRequestFailure) as caught:
        await generate(adapter)
    assert caught.value.reason == "model_deadline"
    assert not caught.value.request_started
    assert adapter.client.aio.models.generate_content_stream.await_count == 0


async def test_external_cancellation_is_not_reclassified_as_a_provider_failure():
    adapter = adapter_for([])
    entered = asyncio.Event()

    async def count(**kwargs: object) -> object:
        entered.set()
        await asyncio.Event().wait()

    adapter.client.aio.models.count_tokens.side_effect = count
    task = asyncio.create_task(generate(adapter))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


def test_retry_after_is_honored_or_left_for_operator_when_too_long():
    import httpx
    from google.genai.errors import APIError

    for header, expected in (("12", 12), ("999999", None), ("not-a-date", 1)):
        error = APIError(
            429,
            {"error": {"code": 429, "message": "private-provider-payload"}},
            response=httpx.Response(429, headers={"Retry-After": header}),
        )
        delay = _retry_delay(error, 1, jitter=0)
        assert delay == expected
