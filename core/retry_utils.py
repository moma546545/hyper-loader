"""
core/retry_utils.py - Shared retry classification and backoff helpers.
"""
import socket
import time
import urllib.error


RETRYABLE_TEXT_TOKENS = (
    "timed out",
    "timeout",
    "time out",
    "429",
    "too many requests",
    "rate limit",
    "tempor",
    "try again",
    "connection reset",
    "connection aborted",
    "network is unreachable",
    "remote end closed",
    "refused",
    "unreachable",
    "tls",
    "ssl",
    "handshake",
    "read timed out",
)


def is_retryable_error_text(text: str, extra_tokens=()) -> bool:
    normalized = str(text or "").strip().lower()
    if not normalized:
        return False
    tokens = tuple(RETRYABLE_TEXT_TOKENS) + tuple(extra_tokens or ())
    return any(token in normalized for token in tokens)


def is_retryable_exception(
    exc: BaseException | None,
    *,
    timeout_checker=None,
    retryable_http_statuses=None,
) -> bool:
    if exc is None:
        return False
    if callable(timeout_checker) and timeout_checker(exc):
        return True
    statuses = set(retryable_http_statuses or {408, 425, 429, 500, 502, 503, 504})
    if isinstance(exc, urllib.error.HTTPError):
        return int(getattr(exc, "code", 0) or 0) in statuses
    if isinstance(exc, urllib.error.URLError):
        reason = getattr(exc, "reason", None)
        if callable(timeout_checker) and timeout_checker(reason):
            return True
        if isinstance(reason, (ConnectionError, OSError, socket.error)):
            return True
        return is_retryable_error_text(str(reason or exc))
    if isinstance(exc, (ConnectionError, OSError, socket.error)):
        return True
    return is_retryable_error_text(str(exc))


def run_with_retries(
    operation_name: str,
    action,
    retry_delays,
    *,
    should_retry_exception,
    logger,
    sleep_func=time.sleep,
    should_abort=None,
    abort_error_factory=None,
    sleep_quantum_seconds: float = 0.1,
):
    def _raise_abort():
        if callable(abort_error_factory):
            raise abort_error_factory()
        raise InterruptedError(f"{operation_name} cancelled")

    def _sleep_with_abort(wait_seconds: float):
        remaining = max(0.0, float(wait_seconds))
        if not callable(should_abort):
            sleep_func(remaining)
            return
        quantum = max(0.01, float(sleep_quantum_seconds or 0.1))
        while remaining > 0:
            if should_abort():
                _raise_abort()
            step = min(quantum, remaining)
            sleep_func(step)
            remaining -= step
        if should_abort():
            _raise_abort()

    delays = tuple(retry_delays or ())
    total_attempts = 1 + max(0, len(delays))
    last_exc = None
    for attempt_index in range(total_attempts):
        if callable(should_abort) and should_abort():
            _raise_abort()
        try:
            return action(attempt_index + 1, total_attempts)
        except Exception as exc:
            last_exc = exc
            if attempt_index >= (total_attempts - 1) or not bool(should_retry_exception(exc)):
                raise
            wait_seconds = float(delays[attempt_index])
            logger.warning(
                "[%s] attempt %s/%s failed: %s. Retrying in %.1fs",
                operation_name,
                attempt_index + 1,
                total_attempts,
                exc,
                wait_seconds,
            )
            _sleep_with_abort(wait_seconds)
    if last_exc is not None:
        raise last_exc
    raise RuntimeError(f"{operation_name} failed")
