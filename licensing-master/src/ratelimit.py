"""Small in-memory failure limiter (audit A3).

Counts FAILED attempts per key (client IP) inside a sliding window; reaching
``max_failures`` locks the key out for ``lockout_seconds``. State lives in this
process only, which is correct for the current single-process deployment; a
multi-worker deployment would need a shared store (e.g. Redis).
"""

from __future__ import annotations

import math
import threading
import time
from collections import deque
from collections.abc import Callable

from src.config import settings


class FailureLimiter:
    def __init__(
        self,
        max_failures: int,
        window_seconds: int,
        lockout_seconds: int,
        *,
        clock: Callable[[], float] = time.monotonic,
        max_keys: int = 10_000,
    ) -> None:
        if max_failures < 1 or window_seconds < 1 or lockout_seconds < 1:
            raise ValueError("max_failures, window_seconds and lockout_seconds must be >= 1")
        self.max_failures = max_failures
        self.window_seconds = window_seconds
        self.lockout_seconds = lockout_seconds
        self._clock = clock
        self._max_keys = max_keys
        self._failures: dict[str, deque[float]] = {}
        self._locked_until: dict[str, float] = {}
        self._lock = threading.Lock()

    def retry_after(self, key: str) -> int:
        """Seconds until ``key`` may try again; 0 when it is not locked out."""
        with self._lock:
            until = self._locked_until.get(key)
            if until is None:
                return 0
            remaining = until - self._clock()
            if remaining <= 0:
                del self._locked_until[key]
                self._failures.pop(key, None)
                return 0
            return math.ceil(remaining)

    def record_failure(self, key: str) -> None:
        with self._lock:
            now = self._clock()
            if len(self._failures) >= self._max_keys and key not in self._failures:
                self._prune(now)
            hits = self._failures.setdefault(key, deque())
            hits.append(now)
            cutoff = now - self.window_seconds
            while hits and hits[0] <= cutoff:
                hits.popleft()
            if len(hits) >= self.max_failures:
                self._locked_until[key] = now + self.lockout_seconds

    def clear(self, key: str) -> None:
        """Forget ``key`` entirely (e.g. after a fully successful login)."""
        with self._lock:
            self._failures.pop(key, None)
            self._locked_until.pop(key, None)

    def reset(self) -> None:
        with self._lock:
            self._failures.clear()
            self._locked_until.clear()

    def tracked_keys(self) -> int:
        with self._lock:
            return len(self._failures)

    def _prune(self, now: float) -> None:
        """Drop expired entries; if still over capacity, evict the oldest keys."""
        cutoff = now - self.window_seconds
        for k in [k for k, h in self._failures.items() if not h or h[-1] <= cutoff]:
            if self._locked_until.get(k, 0) <= now:
                self._failures.pop(k, None)
                self._locked_until.pop(k, None)
        while len(self._failures) >= self._max_keys:
            oldest = next(iter(self._failures))
            self._failures.pop(oldest)
            self._locked_until.pop(oldest, None)


activation_limiter = FailureLimiter(
    settings.LM_ACTIVATE_MAX_FAILURES,
    settings.LM_ACTIVATE_FAILURE_WINDOW_SECONDS,
    settings.LM_ACTIVATE_LOCKOUT_SECONDS,
)
service_auth_limiter = FailureLimiter(
    settings.LM_CP_AUTH_MAX_FAILURES,
    settings.LM_CP_AUTH_FAILURE_WINDOW_SECONDS,
    settings.LM_CP_AUTH_LOCKOUT_SECONDS,
)
# Admin login: per typed e-mail (also for unknown e-mails, so lockout reveals
# nothing) and per client IP. Also used for wrong re-entered passwords.
login_email_limiter = FailureLimiter(
    settings.LM_ADMIN_LOGIN_EMAIL_MAX_FAILURES,
    settings.LM_ADMIN_LOGIN_EMAIL_WINDOW_SECONDS,
    settings.LM_ADMIN_LOGIN_EMAIL_LOCKOUT_SECONDS,
)
login_ip_limiter = FailureLimiter(
    settings.LM_ADMIN_LOGIN_IP_MAX_FAILURES,
    settings.LM_ADMIN_LOGIN_IP_WINDOW_SECONDS,
    settings.LM_ADMIN_LOGIN_IP_LOCKOUT_SECONDS,
)


def reset_all() -> None:
    """Test helper: also restores the configured thresholds."""
    activation_limiter.reset()
    service_auth_limiter.reset()
    login_email_limiter.reset()
    login_ip_limiter.reset()
    login_email_limiter.max_failures = settings.LM_ADMIN_LOGIN_EMAIL_MAX_FAILURES
    login_ip_limiter.max_failures = settings.LM_ADMIN_LOGIN_IP_MAX_FAILURES
    activation_limiter.max_failures = settings.LM_ACTIVATE_MAX_FAILURES
    service_auth_limiter.max_failures = settings.LM_CP_AUTH_MAX_FAILURES


def client_ip(request) -> str:
    """The caller's IP. Behind Traefik, uvicorn's ProxyHeadersMiddleware
    (``--proxy-headers`` + ``--forwarded-allow-ips``, see entrypoint.sh) has
    already replaced ``request.client`` with the real client address, so this
    never reads X-Forwarded-For itself."""
    return request.client.host if request.client else "unknown"
