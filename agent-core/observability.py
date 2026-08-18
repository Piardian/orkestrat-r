from __future__ import annotations

from contextlib import contextmanager, nullcontext
import os
from typing import Any, Iterator


def _truthy(name: str, default: str = "false") -> bool:
    return os.getenv(name, default).strip().lower() in {"1", "true", "yes", "on"}


def init_sentry() -> bool:
    dsn = os.getenv("SENTRY_DSN", "").strip()
    if not dsn or not _truthy("AGENT_ARMY_SENTRY_ENABLED"):
        return False
    try:
        import sentry_sdk

        sentry_sdk.init(
            dsn=dsn,
            environment=os.getenv("AGENT_ARMY_ENV", "local"),
            traces_sample_rate=float(os.getenv("SENTRY_TRACES_SAMPLE_RATE", "0.1")),
            send_default_pii=False,
        )
        return True
    except Exception:
        return False


def capture_exception(exc: BaseException, **context: Any) -> None:
    try:
        import sentry_sdk

        with sentry_sdk.push_scope() as scope:
            for key, value in context.items():
                scope.set_extra(key, value)
            sentry_sdk.capture_exception(exc)
    except Exception:
        pass


@contextmanager
def observe_run(name: str, *, metadata: dict[str, Any] | None = None) -> Iterator[None]:
    client = None
    context = nullcontext(None)

    if _truthy("AGENT_ARMY_LANGFUSE_ENABLED"):
        try:
            from langfuse import get_client

            client = get_client()
            context = client.start_as_current_observation(as_type="agent", name=name)
        except Exception:
            client = None
            context = nullcontext(None)

    try:
        with context as observation:
            if observation is not None and metadata:
                try:
                    observation.update(metadata=metadata)
                except Exception:
                    pass
            yield
    finally:
        if client is not None:
            try:
                client.flush()
            except Exception:
                pass
