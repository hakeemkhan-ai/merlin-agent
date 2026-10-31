"""Safe model diagnostics and bounded request accounting shared by adapters."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from merlin.contracts import ModelServiceError

FailureReason = Literal[
    "model_first_chunk_timeout",
    "model_idle_timeout",
    "model_deadline",
    "model_output_limit",
    "model_blocked",
    "model_invalid_response",
    "model_incomplete_response",
    "model_connection",
    "model_http_error",
    "model_preflight_failed",
]

MESSAGES: dict[FailureReason, str] = {
    "model_first_chunk_timeout": "Gemini connection timed out before its first response.",
    "model_idle_timeout": "Gemini connection stopped producing response chunks.",
    "model_deadline": "Gemini request exceeded its total time allowance.",
    "model_output_limit": "Gemini reached its output limit before completing the response.",
    "model_blocked": "Gemini blocked this response; no proposed tools were executed.",
    "model_invalid_response": (
        "Gemini returned an invalid response; no proposed tools were executed."
    ),
    "model_incomplete_response": "Gemini response was incomplete; no proposed tools were executed.",
    "model_connection": "Gemini connection was interrupted.",
    "model_http_error": "Gemini rejected the request.",
    "model_preflight_failed": "Gemini did not return a valid input token count.",
}


@dataclass
class RequestAccounting:
    """Per-call state; never share mutable request state between worker consumers."""

    request_started: bool = False
    generation_attempts: int = 0
    input_tokens: int = 0
    cached_input_tokens: int = 0
    thinking_tokens: int = 0
    candidate_tokens: int = 0
    usage_reported: bool = False
    final_usage_reported: bool = False
    provider_response_id: str | None = None

    @property
    def output_tokens(self) -> int:
        return self.candidate_tokens + self.thinking_tokens


class ModelRequestFailure(ModelServiceError):
    """A static public error with explicit billing uncertainty, never provider text.

    ``request_started=False`` is the only automatic reservation-release signal.
    A reported final usage amount can be settled even when generation was blocked
    or reached a limit. Transport failures retain the reservation conservatively.
    """

    def __init__(
        self,
        reason: FailureReason,
        accounting: RequestAccounting,
        *,
        retryable: bool = True,
        status_code: int | None = None,
        cost_usd: float | None = None,
    ) -> None:
        super().__init__(status_code)
        self.reason = reason
        self.retryable = retryable
        self.request_started = accounting.request_started
        self.generation_attempts = accounting.generation_attempts
        self.input_tokens = accounting.input_tokens
        self.output_tokens = accounting.output_tokens
        self.cached_input_tokens = accounting.cached_input_tokens
        self.thinking_tokens = accounting.thinking_tokens
        self.usage_complete = accounting.final_usage_reported and cost_usd is not None
        self.cost_usd = cost_usd if self.usage_complete else None
        message = MESSAGES[reason]
        if status_code is not None:
            # Include only a validated status, never a response body or URL.
            if type(status_code) is int and 400 <= status_code <= 599:
                message += f" (HTTP {status_code})."
        if retryable:
            message += " Task history is saved; inspect it before using /resume."
        RuntimeError.__init__(self, message)
