"""Langfuse tracing wrapper for the migration agent (Phase 5).

All tracing is opt-in: if LANGFUSE_PUBLIC_KEY and LANGFUSE_SECRET_KEY are not
set, every function in this module is a no-op and returns None / empty strings.
This means Phase 5 can be deployed without requiring Langfuse credentials.

Usage:
    trace = init_trace(run_id, "java_to_kotlin migration")
    log_span(trace, "worker", input={"path": "..."}, output="diff...", cost_usd=0.02)
    url = get_trace_url(trace)
"""

from __future__ import annotations

import logging
import os
from typing import Any

log = logging.getLogger(__name__)

# Module-level Langfuse client — initialised lazily on first use
_langfuse_client: Any | None = None
_client_initialised = False


def _get_client() -> Any | None:
    global _langfuse_client, _client_initialised
    if _client_initialised:
        return _langfuse_client

    _client_initialised = True
    public_key = os.environ.get("LANGFUSE_PUBLIC_KEY", "")
    secret_key = os.environ.get("LANGFUSE_SECRET_KEY", "")
    host = os.environ.get("LANGFUSE_HOST", "https://cloud.langfuse.com")

    if not public_key or not secret_key:
        log.debug("Langfuse keys not set — tracing disabled")
        return None

    try:
        from langfuse import Langfuse
        _langfuse_client = Langfuse(
            public_key=public_key,
            secret_key=secret_key,
            host=host,
        )
        log.info("Langfuse tracing enabled (host=%s)", host)
    except ImportError:
        log.warning("langfuse package not installed — tracing disabled. Run: pip install langfuse")
    except Exception as exc:
        log.warning("Langfuse init failed: %s — tracing disabled", exc)

    return _langfuse_client


class _NoopTrace:
    """Returned when Langfuse is not configured."""
    id: str = ""

    def span(self, *args: Any, **kwargs: Any) -> "_NoopSpan":
        return _NoopSpan()

    def update(self, *args: Any, **kwargs: Any) -> None:
        pass


class _NoopSpan:
    def end(self, *args: Any, **kwargs: Any) -> None:
        pass


def init_trace(run_id: str, name: str, metadata: dict | None = None) -> Any:
    """Create and return a Langfuse trace for this run.

    Returns a real Langfuse StatefulTraceClient if configured, or a _NoopTrace
    that silently swallows all calls.
    """
    client = _get_client()
    if client is None:
        return _NoopTrace()

    try:
        trace = client.trace(
            id=run_id,
            name=name,
            metadata=metadata or {},
        )
        log.info("Langfuse trace created: %s", run_id)
        return trace
    except Exception as exc:
        log.warning("Langfuse trace init failed: %s", exc)
        return _NoopTrace()


def log_span(
    trace: Any,
    name: str,
    input_data: dict | None = None,
    output: str = "",
    cost_usd: float = 0.0,
    metadata: dict | None = None,
) -> None:
    """Log a named span to the trace. No-op if trace is a _NoopTrace or Langfuse fails."""
    if isinstance(trace, _NoopTrace):
        return
    try:
        span = trace.span(
            name=name,
            input=input_data or {},
            output=output,
            metadata={**(metadata or {}), "cost_usd": cost_usd},
        )
        span.end()
    except Exception as exc:
        log.debug("Langfuse log_span failed: %s", exc)


def get_trace_url(trace: Any) -> str:
    """Return the public URL for this trace, or '' if not available."""
    if isinstance(trace, _NoopTrace):
        return ""
    try:
        client = _get_client()
        if client is None:
            return ""
        host = os.environ.get("LANGFUSE_HOST", "https://cloud.langfuse.com").rstrip("/")
        trace_id = getattr(trace, "id", None) or getattr(trace, "trace_id", None)
        if trace_id:
            return f"{host}/trace/{trace_id}"
    except Exception as exc:
        log.debug("get_trace_url failed: %s", exc)
    return ""


def flush() -> None:
    """Flush all pending Langfuse events. Call at process exit or end of run."""
    client = _get_client()
    if client is None:
        return
    try:
        client.flush()
    except Exception as exc:
        log.debug("Langfuse flush failed: %s", exc)
