"""
MikAura Observability Utilities
===============================

Pipeline-agnostic structured logging and metrics for all MikMak pipelines.

Quick Start
-----------
    from utils.mikaura_observability import (
        MikAuraObservabilityConfig,
        MikAuraStatusLogger,
        MikAuraMetricLogger,
    )

    # Option A: shared config (recommended) — pipeline defines context keys
    config = MikAuraObservabilityConfig(
        context={
            "pipeline_context": "Data Ingestion Pipeline",
            "client_name": "madebygather",
            "brand_name": "bella-US",
            "retailer_name": "amazon",
            "country": "US",
        },
    )
    logger = MikAuraStatusLogger.from_config(config)
    metrics = MikAuraMetricLogger.from_config(config)

    # Log status
    logger.log_running("Starting process")
    logger.log_success("Process complete")

    # Scoped context: yields a NEW logger (original unchanged)
    with logger.with_context(retailer_name="walmart") as child:
        child.log_info("Processing walmart data")

    # Verbose structured debug (requires min_level=DEBUG on the logger)
    verbose = MikAuraStatusLogger(
        context={"pipeline_context": "Data Ingestion Pipeline"},
        min_level="DEBUG",
    )
    verbose.log_debug("File matching detail")

    # Option B: build logger directly
    logger = MikAuraStatusLogger(
        context={"pipeline_context": "Prediction Pipeline", "client_name": "client123"},
    )
"""

from __future__ import annotations

import json
import os
import random
import re
import sys
import warnings
import time
import traceback
import uuid
from collections import defaultdict
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, FrozenSet, Iterator, List, Optional, Set, Tuple, Union

# ---------------------------------------------------------------------------
# Country inference helper (optional; callers may use when building context)
# ---------------------------------------------------------------------------


def _infer_country_from_brand(brand_name: str) -> str:
    """Best-effort country extraction from brand names like 'bella-US'."""
    if not brand_name or brand_name == "unknown":
        return "unknown"
    if "-" in brand_name:
        suffix = brand_name.rsplit("-", 1)[-1].upper()
        if len(suffix) == 2 and suffix.isalpha():
            return suffix
    if "_" in brand_name:
        suffix = brand_name.rsplit("_", 1)[-1].upper()
        if len(suffix) == 2 and suffix.isalpha():
            return suffix
    return "unknown"


# ---------------------------------------------------------------------------
# Shared configuration
# ---------------------------------------------------------------------------


@dataclass
class MikAuraObservabilityConfig:
    """Shared configuration for both MikAuraStatusLogger and MikAuraMetricLogger.

    Pipelines supply arbitrary key-value pairs in ``context``; the only
    required key is ``pipeline_context`` (non-empty string).

    ``allowed_statuses`` is optional; when set, it is passed to the status logger.
    """

    context: Dict[str, Any]
    environment: str = ""
    correlation_id: str = ""
    allowed_statuses: Optional[Set[str]] = None

    def __post_init__(self) -> None:
        if not self.context:
            raise ValueError("context must not be empty")
        self.context = dict(self.context)
        pc = str(self.context.get("pipeline_context", "")).strip()
        if not pc:
            raise ValueError("context['pipeline_context'] is required and cannot be empty")
        self.environment = self.environment or os.getenv("ENVIRONMENT", "dev")
        self.correlation_id = self.correlation_id or str(uuid.uuid4())


# ---------------------------------------------------------------------------
# Redaction: compile once (default patterns)
# ---------------------------------------------------------------------------

_REDACT_SPECS: List[Tuple[str, str]] = [
    (r'password["\']?\s*[:=]\s*["\']?([^"\'\}\s]+)', "password=***"),
    (r'api[_\-]?key["\']?\s*[:=]\s*["\']?([^"\'\}\s]+)', "api_key=***"),
    (r'secret["\']?\s*[:=]\s*["\']?([^"\'\}\s]+)', "secret=***"),
]


def _compile_redact_patterns(specs: List[Tuple[str, str]]) -> List[Tuple[Any, str]]:
    return [(re.compile(p, re.IGNORECASE), r) for p, r in specs]


_DEFAULT_REDACT_COMPILED: List[Tuple[Any, str]] = _compile_redact_patterns(_REDACT_SPECS)


def _redact(message: str) -> str:
    """Apply default redaction (used by tests and legacy callers)."""
    for pattern, replacement in _DEFAULT_REDACT_COMPILED:
        message = pattern.sub(replacement, message)
    return message


# ---------------------------------------------------------------------------
# Log level constants
# ---------------------------------------------------------------------------

_LOG_LEVELS: Dict[str, int] = {
    "DEBUG": 10,
    "INFO": 20,
    "WARNING": 30,
    "ERROR": 40,
}

DEFAULT_STATUSES: FrozenSet[str] = frozenset({"running", "success", "failed", "warning", "info"})

# Keys that apply to MikAuraStatusLogger itself, not merged into context, in derive()
_STATUS_LOGGER_DERIVE_KEYS: FrozenSet[str] = frozenset(
    {
        "environment",
        "correlation_id",
        "sample_rate",
        "min_level",
        "allowed_statuses",
        "strict_validation",
        "redact_patterns",
        "extra_metadata",
    }
)

_METRIC_LOGGER_DERIVE_KEYS: FrozenSet[str] = frozenset(
    {
        "environment",
        "host",
        "port",
        "enabled",
        "extra_tags",
    }
)


# ---------------------------------------------------------------------------
# MikAuraStatusLogger
# ---------------------------------------------------------------------------


class MikAuraStatusLogger:
    """Pipeline-agnostic structured status logger for MikMak pipelines.

    ``context`` holds pipeline-defined dimensions; system fields overlay on emit.

    **min_level** (default ``INFO``) filters high-level helpers using the same
    ordering as standard logging: ``DEBUG`` < ``INFO`` < ``WARNING`` < ``ERROR``.

    - ``DEBUG`` — verbose diagnostics (via ``log_debug`` only).
    - ``INFO`` — ``log_running``, ``log_success``, ``log_info``, and non-failure
      statuses whose JSON ``level`` is INFO.
    - ``WARNING`` — ``log_warning``.
    - ``ERROR`` — ``log_failed``, ``log_error``, ``log_exception``.

    All of ``log_debug``, ``log_running``, ``log_success``, ``log_failed``,
    ``log_exception``, ``log_info``, ``log_warning``, and ``log_error`` respect
    ``min_level`` (and ``log_debug`` / ``log_info`` also honor ``sample_rate``
    unless ``force=True``).

    **log_status** is the low-level API: it does **not** apply ``min_level`` in
    Phase 0, so direct calls like ``log_status("running", ...)`` can still emit
    when stricter ``min_level`` would suppress ``log_running``. Prefer the
    helpers for consistent filtering.
    """

    def __init__(
        self,
        context: Dict[str, Any],
        environment: str = "",
        correlation_id: str = "",
        extra_metadata: Optional[Dict[str, Any]] = None,
        sample_rate: float = 1.0,
        min_level: str = "INFO",
        allowed_statuses: Optional[Set[str]] = None,
        strict_validation: bool = True,
        redact_patterns: Optional[List[Tuple[str, str]]] = None,
    ) -> None:
        self.context = dict(context)
        pc = str(self.context.get("pipeline_context", "")).strip()
        if not pc:
            raise ValueError("context must include non-empty pipeline_context")

        if extra_metadata:
            existing = self.context.get("extra_metadata")
            if isinstance(existing, dict):
                self.context["extra_metadata"] = {**existing, **extra_metadata}
            else:
                self.context["extra_metadata"] = dict(extra_metadata)

        self.environment = environment or os.getenv("ENVIRONMENT", "dev")
        self.correlation_id = correlation_id or str(uuid.uuid4())
        self.sample_rate = max(0.0, min(1.0, sample_rate))
        self._min_level_name = min_level.upper() if isinstance(min_level, str) else "INFO"
        self.min_level = _LOG_LEVELS.get(self._min_level_name, 20)
        self.strict_validation = strict_validation

        if allowed_statuses is None:
            self.allowed_statuses: FrozenSet[str] = DEFAULT_STATUSES
        else:
            self.allowed_statuses = frozenset(self._normalize_token(s) for s in allowed_statuses)

        self._redact_specs: Optional[List[Tuple[str, str]]] = redact_patterns
        specs = redact_patterns if redact_patterns is not None else _REDACT_SPECS
        self._redact_compiled = _compile_redact_patterns(specs)

        self._dd_trace_id: Optional[int] = None
        self._dd_span_id: Optional[int] = None
        try:
            from ddtrace import tracer  # type: ignore[import-untyped]

            span = tracer.current_span()
            if span:
                self._dd_trace_id = span.trace_id
                self._dd_span_id = span.span_id
        except Exception:
            pass

    @staticmethod
    def _normalize_token(value: Union[str, Any]) -> str:
        """Normalize to snake_case: lower, spaces/hyphens to underscore, collapse repeats."""
        if value is None:
            return ""
        s = str(value).strip().lower()
        s = re.sub(r"[\s\-]+", "_", s)
        s = re.sub(r"_+", "_", s)
        return s.strip("_")

    @classmethod
    def from_config(cls, config: MikAuraObservabilityConfig, **overrides: Any) -> "MikAuraStatusLogger":
        kwargs: Dict[str, Any] = {
            "context": dict(config.context),
            "environment": config.environment,
            "correlation_id": config.correlation_id,
            "allowed_statuses": set(config.allowed_statuses) if config.allowed_statuses is not None else None,
        }
        kwargs.update(overrides)
        return cls(**kwargs)

    def derive(self, **overrides: Any) -> "MikAuraStatusLogger":
        """Return a new logger with merged context and/or logger fields."""
        new_ctx = dict(self.context)
        logger_kw: Dict[str, Any] = {}
        for k, v in overrides.items():
            if k in _STATUS_LOGGER_DERIVE_KEYS:
                logger_kw[k] = v
            else:
                new_ctx[k] = v
        return MikAuraStatusLogger(
            context=new_ctx,
            environment=logger_kw.get("environment", self.environment),
            correlation_id=logger_kw.get("correlation_id", self.correlation_id),
            extra_metadata=logger_kw.get("extra_metadata"),
            sample_rate=logger_kw.get("sample_rate", self.sample_rate),
            min_level=logger_kw.get("min_level", self._min_level_name),
            allowed_statuses=logger_kw.get("allowed_statuses", set(self.allowed_statuses)),
            strict_validation=logger_kw.get("strict_validation", self.strict_validation),
            redact_patterns=logger_kw.get("redact_patterns", self._redact_specs),
        )

    def _redact_message(self, message: str) -> str:
        for pattern, replacement in self._redact_compiled:
            message = pattern.sub(replacement, message)
        return message

    def _build_entry(self, status: str, message: str, level: str, **extra: Any) -> Dict[str, Any]:
        normalized_status = self._normalize_token(status)
        entry: Dict[str, Any] = dict(self.context)
        entry.update(
            {
                "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
                "correlation_id": self.correlation_id,
                "environment": self.environment,
                "status": normalized_status,
                "message": self._redact_message(message),
                "level": level,
            }
        )
        if self._dd_trace_id is not None:
            entry["dd.trace_id"] = str(self._dd_trace_id)
            entry["dd.span_id"] = str(self._dd_span_id)
        for k, v in extra.items():
            if v is not None:
                entry[k] = v
        return entry

    def _emit(self, entry: Dict[str, Any]) -> Dict[str, Any]:
        try:
            line = json.dumps(entry, default=str, separators=(",", ":"))
            print(line, flush=True)
        except Exception as exc:
            print(f"[MIKAURA] Log emit failed: {exc}", file=sys.stderr)
            try:
                safe = json.dumps(entry, default=str, separators=(",", ":"))
                print(f"[MIKAURA] Entry: {safe}", file=sys.stderr)
            except Exception:
                print(f"[MIKAURA] Entry (repr): {entry!r}", file=sys.stderr)
        return entry

    def _should_emit(self, level_name: str) -> bool:
        return _LOG_LEVELS.get(level_name, 20) >= self.min_level

    def log_status(self, status: str, message: str, reason: Optional[str] = None, **extra: Any) -> Dict[str, Any]:
        """Emit a structured status log. Validates normalized status when strict.

        Does not consult ``min_level``; use ``log_running`` / ``log_success`` /
        ``log_failed`` / ``log_info`` / etc. when level filtering is required.
        """
        normalized = self._normalize_token(status)
        if self.strict_validation and normalized not in self.allowed_statuses:
            raise ValueError(
                f"Invalid status: {status!r} (normalized: {normalized!r}). "
                f"Allowed: {sorted(self.allowed_statuses)}"
            )
        level = "ERROR" if normalized == "failed" else "WARNING" if normalized == "warning" else "INFO"
        if reason:
            extra["reason"] = self._redact_message(str(reason))
        entry = self._build_entry(status, message, level, **extra)
        return self._emit(entry)

    def log_success(self, message: str, **extra: Any) -> Optional[Dict[str, Any]]:
        if not self._should_emit("INFO"):
            return None
        return self.log_status("success", message, **extra)

    def log_failed(self, message: str, reason: str, **extra: Any) -> Optional[Dict[str, Any]]:
        if not self._should_emit("ERROR"):
            return None
        return self.log_status("failed", message, reason=reason, **extra)

    def log_running(self, message: str, **extra: Any) -> Optional[Dict[str, Any]]:
        if not self._should_emit("INFO"):
            return None
        return self.log_status("running", message, **extra)

    def log_info(self, message: str, force: bool = False, **extra: Any) -> Optional[Dict[str, Any]]:
        if not self._should_emit("INFO"):
            return None
        if not force and self.sample_rate < 1.0 and random.random() > self.sample_rate:
            return None
        return self.log_status("info", message, **extra)

    def log_warning(self, message: str, **extra: Any) -> Optional[Dict[str, Any]]:
        if not self._should_emit("WARNING"):
            return None
        return self.log_status("warning", message, **extra)

    def log_error(self, message: str, reason: Optional[str] = None, **extra: Any) -> Optional[Dict[str, Any]]:
        if not self._should_emit("ERROR"):
            return None
        return self.log_status("failed", message, reason=reason, **extra)

    def log_debug(self, message: str, force: bool = False, **extra: Any) -> Optional[Dict[str, Any]]:
        """Emit a structured DEBUG line (``level`` DEBUG, ``status`` debug).

        Does not go through ``log_status``, so ``strict_validation`` /
        ``allowed_statuses`` do not apply. Subject to ``min_level`` and
        ``sample_rate`` (same sampling rule as ``log_info``).
        """
        if not self._should_emit("DEBUG"):
            return None
        if not force and self.sample_rate < 1.0 and random.random() > self.sample_rate:
            return None
        entry = self._build_entry("debug", message, "DEBUG", **extra)
        return self._emit(entry)

    def log_exception(self, message: str, exception: Exception, **extra: Any) -> Optional[Dict[str, Any]]:
        """Log an exception with type, module, and full stack trace."""
        if not self._should_emit("ERROR"):
            return None
        return self.log_status(
            "failed",
            message,
            reason=str(exception),
            exception_type=type(exception).__name__,
            exception_module=type(exception).__module__,
            stack_trace=traceback.format_exc(),
            **extra,
        )

    def log_batch_progress(
        self,
        current: int,
        total: int,
        operation: str,
        interval: int = 10,
    ) -> Optional[Dict[str, Any]]:
        if total <= 0:
            return None
        if current == 1 or current == total or current % interval == 0:
            pct = current / total * 100
            return self.log_info(
                f"{operation} progress: {current}/{total} ({pct:.0f}%)",
                force=True,
                current=current,
                total=total,
            )
        return None

    @contextmanager
    def with_context(self, **kwargs: Any) -> Iterator["MikAuraStatusLogger"]:
        """Yield a derived logger; the original instance is not mutated."""
        yield self.derive(**kwargs)

    def update_metadata(self, **kwargs: Any) -> None:
        """Merge key-value pairs into ``context``."""
        warnings.warn(
            "update_metadata() is deprecated; use derive() or with_context() for scoped context.",
            DeprecationWarning,
            stacklevel=2,
        )
        self.context.update(kwargs)


# ---------------------------------------------------------------------------
# No-op metrics fallback
# ---------------------------------------------------------------------------


class _NoOpMetrics:
    """Silent fallback when DogStatsD is unavailable."""

    def increment(self, *a: Any, **kw: Any) -> None: ...
    def gauge(self, *a: Any, **kw: Any) -> None: ...
    def histogram(self, *a: Any, **kw: Any) -> None: ...
    def timing(self, *a: Any, **kw: Any) -> None: ...
    def close(self) -> None: ...


# ---------------------------------------------------------------------------
# MikAuraMetricLogger
# ---------------------------------------------------------------------------


class MikAuraMetricLogger:
    """Pipeline-agnostic metric logger. Tags are built from ``context`` plus ``env``."""

    def __init__(
        self,
        context: Dict[str, Any],
        environment: str = "",
        host: Optional[str] = None,
        port: Optional[int] = None,
        enabled: Optional[bool] = None,
        extra_tags: Optional[List[str]] = None,
    ) -> None:
        self.context = dict(context)
        pc = str(self.context.get("pipeline_context", "")).strip()
        if not pc:
            raise ValueError("context must include non-empty pipeline_context")

        self.environment = environment or os.getenv("ENVIRONMENT", "dev")
        self._extra_tags = list(extra_tags) if extra_tags else []

        self._host = host
        self._port = port
        self._enabled = enabled
        self._client: Any = None
        self._aggregations: Dict[str, List[float]] = defaultdict(list)

    @classmethod
    def from_config(cls, config: MikAuraObservabilityConfig, **overrides: Any) -> "MikAuraMetricLogger":
        return cls(
            context=dict(config.context),
            environment=config.environment,
            **overrides,
        )

    def derive(self, **overrides: Any) -> "MikAuraMetricLogger":
        new_ctx = dict(self.context)
        logger_kw: Dict[str, Any] = {}
        for k, v in overrides.items():
            if k in _METRIC_LOGGER_DERIVE_KEYS:
                logger_kw[k] = v
            else:
                new_ctx[k] = v
        return MikAuraMetricLogger(
            context=new_ctx,
            environment=logger_kw.get("environment", self.environment),
            host=logger_kw.get("host", self._host),
            port=logger_kw.get("port", self._port),
            enabled=logger_kw.get("enabled", self._enabled),
            extra_tags=logger_kw.get("extra_tags", list(self._extra_tags)),
        )

    @property
    def client(self) -> Any:
        if self._client is None:
            try:
                from utils.metrics_utils import MetricsUtils

                kwargs: Dict[str, Any] = {}
                if self._host is not None:
                    kwargs["host"] = self._host
                if self._port is not None:
                    kwargs["port"] = self._port
                if self._enabled is not None:
                    kwargs["enabled"] = self._enabled
                self._client = MetricsUtils(**kwargs)
            except Exception:
                self._client = _NoOpMetrics()
        return self._client

    def _tag(self, key: str, value: str) -> str:
        nk = MikAuraStatusLogger._normalize_token(key)
        nv = MikAuraStatusLogger._normalize_token(str(value))
        return f"{nk}:{nv}"

    def _build_tags(self, extra_tags: Optional[List[str]] = None) -> List[str]:
        tags: List[str] = []
        for k, v in self.context.items():
            if v is None:
                continue
            tags.append(self._tag(str(k), str(v)))
        tags.append(self._tag("env", self.environment))
        tags.extend(self._extra_tags)
        if extra_tags:
            tags.extend(extra_tags)
        return tags

    def increment(self, metric_name: str, value: int = 1, extra_tags: Optional[List[str]] = None) -> None:
        try:
            self.client.increment(metric_name, value=value, tags=self._build_tags(extra_tags))
        except Exception:
            pass

    def gauge(self, metric_name: str, value: float, extra_tags: Optional[List[str]] = None) -> None:
        try:
            self.client.gauge(metric_name, value, tags=self._build_tags(extra_tags))
        except Exception:
            pass

    def histogram(self, metric_name: str, value: float, extra_tags: Optional[List[str]] = None) -> None:
        try:
            self.client.histogram(metric_name, value, tags=self._build_tags(extra_tags))
        except Exception:
            pass

    def timing(self, metric_name: str, value_ms: float, extra_tags: Optional[List[str]] = None) -> None:
        try:
            self.client.timing(metric_name, value_ms, tags=self._build_tags(extra_tags))
        except Exception:
            pass

    @contextmanager
    def timed(self, metric_name: str, extra_tags: Optional[List[str]] = None) -> Iterator[None]:
        start = time.monotonic()
        try:
            yield
        finally:
            duration_ms = (time.monotonic() - start) * 1000
            try:
                self.timing(metric_name, duration_ms, extra_tags)
            except Exception:
                pass

    def aggregate_gauge(self, metric_name: str, value: float) -> None:
        self._aggregations[metric_name].append(value)

    def flush(self) -> None:
        for metric, values in self._aggregations.items():
            if not values:
                continue
            self.gauge(f"{metric}.avg", sum(values) / len(values))
            self.gauge(f"{metric}.max", max(values))
            self.gauge(f"{metric}.min", min(values))
        self._aggregations.clear()

    def health_check(self) -> Dict[str, Any]:
        try:
            self.client.increment("mikaura.health.check", value=1, tags=self._build_tags())
            return {"status": "healthy", "datadog": "connected"}
        except Exception as e:
            return {"status": "unhealthy", "error": str(e)}

    @contextmanager
    def with_context(self, **kwargs: Any) -> Iterator["MikAuraMetricLogger"]:
        yield self.derive(**kwargs)

    def update_metadata(self, **kwargs: Any) -> None:
        warnings.warn(
            "update_metadata() is deprecated; use derive() or with_context() for scoped context.",
            DeprecationWarning,
            stacklevel=2,
        )
        self.context.update(kwargs)


def create_status_logger(
    pipeline_context: Optional[str] = None,
    *,
    context: Optional[Dict[str, Any]] = None,
    **kwargs: Any,
) -> MikAuraStatusLogger:
    """Create a new ``MikAuraStatusLogger``. Pass ``context=`` or ``pipeline_context=`` for minimal setup."""
    if context is not None:
        ctx = dict(context)
    elif pipeline_context is not None:
        ctx = {"pipeline_context": pipeline_context}
    else:
        raise ValueError("Provide pipeline_context= or context= to identify the pipeline")
    return MikAuraStatusLogger(context=ctx, **kwargs)


def create_metric_logger(
    pipeline_context: Optional[str] = None,
    *,
    context: Optional[Dict[str, Any]] = None,
    **kwargs: Any,
) -> MikAuraMetricLogger:
    """Create a new ``MikAuraMetricLogger``."""
    if context is not None:
        ctx = dict(context)
    elif pipeline_context is not None:
        ctx = {"pipeline_context": pipeline_context}
    else:
        raise ValueError("Provide pipeline_context= or context= to identify the pipeline")
    return MikAuraMetricLogger(context=ctx, **kwargs)


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="MikAura Observability test emitter")
    parser.add_argument("--pipeline", required=True, help="Pipeline context name")
    parser.add_argument("--client", default="test-client")
    parser.add_argument("--brand", default="test-brand")
    parser.add_argument("--retailer", default="test-retailer")
    parser.add_argument("--country", default="US")
    args = parser.parse_args()

    cfg = MikAuraObservabilityConfig(
        context={
            "pipeline_context": args.pipeline,
            "client_name": args.client,
            "brand_name": args.brand,
            "retailer_name": args.retailer,
            "country": args.country,
        },
    )
    sl = MikAuraStatusLogger.from_config(cfg)
    sl.log_running("CLI test started")
    sl.log_info("Processing in progress")
    sl.log_success("CLI test complete")
    print(f"\ncorrelation_id = {sl.correlation_id}")
