import unittest
from types import SimpleNamespace
from unittest.mock import patch

from openai import OpenAIError

from src.infrastructure.retry import request_with_retry


class StatusError(OpenAIError):
    def __init__(self, status_code: int, headers: dict[str, str] | None = None):
        super().__init__(f"status {status_code}")
        self.status_code = status_code
        self.response = SimpleNamespace(
            status_code=status_code,
            headers=headers or {},
        )


class RequestRetryTests(unittest.TestCase):
    def test_429_honors_retry_after(self) -> None:
        responses = iter([StatusError(429, {"Retry-After": "2.5"}), "ok"])

        def request():
            response = next(responses)
            if isinstance(response, Exception):
                raise response
            return response

        with patch("src.infrastructure.retry.time.sleep") as sleep:
            result = request_with_retry(request)

        self.assertEqual(result, "ok")
        sleep.assert_called_once_with(2.5)

    def test_529_uses_exponential_backoff_when_retry_after_is_missing(self):
        responses = iter([
            StatusError(529),
            StatusError(529),
            StatusError(529),
            "ok",
        ])

        def request():
            response = next(responses)
            if isinstance(response, Exception):
                raise response
            return response

        with (
            patch("src.infrastructure.retry.time.sleep") as sleep,
            patch("src.infrastructure.retry.random.uniform", return_value=0),
        ):
            result = request_with_retry(request)

        self.assertEqual(result, "ok")
        self.assertEqual(
            [call.args[0] for call in sleep.call_args_list],
            [1.0, 2.0, 4.0],
        )

    def test_non_retryable_status_is_raised_without_retrying(self) -> None:
        error = StatusError(500)

        def request():
            raise error

        with (
            patch("src.infrastructure.retry.time.sleep") as sleep,
            self.assertRaises(StatusError),
        ):
            request_with_retry(request)

        sleep.assert_not_called()

    def test_stops_after_three_retries(self) -> None:
        calls = 0

        def request():
            nonlocal calls
            calls += 1
            raise StatusError(429)

        with (
            patch("src.infrastructure.retry.time.sleep"),
            patch("src.infrastructure.retry.random.uniform", return_value=0),
            self.assertRaises(StatusError),
        ):
            request_with_retry(request)

        self.assertEqual(calls, 4)


if __name__ == "__main__":
    unittest.main()
