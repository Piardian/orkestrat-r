from __future__ import annotations

from contextlib import contextmanager, nullcontext
import os
import sys
from typing import Any, Iterator


_HEALTH: dict[str, dict[str, Any]] = {
    "sentry": {"configured": False, "healthy": False, "error": None},
    "langfuse": {"configured": False, "healthy": False, "error": None},
    "opentelemetry": {"configured": False, "healthy": False, "error": None},
}
_OTEL_TRACER = None


def _truthy(name: str, default: str = "false") -> bool:
    return os.getenv(name, default).strip().lower() in {"1", "true", "yes", "on"}


def _mark(component: str, *, configured: bool, healthy: bool, error: Exception | str | None = None) -> None:
    _HEALTH[component] = {
        "configured": configured,
        "healthy": healthy,
        "error": None if error is None else str(error),
    }
    if configured and not healthy and error is not None:
        print(f"OBSERVABILITY_WARNING component={component}: {error}", file=sys.stderr)


def init_sentry() -> bool:
    dsn = os.getenv("SENTRY_DSN", "").strip()
    enabled = _truthy("AGENT_ARMY_SENTRY_ENABLED")
    configured = bool(dsn and enabled)
    if not configured:
        _mark("sentry", configured=False, healthy=False)
        return False
    try:
        import sentry_sdk

        sentry_sdk.init(
            dsn=dsn,
            environment=os.getenv("AGENT_ARMY_ENV", "local"),
            traces_sample_rate=float(os.getenv("SENTRY_TRACES_SAMPLE_RATE", "0.1")),
            send_default_pii=False,
        )
        _mark("sentry", configured=True, healthy=True)
        return True
    except Exception as exc:
        _mark("sentry", configured=True, healthy=False, error=exc)
        return False


def init_opentelemetry() -> bool:
    global _OTEL_TRACER
    enabled = _truthy("AGENT_ARMY_OTEL_ENABLED")
    endpoint = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", "").strip()
    configured = bool(enabled and endpoint)
    if not configured:
        _mark("opentelemetry", configured=False, healthy=False)
        return False
    try:
        from opentelemetry import trace
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor

        current = trace.get_tracer_provider()
        if current.__class__.__module__.startswith("opentelemetry.sdk"):
            provider = current
        else:
            resource = Resource.create(
                {
                    "service.name": os.getenv("OTEL_SERVICE_NAME", "agent-army"),
                    "deployment.environment": os.getenv("AGENT_ARMY_ENV", "local"),
                }
            )
            provider = TracerProvider(resource=resource)
            trace.set_tracer_provider(provider)

        exporter_endpoint = endpoint.rstrip("/")
        if not exporter_endpoint.endswith("/v1/traces"):
            exporter_endpoint += "/v1/traces"
        processor = BatchSpanProcessor(OTLPSpanExporter(endpoint=exporter_endpoint))
        try:
            provider.add_span_processor(processor)
        except AttributeError:
            # A non-SDK provider can exist when a host application already owns OTel.
            pass
        _OTEL_TRACER = trace.get_tracer("agent-army")
        _mark("opentelemetry", configured=True, healthy=True)
        return True
    except Exception as exc:
        _OTEL_TRACER = None
        _mark("opentelemetry", configured=True, healthy=False, error=exc)
        return False


def init_langfuse() -> bool:
    enabled = _truthy("AGENT_ARMY_LANGFUSE_ENABLED")
    configured = bool(
        enabled
        and os.getenv("LANGFUSE_PUBLIC_KEY", "").strip()
        and os.getenv("LANGFUSE_SECRET_KEY", "").strip()
    )
    if not configured:
        _mark("langfuse", configured=False, healthy=False)
        return False
    try:
        from langfuse import get_client

        get_client()
        _mark("langfuse", configured=True, healthy=True)
        return True
    except Exception as exc:
        _mark("langfuse", configured=True, healthy=False, error=exc)
        return False


def init_observability() -> dict[str, dict[str, Any]]:
    init_sentry()
    init_opentelemetry()
    init_langfuse()
    return observability_health()


def observability_health() -> dict[str, dict[str, Any]]:
    return {name: dict(value) for name, value in _HEALTH.items()}


def capture_exception(exc: BaseException, **context: Any) -> None:
    if _OTEL_TRACER is not None:
        try:
            from opentelemetry.trace import Status, StatusCode

            with _OTEL_TRACER.start_as_current_span("agent-army.exception") as span:
                span.record_exception(exc)
                span.set_status(Status(StatusCode.ERROR, str(exc)))
                for key, value in context.items():
                    span.set_attribute(f"agent_army.{key}", str(value))
        except Exception as otel_exc:
            _mark("opentelemetry", configured=True, healthy=False, error=otel_exc)

    if _HEALTH["sentry"].get("healthy"):
        try:
            import sentry_sdk

            with sentry_sdk.push_scope() as scope:
                for key, value in context.items():
                    scope.set_extra(key, value)
                sentry_sdk.capture_exception(exc)
        except Exception as sentry_exc:
            _mark("sentry", configured=True, healthy=False, error=sentry_exc)


@contextmanager
def observe_run(name: str, *, metadata: dict[str, Any] | None = None) -> Iterator[None]:
    langfuse_client = None
    langfuse_context = nullcontext(None)
    otel_context = nullcontext(None)

    if _truthy("AGENT_ARMY_LANGFUSE_ENABLED"):
        try:
            from langfuse import get_client

            langfuse_client = get_client()
            langfuse_context = langfuse_client.start_as_current_observation(as_type="agent", name=name)
            _mark("langfuse", configured=True, healthy=True)
        except Exception as exc:
            langfuse_client = None
            langfuse_context = nullcontext(None)
            _mark("langfuse", configured=True, healthy=False, error=exc)

    if _OTEL_TRACER is not None:
        otel_context = _OTEL_TRACER.start_as_current_span(name)

    try:
        with otel_context as otel_span:
            if otel_span is not None and metadata:
                try:
                    for key, value in metadata.items():
                        otel_span.set_attribute(f"agent_army.{key}", str(value))
                except Exception as exc:
                    _mark("opentelemetry", configured=True, healthy=False, error=exc)
            with langfuse_context as observation:
                if observation is not None and metadata:
                    try:
                        observation.update(metadata=metadata)
                    except Exception as exc:
                        _mark("langfuse", configured=True, healthy=False, error=exc)
                yield
    finally:
        if langfuse_client is not None:
            try:
                langfuse_client.flush()
            except Exception as exc:
                _mark("langfuse", configured=True, healthy=False, error=exc)
