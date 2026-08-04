import random
import time
from collections.abc import Callable
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any

from openai import OpenAIError


RETRYABLE_STATUS_CODES = frozenset({429, 529})
MAX_RETRIES = 3
INITIAL_BACKOFF_SECONDS = 1.0
MAX_BACKOFF_SECONDS = 30.0
BACKOFF_JITTER_RATIO = 0.1


def request_with_retry(request: Callable[[], Any]) -> Any:
    """Run a model request, retrying only rate-limit and overload errors."""
    for retry_index in range(MAX_RETRIES + 1):
        try:
            return request()
        except OpenAIError as exc:
            if not _is_retryable(exc) or retry_index >= MAX_RETRIES:
                raise

            delay = _retry_after_seconds(exc)
            if delay is None:
                backoff = min(
                    MAX_BACKOFF_SECONDS,
                    INITIAL_BACKOFF_SECONDS * (2 ** retry_index),
                )
                jitter = random.uniform(0, backoff * BACKOFF_JITTER_RATIO)
                delay = min(MAX_BACKOFF_SECONDS, backoff + jitter)
            else:
                delay = min(MAX_BACKOFF_SECONDS, delay)

            time.sleep(delay)

    raise AssertionError("unreachable")


def _is_retryable(exc: OpenAIError) -> bool:
    return _status_code(exc) in RETRYABLE_STATUS_CODES


def _status_code(exc: OpenAIError) -> int | None:
    status_code = getattr(exc, "status_code", None)
    if isinstance(status_code, int):
        return status_code

    response = getattr(exc, "response", None)
    status_code = getattr(response, "status_code", None)
    return status_code if isinstance(status_code, int) else None


def _retry_after_seconds(exc: OpenAIError) -> float | None:
    headers = getattr(exc, "headers", None)
    if headers is None:
        response = getattr(exc, "response", None)
        headers = getattr(response, "headers", None)
    if headers is None:
        return None

    value = None
    for name, header_value in headers.items():
        if str(name).lower() == "retry-after":
            value = str(header_value).strip()
            break
    if not value:
        return None

    try:
        return max(0.0, float(value))
    except ValueError:
        pass

    try:
        retry_at = parsedate_to_datetime(value)
    except (TypeError, ValueError, OverflowError):
        return None

    if retry_at.tzinfo is None:
        retry_at = retry_at.replace(tzinfo=timezone.utc)
    return max(0.0, (retry_at - datetime.now(timezone.utc)).total_seconds())
