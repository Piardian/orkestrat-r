from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from hashlib import sha256
from threading import Lock
from time import monotonic, sleep
from typing import Callable


@dataclass(frozen=True)
class BuilderRateLimitConfig:
    enabled: bool = True
    requests_per_minute: int = 20
    safety_margin: int = 1
    window_seconds: int = 60
    retry_attempts: int = 3
    retry_backoff_seconds: tuple[float, ...] = (5.0, 15.0, 30.0)

    @property
    def effective_limit(self) -> int:
        return max(1, int(self.requests_per_minute) - max(0, int(self.safety_margin)))


@dataclass(frozen=True)
class BuilderRateLimitObservation:
    scope_id: str
    profile_id: str
    provider: str
    model: str
    request_count: int
    effective_limit: int
    wait_seconds: float


class BuilderRateLimiter:
    def __init__(
        self,
        config: BuilderRateLimitConfig | None = None,
        *,
        clock: Callable[[], float] = monotonic,
        sleeper: Callable[[float], None] = sleep,
    ) -> None:
        self.config = config or BuilderRateLimitConfig()
        self.clock = clock
        self.sleeper = sleeper
        self._lock = Lock()
        self._requests: dict[str, deque[float]] = {}

    def scope_id(self, *, profile_id: str, provider: str, model: str, api_key: str | None) -> str:
        digest_source = "|".join([profile_id, provider, model, api_key or ""])
        return sha256(digest_source.encode("utf-8")).hexdigest()[:24]

    def observe(self, scope_id: str, profile_id: str, provider: str, model: str) -> BuilderRateLimitObservation:
        with self._lock:
            request_count, wait_seconds = self._evaluate_locked(scope_id, reserve=False)
            return BuilderRateLimitObservation(
                scope_id=scope_id,
                profile_id=profile_id,
                provider=provider,
                model=model,
                request_count=request_count,
                effective_limit=self.config.effective_limit,
                wait_seconds=wait_seconds,
            )

    def reserve(self, scope_id: str) -> float:
        if not self.config.enabled:
            return 0.0
        with self._lock:
            request_count, wait_seconds = self._evaluate_locked(scope_id, reserve=True)
        if wait_seconds > 0:
            self.sleeper(wait_seconds)
        now = self.clock()
        with self._lock:
            bucket = self._requests.setdefault(scope_id, deque())
            self._drop_stale(bucket, now)
            bucket.append(now)
        return wait_seconds

    def record_result(self, scope_id: str) -> None:
        if not self.config.enabled:
            return
        now = self.clock()
        with self._lock:
            bucket = self._requests.setdefault(scope_id, deque())
            self._drop_stale(bucket, now)
            bucket.append(now)

    def clear(self, scope_id: str) -> None:
        with self._lock:
            self._requests.pop(scope_id, None)

    def _evaluate_locked(self, scope_id: str, reserve: bool) -> tuple[int, float]:
        now = self.clock()
        bucket = self._requests.setdefault(scope_id, deque())
        self._drop_stale(bucket, now)
        count = len(bucket)
        if count < self.config.effective_limit:
            return count, 0.0
        oldest = bucket[0]
        wait_seconds = max(0.0, oldest + self.config.window_seconds - now + 1.0)
        return count, wait_seconds

    def _drop_stale(self, bucket: deque[float], now: float) -> None:
        cutoff = now - self.config.window_seconds
        while bucket and bucket[0] <= cutoff:
            bucket.popleft()
