"""Regression tests for terminal Codex Responses context-overflow recovery."""

from types import SimpleNamespace

import pytest

from agent.conversation_loop import (
    _CodexTerminalResponseError,
    _codex_terminal_context_error,
)
from agent.error_classifier import FailoverReason, classify_api_error


def test_terminal_codex_context_failure_requests_compression():
    """A terminal context failure must enter compression recovery, not invalid-response retry."""
    response = SimpleNamespace(
        status="failed",
        error=SimpleNamespace(
            code="context_length_exceeded",
            message="The request exceeds the model context window.",
        ),
    )

    error = _codex_terminal_context_error(response)

    assert error is not None
    classified = classify_api_error(error)
    assert classified.reason is FailoverReason.context_overflow
    assert classified.retryable is True
    assert classified.should_compress is True


def test_max_tokens_exceeded_is_not_destructive_context_recovery():
    """An output-limit terminal code must fail over without compressing valid input history."""
    response = SimpleNamespace(
        status="failed",
        error=SimpleNamespace(
            code="max_tokens_exceeded",
            message="The response reached the model output token limit.",
        ),
    )

    assert _codex_terminal_context_error(response) is None

    classified = classify_api_error(
        _CodexTerminalResponseError(
            "The response reached the model output token limit.",
            code="max_tokens_exceeded",
        )
    )
    assert classified.reason is FailoverReason.format_error
    assert classified.retryable is False
    assert classified.should_compress is False
    assert classified.should_fallback is True


def test_structured_terminal_code_survives_message_formatter_changes():
    """Recovery must use the machine code rather than depend on display wording."""
    error = _CodexTerminalResponseError(
        "synthetic terminal failure",
        code="context_length_exceeded",
    )

    classified = classify_api_error(error)

    assert classified.reason is FailoverReason.context_overflow
    assert classified.should_compress is True


def test_message_context_marker_overrides_generic_terminal_code():
    """A wrapper code must not suppress a context marker that requires compression."""
    response = SimpleNamespace(
        status="failed",
        error=SimpleNamespace(
            code="max_tokens_exceeded",
            message="provider wrapper: context_length_exceeded while encoding input",
        ),
    )

    error = _codex_terminal_context_error(response)

    assert error is not None
    assert error.code == "context_length_exceeded"
    classified = classify_api_error(error)
    assert classified.reason is FailoverReason.context_overflow
    assert classified.should_compress is True


@pytest.mark.parametrize(
    "status,code",
    [
        ("failed", "invalid_api_key"),
        ("failed", "rate_limit_exceeded"),
        ("completed", "context_length_exceeded"),
        ("cancelled", "context_length_exceeded"),
    ],
)
def test_non_overflow_terminal_responses_do_not_request_compaction(status, code):
    """Unrelated failures and completed responses must stay out of the destructive compaction path."""
    response = SimpleNamespace(
        status=status,
        error=SimpleNamespace(code=code, message="synthetic provider response"),
    )

    assert _codex_terminal_context_error(response) is None
