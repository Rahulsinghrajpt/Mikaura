# MikAura Observability — Complete Implementation Deep Dive

## Table of Contents

1. [What MikAura Is](#1-what-mikaura-is)
2. [File Location and Module Layout](#2-file-location-and-module-layout)
3. [Architectural Principles](#3-architectural-principles)
4. [Public API Surface](#4-public-api-surface)
5. [Internal Mechanics — MikAuraObservabilityConfig](#5-internal-mechanics--mikauraobservabilityconfig)
6. [Internal Mechanics — MikAuraStatusLogger](#6-internal-mechanics--mikaurastatuslogger)
7. [Internal Mechanics — MikAuraMetricLogger](#7-internal-mechanics--mikaurametriclogger)
8. [Factory Functions](#8-factory-functions)
9. [Helper: Country Inference](#9-helper-country-inference)
10. [How Every Consumer Imports and Uses MikAura](#10-how-every-consumer-imports-and-uses-mikaura)
11. [The Wrapper Pattern (How Lambdas Bridge MikAura and stdlib)](#11-the-wrapper-pattern)
12. [Log Output Format and Datadog Integration](#12-log-output-format-and-datadog-integration)
13. [Level Filtering and Per-Environment Control](#13-level-filtering-and-per-environment-control)
14. [Status Validation and Pipeline Vocabulary](#14-status-validation-and-pipeline-vocabulary)
15. [Scoped Context: derive() and with_context()](#15-scoped-context-derive-and-with_context)
16. [Redaction System](#16-redaction-system)
17. [Sampling and Rate Limiting](#17-sampling-and-rate-limiting)
18. [Metric Aggregation and Flush](#18-metric-aggregation-and-flush)
19. [Datadog APM Trace Correlation](#19-datadog-apm-trace-correlation)
20. [Test Coverage Map](#20-test-coverage-map)
21. [Complete Call Flow: End-to-End Example](#21-complete-call-flow-end-to-end-example)

---

## 1. What MikAura Is

MikAura is the **pipeline-agnostic structured observability layer** for all MikMak pipelines. It replaces ad-hoc `logging.getLogger()` calls with:

- **Structured JSON logs** printed to stdout, where AWS Lambda + Datadog Log Forwarder can ingest them as structured facets.
- **DogStatsD metrics** emitted over UDP to the Datadog Lambda Extension (port 8125), supporting counters, gauges, histograms, and timings.
- **A single correlation ID** threaded through every log line and metric tag in a Lambda invocation, enabling end-to-end trace assembly in Datadog dashboards.

The module lives at a single file path. It has **zero external dependencies** beyond the Python standard library (Datadog's `ddtrace` and the internal `MetricsUtils` are optional lazy imports).

---

## 2. File Location and Module Layout

```
data_ingestion_pipeline/
└── src/
    └── utils/
        └── mikaura_observability.py    ← THE MODULE (689 lines)
```

### Internal sections of the file (top to bottom)

| Lines | Section | Purpose |
|-------|---------|---------|
| 1–47 | Module docstring | Quick-start examples |
| 49–64 | Imports | stdlib only; `ddtrace` imported lazily |
| 66–83 | `_infer_country_from_brand()` | Optional standalone helper |
| 86–114 | `MikAuraObservabilityConfig` | Shared dataclass for both loggers |
| 117–139 | Redaction system | Regex-based secret scrubbing |
| 142–177 | Level constants + derive key sets | `_LOG_LEVELS`, `DEFAULT_STATUSES`, `_STATUS_LOGGER_DERIVE_KEYS` |
| 180–456 | `MikAuraStatusLogger` | Structured status logging class |
| 458–471 | `_NoOpMetrics` | Silent fallback for DogStatsD |
| 473–630 | `MikAuraMetricLogger` | Metric emission class |
| 632–661 | `create_status_logger()` / `create_metric_logger()` | Factory functions |
| 664–689 | `__main__` CLI | Manual test emitter |

---

## 3. Architectural Principles

### 3.1 The utility owns only generic capabilities

MikAura provides:
- Log formatting (JSON structure with timestamp, level, status, message)
- Context merging (arbitrary `Dict[str, Any]` carried on every log line)
- Metric emission (increment, gauge, histogram, timing, timed context manager)
- Exception logging (structured with type, module, stack trace)
- Redaction (regex-based secret scrubbing)
- Timing (context manager for duration measurement)

### 3.2 The consumer pipeline owns domain specifics

Each Lambda defines:
- Which **status tokens** are valid (e.g., `"no_file"` is only valid in `mmm_dev_data_transfer`)
- Which **context keys** to include (e.g., `client_name`, `brand_name`, `country`)
- Which **metric names and tags** to send (e.g., `data_ingestion.transfer.outcome`)
- What **level** to filter at (`LOG_LEVEL` env var → `min_level` parameter)

### 3.3 No singletons, no global cached state

Every call to `MikAuraStatusLogger(...)`, `from_config(...)`, or `create_status_logger(...)` returns a **brand-new instance**. There is no module-level cached logger. This prevents cross-invocation state leakage in Lambda warm starts.

### 3.4 Immutable scoping

`with_context()` and `derive()` return new logger instances. The original is never mutated. `update_metadata()` exists for backward compatibility but emits a `DeprecationWarning`.

---

## 4. Public API Surface

### Exported symbols (what consumers import)

```python
from utils.mikaura_observability import (
    MikAuraObservabilityConfig,      # Shared config dataclass
    MikAuraStatusLogger,             # Structured status logging
    MikAuraMetricLogger,             # Datadog metric emission
    create_status_logger,            # Factory: quick status logger
    create_metric_logger,            # Factory: quick metric logger
    _infer_country_from_brand,       # Optional: country from "bella-US"
    DEFAULT_STATUSES,                # frozenset: {"running","success","failed","warning","info"}
    _redact,                         # Standalone redaction function (used by tests)
)
```

---

## 5. Internal Mechanics — MikAuraObservabilityConfig

```
File: mikaura_observability.py, lines 91–114
```

A `@dataclass` that serves as a shared configuration object for constructing both a `MikAuraStatusLogger` and a `MikAuraMetricLogger` from the same source of truth.

### Fields

| Field | Type | Default | Purpose |
|-------|------|---------|---------|
| `context` | `Dict[str, Any]` | (required) | Arbitrary key-value pairs. Must include `"pipeline_context"`. |
| `environment` | `str` | `""` | Falls back to `os.getenv("ENVIRONMENT", "dev")` |
| `correlation_id` | `str` | `""` | Falls back to `uuid.uuid4()` |
| `allowed_statuses` | `Optional[Set[str]]` | `None` | Pipeline-specific status vocabulary |

### `__post_init__` validation

1. Rejects empty `context` dict → `ValueError("context must not be empty")`
2. Makes a defensive copy: `self.context = dict(self.context)`
3. Extracts `pipeline_context`, strips whitespace, rejects empty → `ValueError`
4. Auto-fills `environment` from `ENVIRONMENT` env var if not provided
5. Auto-generates a UUID4 `correlation_id` if not provided

### What it does NOT do

- Does NOT auto-inject `client_name`, `brand_name`, `retailer_name`, `country` (removed per agnosticism principle)
- Does NOT call `_infer_country_from_brand()` (decoupled; pipelines call it themselves if needed)

---

## 6. Internal Mechanics — MikAuraStatusLogger

```
File: mikaura_observability.py, lines 185–456
```

### 6.1 Constructor (`__init__`)

Parameters:

| Parameter | Type | Default | What it does |
|-----------|------|---------|-------------|
| `context` | `Dict[str, Any]` | (required) | Pipeline-owned dimensions merged into every log entry |
| `environment` | `str` | `""` | Falls back to `ENVIRONMENT` env var |
| `correlation_id` | `str` | `""` | Falls back to UUID4 |
| `extra_metadata` | `Optional[Dict]` | `None` | Deep-merged into `context["extra_metadata"]` |
| `sample_rate` | `float` | `1.0` | 0.0–1.0; controls probabilistic sampling of `log_info`/`log_debug` |
| `min_level` | `str` | `"INFO"` | Minimum severity to emit: `DEBUG`=10, `INFO`=20, `WARNING`=30, `ERROR`=40 |
| `allowed_statuses` | `Optional[Set[str]]` | `None` | When `None`, uses `DEFAULT_STATUSES`. When set, normalizes each token. |
| `strict_validation` | `bool` | `True` | When `True`, `log_status()` raises `ValueError` for unknown statuses |
| `redact_patterns` | `Optional[List[Tuple]]` | `None` | Custom redaction regex. When `None`, uses default password/api_key/secret patterns. |

Constructor flow:

1. Defensive-copies `context`
2. Validates `pipeline_context` is non-empty
3. Merges `extra_metadata` into context
4. Resolves `environment` (explicit → env var → `"dev"`)
5. Resolves `correlation_id` (explicit → UUID4)
6. Clamps `sample_rate` to [0.0, 1.0]
7. Maps `min_level` string to numeric (unknown → defaults to 20/INFO)
8. Normalizes `allowed_statuses` via `_normalize_token()` and freezes
9. Compiles redaction regex patterns
10. Attempts to grab Datadog APM trace/span IDs from `ddtrace` (silent failure if unavailable)

### 6.2 `_normalize_token(value)` — static method

Converts any status or tag value into a canonical lowercase_underscore form:

```
"No File"  → "no_file"
"NO_FILE"  → "no_file"
"stale-data" → "stale_data"
"Multiple  Spaces" → "multiple_spaces"
```

Algorithm: lowercase → replace `[\s\-]+` with `_` → collapse `_+` → strip leading/trailing `_`.

Used in:
- `_build_entry()` for status normalization
- `MikAuraMetricLogger._tag()` for metric tag key/value normalization
- `allowed_statuses` normalization at construction time

### 6.3 `from_config(config, **overrides)` — classmethod

Takes a `MikAuraObservabilityConfig` and constructs a logger. Any `**overrides` (e.g., `min_level="DEBUG"`, `allowed_statuses=set(...)`) are merged into the constructor kwargs, overriding config defaults.

### 6.4 `derive(**overrides)` — instance method

Returns a **new** `MikAuraStatusLogger` instance with merged context. Overrides are split into two categories:

- Keys in `_STATUS_LOGGER_DERIVE_KEYS` (like `environment`, `min_level`, `sample_rate`) → set as constructor kwargs
- All other keys → merged into `context`

This is the backbone of `with_context()`.

### 6.5 `_build_entry(status, message, level, **extra)` — internal

Constructs the log dict:

```python
{
    # ALL keys from self.context (pipeline-defined, e.g. pipeline_context, client_name, ...)
    "timestamp": "2026-03-31T12:00:00.000000Z",   # UTC ISO-8601
    "correlation_id": "abc-123-...",
    "environment": "dev",
    "status": "running",                            # normalized
    "message": "Starting process",                  # redacted
    "level": "INFO",
    # Datadog APM (only if ddtrace available):
    "dd.trace_id": "12345",
    "dd.span_id": "67890",
    # Any **extra kwargs with non-None values:
    "reason": "some_reason",
    "files_processed": 3,
}
```

### 6.6 `_emit(entry)` — internal

Serializes the entry dict to compact JSON (`separators=(",",":")`) and prints to stdout with `flush=True`. This is what AWS Lambda captures and Datadog Log Forwarder parses.

If JSON serialization fails, falls back to stderr with `[MIKAURA]` prefix for debugging. **Never raises** — observability must not crash the pipeline.

### 6.7 `_should_emit(level_name)` — internal

Compares the requested level's numeric value against `self.min_level`:

```
DEBUG=10, INFO=20, WARNING=30, ERROR=40
```

Returns `True` if `_LOG_LEVELS[level_name] >= self.min_level`. This is how per-environment filtering works.

### 6.8 High-level log methods

Each method checks `_should_emit()` and delegates to `log_status()`:

| Method | Level check | Status token | Level string | Sampling |
|--------|------------|--------------|-------------|----------|
| `log_running(msg)` | INFO | `"running"` | `"INFO"` | No |
| `log_success(msg)` | INFO | `"success"` | `"INFO"` | No |
| `log_info(msg, force=False)` | INFO | `"info"` | `"INFO"` | Yes (unless `force=True`) |
| `log_warning(msg)` | WARNING | `"warning"` | `"WARNING"` | No |
| `log_error(msg, reason)` | ERROR | `"failed"` | `"ERROR"` | No |
| `log_failed(msg, reason)` | ERROR | `"failed"` | `"ERROR"` | No |
| `log_exception(msg, exc)` | ERROR | `"failed"` | `"ERROR"` | No |
| `log_debug(msg, force=False)` | DEBUG | `"debug"` | `"DEBUG"` | Yes (unless `force=True`) |

**Important behavioral notes:**

- `log_debug()` bypasses `log_status()` entirely. It calls `_build_entry()` → `_emit()` directly. This means `strict_validation` and `allowed_statuses` do **not** apply to debug entries.
- `log_status()` is the low-level API that does **not** consult `min_level`. A direct `log_status("running", ...)` call will emit even if `min_level="WARNING"`. Always prefer the helper methods for consistent filtering.
- `log_info()` and `log_debug()` respect `sample_rate`: when `sample_rate < 1.0` and `force=False`, a `random.random()` check may suppress the emit.

### 6.9 `log_exception(message, exception, **extra)`

Emits a structured error log with enriched fields:

```python
{
    "status": "failed",
    "level": "ERROR",
    "message": "Operation failed",
    "reason": "connection refused",           # str(exception)
    "exception_type": "ConnectionError",      # type(exception).__name__
    "exception_module": "builtins",           # type(exception).__module__
    "stack_trace": "Traceback (most recent ..." # traceback.format_exc()
}
```

### 6.10 `log_batch_progress(current, total, operation, interval=10)`

Emits an info log at the first item, every `interval`-th item, and the last item. Useful for file processing loops. Uses `force=True` to bypass sampling.

### 6.11 `with_context(**kwargs)` — context manager

```python
with logger.with_context(retailer_name="walmart") as child:
    child.log_info("Processing walmart data")
# logger is unchanged after the block
```

Calls `self.derive(**kwargs)` and yields the new instance. The original logger is **never mutated**.

### 6.12 `update_metadata(**kwargs)` — DEPRECATED

Still functional (mutates `self.context` in place) but emits `DeprecationWarning`. No Lambda code calls this method. Kept for backward compatibility with documentation references.

---

## 7. Internal Mechanics — MikAuraMetricLogger

```
File: mikaura_observability.py, lines 478–630
```

### 7.1 Constructor

| Parameter | Type | Default | Purpose |
|-----------|------|---------|---------|
| `context` | `Dict[str, Any]` | (required) | Context keys become metric tags |
| `environment` | `str` | `""` | `ENVIRONMENT` env var fallback |
| `host` | `Optional[str]` | `None` | DogStatsD host override |
| `port` | `Optional[int]` | `None` | DogStatsD port override |
| `enabled` | `Optional[bool]` | `None` | Explicit enable/disable |
| `extra_tags` | `Optional[List[str]]` | `None` | Additional static tags on every metric |

Validates `pipeline_context` is non-empty (same rule as status logger).

### 7.2 Lazy client initialization (`client` property)

The DogStatsD client is **not** created at construction time. On first access of `self.client`:

1. Attempts `from utils.metrics_utils import MetricsUtils`
2. Passes `host`/`port`/`enabled` if set
3. On any failure → falls back to `_NoOpMetrics()` (silently does nothing)

This means the metric logger is always safe to construct, even in environments without DogStatsD.

### 7.3 Tag building (`_build_tags`)

Every metric call builds tags from:

1. **All context key-value pairs** → normalized via `_normalize_token()` → `"pipeline_context:data_ingestion_pipeline"`, `"client_name:madebygather"`, etc.
2. **`env` tag** → `"env:dev"` / `"env:prod"`
3. **`_extra_tags`** → static tags set at construction
4. **`extra_tags`** → per-call tags passed to `increment()`/`gauge()`/etc.

### 7.4 Metric methods

| Method | DogStatsD type | Signature |
|--------|---------------|-----------|
| `increment(name, value=1, extra_tags)` | Counter | Counts events |
| `gauge(name, value, extra_tags)` | Gauge | Current value |
| `histogram(name, value, extra_tags)` | Histogram | Distribution |
| `timing(name, value_ms, extra_tags)` | Timing | Duration in ms |
| `timed(name, extra_tags)` | Context manager | Auto-measures duration |

All methods silently swallow exceptions — metric emission must never crash the pipeline.

### 7.5 `timed(metric_name)` — context manager

```python
with metrics.timed("data_ingestion.transfer.duration_ms"):
    process_files()  # duration is automatically measured and sent
```

Uses `time.monotonic()` for reliable elapsed time measurement. Sends the result via `self.timing()`.

### 7.6 Metric aggregation (`aggregate_gauge` / `flush`)

Collects multiple gauge values under the same name, then on `flush()` emits:
- `{metric}.avg` — average of collected values
- `{metric}.max` — maximum
- `{metric}.min` — minimum

Useful for batch processing where you want summary stats rather than individual data points.

### 7.7 `health_check()`

Sends a test `increment("mikaura.health.check")` to verify DogStatsD connectivity. Returns `{"status": "healthy", "datadog": "connected"}` or `{"status": "unhealthy", "error": "..."}`.

### 7.8 `derive()` and `with_context()`

Same pattern as `MikAuraStatusLogger`: returns a new instance, never mutates the original. Keys in `_METRIC_LOGGER_DERIVE_KEYS` are constructor kwargs; all others are merged into context.

---

## 8. Factory Functions

```
File: mikaura_observability.py, lines 632–661
```

### `create_status_logger(pipeline_context=None, *, context=None, **kwargs)`

Convenience function for quick logger creation:

```python
# Minimal
logger = create_status_logger("Data Ingestion Pipeline")

# With full context
logger = create_status_logger(context={"pipeline_context": "...", "client_name": "..."})
```

**Always returns a new instance** (no caching). Raises `ValueError` if neither `pipeline_context` nor `context` is provided.

### `create_metric_logger(pipeline_context=None, *, context=None, **kwargs)`

Same pattern for metric loggers.

---

## 9. Helper: Country Inference

```
File: mikaura_observability.py, lines 71–83
```

`_infer_country_from_brand(brand_name)` extracts a 2-letter country code from brand naming conventions:

```
"bella-US"     → "US"
"cleaning-CA"  → "CA"
"brand_uk"     → "UK"
"nobrand"      → "unknown"
""             → "unknown"
"bella-123"    → "unknown"  (suffix "123" is not alpha)
```

This is a **standalone public function**, not auto-invoked by any class. The `mmm_dev_data_transfer` Lambda calls it manually when building its MikAura context from event data.

---

## 10. How Every Consumer Imports and Uses MikAura

### 10.1 Import pattern

Every Lambda and utility module follows the same import guard pattern:

```python
try:
    from utils.mikaura_observability import (
        MikAuraObservabilityConfig,
        MikAuraStatusLogger,
        MikAuraMetricLogger,
    )
    _MIKAURA_AVAILABLE = True
except ImportError:
    _MIKAURA_AVAILABLE = False
```

The `try/except ImportError` ensures the Lambda still runs even if `mikaura_observability.py` is missing from the deployment package.

### 10.2 Consumer map

| File | Import Guard | Config | StatusLogger | MetricLogger | Wrapper Pattern |
|------|-------------|--------|-------------|-------------|----------------|
| `mmm_dev_get_client/lambda_function.py` | `_MIKAURA_AVAILABLE` | Yes | Yes | Yes | `if status_logger: ... else: logger.*` |
| `stale_data_check/lambda_function.py` | Hard import (no guard) | Yes | Yes | Yes | Direct `status_logger.*` calls |
| `data_ingestion_slack/lambda_function.py` | `_MIKAURA_AVAILABLE` | Yes | Yes | Yes | `_slack_info/_warning/_error/_exception/_debug` wrappers |
| `mmm_dev_data_transfer/lambda_function.py` | `_MIKAURA_AVAILABLE` | Yes | Yes | Yes | `_transfer_debug/_info/_warning/_error/_failed/_exception/_running` wrappers |
| `onboarding_pipeline/lambda/lambda_function.py` | `_MIKAURA_AVAILABLE` | Yes | Yes | No | `_onb_info/_warning/_error/_exception` wrappers |
| `src/utils/pipeline_info_helper.py` | `_MIKAURA_AVAILABLE` | No | Type-only import | No | Accepts optional `status_logger` param |
| `src/s3_utils.py` | `_MIKAURA_AVAILABLE` | No | Type-only import | No | Accepts optional `status_logger` param |

### 10.3 Construction pattern in Lambda handlers

Every Lambda handler follows this initialization sequence:

```python
def lambda_handler(event, context):
    execution_id = context.aws_request_id

    status_logger = None
    metric_logger = None
    if _MIKAURA_AVAILABLE:
        _config = MikAuraObservabilityConfig(
            context={"pipeline_context": "Data Ingestion Pipeline", ...},
            environment=ENVIRONMENT,
            correlation_id=execution_id,       # ties all logs to this invocation
        )
        status_logger = MikAuraStatusLogger.from_config(
            _config,
            min_level=LOG_LEVEL,               # from env var, e.g. "INFO"
            allowed_statuses=set(MY_LAMBDA_ALLOWED_STATUSES),
        )
        metric_logger = MikAuraMetricLogger.from_config(_config)
```

Key points:
- `correlation_id` is the Lambda `aws_request_id`, so every log line from the same invocation can be correlated in Datadog.
- `allowed_statuses` is the Lambda's own frozenset defined at module level.
- Both loggers share the same config (same context, same correlation_id).

### 10.4 How `mmm_dev_data_transfer` differs

This Lambda includes richer context because it processes per-client/brand/retailer data:

```python
_mikaura_config = MikAuraObservabilityConfig(
    context={
        "pipeline_context": "Data Ingestion Pipeline",
        "client_name": client_id or "unknown",
        "brand_name": brand_name or "unknown",
        "retailer_name": retailer_id or "unknown",
        "country": obs_country,              # inferred from brand_name suffix
    },
    environment=ENVIRONMENT,
    correlation_id=step_function_execution_id,
    allowed_statuses=set(INGESTION_MIKAURA_ALLOWED_STATUSES),
)
```

Its `INGESTION_MIKAURA_ALLOWED_STATUSES` includes `"no_file"` beyond the defaults, because this pipeline has a distinct "no files found" outcome.

---

## 11. The Wrapper Pattern

Each Lambda defines thin wrapper functions that route to MikAura when available, or fall back to stdlib `ContextLogger` when not.

### `mmm_dev_data_transfer` example (7 wrappers)

```python
def _transfer_debug(status_logger, debug_event, message, **fields):
    if status_logger:
        status_logger.log_debug(message, debug_event=debug_event, **fields)
    else:
        logger.debug(message)

def _transfer_info(status_logger, message, force=True, **fields):
    if status_logger:
        status_logger.log_info(message, force=force, **fields)
    else:
        logger.info(message)

def _transfer_warning(status_logger, message, **fields):
    if status_logger:
        status_logger.log_warning(message, **fields)
    else:
        logger.warning(message)

def _transfer_error(status_logger, message, reason=None, **fields):
    if status_logger:
        status_logger.log_error(message, reason=reason or message, **fields)
    else:
        logger.error(message)

def _transfer_failed(status_logger, message, reason, **fields):
    if status_logger:
        status_logger.log_failed(message, reason=reason, **fields)
    else:
        logger.error(f"{message}: {reason}")

def _transfer_exception(status_logger, message, exc, **fields):
    if status_logger:
        status_logger.log_exception(message, exc, **fields)
    else:
        logger.error(message, exc_info=True)

def _transfer_running(status_logger, message, **fields):
    if status_logger:
        status_logger.log_running(message, **fields)
    else:
        logger.info(message)
```

### `data_ingestion_slack` example (5 wrappers)

Same pattern with `_slack_info`, `_slack_warning`, `_slack_error`, `_slack_exception`, `_slack_debug`.

### `onboarding_pipeline` example (4 wrappers)

Same pattern with `_onb_info`, `_onb_warning`, `_onb_error`, `_onb_exception`.

### Why wrappers exist

1. **Graceful degradation**: If MikAura import fails, the pipeline still works with stdlib logging.
2. **Single log path**: When MikAura is available, only MikAura emits (no duplicate stdlib log for the same event).
3. **Structured extras**: The wrapper passes keyword arguments that become structured fields in the JSON output (e.g., `file_key=`, `retailer_id=`, `debug_event=`).

### When stdlib `logger.*` is used directly (not through wrappers)

Only in two cases, both annotated with `# stdlib:` comments:

1. **Module-level startup** (before `lambda_handler`): Import-time checks like "PipelineInfoHelper not available" fire before any MikAura logger exists.
2. **`load_drift_profile()`**: May be called before the first `lambda_handler` invocation in cold start scenarios.

---

## 12. Log Output Format and Datadog Integration

### What MikAura prints to stdout

A single compact JSON line per `_emit()` call:

```json
{"pipeline_context":"Data Ingestion Pipeline","client_name":"madebygather","brand_name":"bella-US","retailer_name":"amazon","country":"US","timestamp":"2026-03-31T12:00:00.000000Z","correlation_id":"abc-123","environment":"dev","status":"running","message":"Lambda 2 started","level":"INFO"}
```

### How Datadog ingests it

1. Lambda writes JSON to stdout
2. Datadog Lambda Extension (or Log Forwarder) reads CloudWatch Logs
3. Datadog auto-parses JSON and creates facets for each key
4. In Datadog Log Explorer, you can filter by `@status:failed`, `@client_name:madebygather`, `@correlation_id:abc-123`, etc.

### The `emit_ingestion_observability_log()` function in `mmm_dev_data_transfer`

This function emits a **combined** JSON line containing both:
- `"ingestion": { ... }` — backward-compatible fields for existing Datadog dashboards
- `"mikaura": { ... }` — new pipeline-agnostic facets built via `_build_entry()`

This is printed as a single `print(json.dumps(...))` call so it appears as one log line in CloudWatch.

---

## 13. Level Filtering and Per-Environment Control

### The level hierarchy

```
DEBUG (10) < INFO (20) < WARNING (30) < ERROR (40)
```

### How `min_level` works

When `min_level="WARNING"`:
- `log_debug("...")` → suppressed (10 < 30)
- `log_info("...")` → suppressed (20 < 30)
- `log_running("...")` → suppressed (20 < 30)
- `log_warning("...")` → emitted (30 >= 30)
- `log_error("...")` → emitted (40 >= 30)

### Per-environment configuration

Every Lambda reads `LOG_LEVEL` from environment variables:

```python
LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO")
```

Deployment configuration sets:
- **dev**: `LOG_LEVEL=DEBUG` — full verbosity including file parsing details
- **qa**: `LOG_LEVEL=INFO` — operational events, no debug noise
- **prod**: `LOG_LEVEL=INFO` or `LOG_LEVEL=WARNING` — only meaningful operational and error events

### `log_status()` bypasses `min_level`

The low-level `log_status()` method does NOT check `_should_emit()`. This is intentional: direct `log_status("running", ...)` calls (used in `emit_ingestion_observability_log`) always emit regardless of `min_level`. Prefer the helper methods (`log_running`, `log_info`, etc.) for consistent filtering.

---

## 14. Status Validation and Pipeline Vocabulary

### Default statuses (utility-level)

```python
DEFAULT_STATUSES = frozenset({"running", "success", "failed", "warning", "info"})
```

### Pipeline-level overrides

| Lambda | Constant | Statuses | Extras beyond default |
|--------|----------|----------|-----------------------|
| `mmm_dev_get_client` | `GET_CLIENT_ALLOWED_STATUSES` | `running, success, failed, warning, info` | (none) |
| `stale_data_check` | `STALE_CHECK_ALLOWED_STATUSES` | `running, success, failed, warning, info` | (none) |
| `data_ingestion_slack` | `SLACK_ALLOWED_STATUSES` | `running, success, failed, warning, info` | (none) |
| `mmm_dev_data_transfer` | `INGESTION_MIKAURA_ALLOWED_STATUSES` | `running, success, failed, warning, info, no_file` | `no_file` |

### How validation works

When `strict_validation=True` (default):

```python
def log_status(self, status, message, ...):
    normalized = self._normalize_token(status)  # "No File" → "no_file"
    if self.strict_validation and normalized not in self.allowed_statuses:
        raise ValueError(f"Invalid status: {status!r} ...")
```

This prevents typos and undeclared statuses from silently entering Datadog facets.

### `log_debug()` bypasses validation

`log_debug()` does NOT go through `log_status()`, so `"debug"` does not need to be in `allowed_statuses`. This is by design — debug is a level, not a business status.

---

## 15. Scoped Context: derive() and with_context()

### Problem

You need to add `retailer_name="walmart"` to logs for a specific block, then revert.

### Solution

```python
with logger.with_context(retailer_name="walmart") as child:
    child.log_info("Processing walmart data")
    # child.context["retailer_name"] == "walmart"

# logger.context["retailer_name"] == original value (unchanged)
```

### How it works internally

1. `with_context(**kwargs)` calls `self.derive(**kwargs)`
2. `derive()` creates a brand-new `MikAuraStatusLogger`:
   - Copies `self.context` into a new dict
   - Splits kwargs: logger-internal keys (`environment`, `min_level`, etc.) go to constructor; everything else merges into the new context dict
   - Calls `MikAuraStatusLogger(context=new_ctx, ...)` — a fresh instance
3. `with_context()` yields this new instance
4. On block exit, the new instance is discarded; original `logger` is untouched

### Same pattern on MikAuraMetricLogger

`MikAuraMetricLogger.with_context()` and `.derive()` follow the exact same immutable-copy pattern, using `_METRIC_LOGGER_DERIVE_KEYS` to separate logger kwargs from context keys.

---

## 16. Redaction System

### Default patterns

```python
_REDACT_SPECS = [
    (r'password["\']?\s*[:=]\s*["\']?([^"\'\}\s]+)', "password=***"),
    (r'api[_\-]?key["\']?\s*[:=]\s*["\']?([^"\'\}\s]+)', "api_key=***"),
    (r'secret["\']?\s*[:=]\s*["\']?([^"\'\}\s]+)', "secret=***"),
]
```

### How it applies

- Patterns are compiled once at module load into `_DEFAULT_REDACT_COMPILED`
- Every log message passes through `_redact_message()` before being placed into the JSON entry
- `reason` fields in `log_status()` are also redacted
- Custom patterns can be passed via `redact_patterns=` at construction time

### Example

```python
logger.log_info("Connection with password=secret123 established")
# Emitted message: "Connection with password=*** established"
```

---

## 17. Sampling and Rate Limiting

### How `sample_rate` works

Only `log_info()` and `log_debug()` are subject to sampling. When `sample_rate < 1.0`:

```python
if not force and self.sample_rate < 1.0 and random.random() > self.sample_rate:
    return None  # suppressed
```

- `sample_rate=0.0` → suppresses all info/debug unless `force=True`
- `sample_rate=0.5` → ~50% of info/debug logs are emitted
- `sample_rate=1.0` (default) → everything emits

### `force=True` bypasses sampling

```python
logger.log_info("Critical info that must always appear", force=True)
```

Used by `log_batch_progress()` and by Lambda wrappers like `_transfer_info(status_logger, msg, force=True)`.

### Not affected by sampling

`log_running`, `log_success`, `log_warning`, `log_error`, `log_failed`, `log_exception` — all always emit (subject only to `min_level`).

---

## 18. Metric Aggregation and Flush

### Use case

During a batch loop processing 100 files, you want summary latency stats, not 100 individual gauge calls:

```python
for file in files:
    latency = process(file)
    metrics.aggregate_gauge("data_ingestion.file_latency", latency)

metrics.flush()
# Sends: file_latency.avg, file_latency.max, file_latency.min
```

### How `flush()` works

1. Iterates over `_aggregations` dict (defaultdict of lists)
2. For each metric name with values:
   - `gauge(f"{metric}.avg", average)`
   - `gauge(f"{metric}.max", max_value)`
   - `gauge(f"{metric}.min", min_value)`
3. Clears all aggregations

---

## 19. Datadog APM Trace Correlation

### What happens at construction

```python
try:
    from ddtrace import tracer
    span = tracer.current_span()
    if span:
        self._dd_trace_id = span.trace_id
        self._dd_span_id = span.span_id
except Exception:
    pass
```

### How it appears in logs

When `ddtrace` is available and there's an active span:

```json
{
    "dd.trace_id": "123456789",
    "dd.span_id": "987654321",
    ...
}
```

Datadog uses these fields to correlate logs with APM traces, so you can click from a log entry to the distributed trace.

### When it's absent

In most Lambda environments without the `ddtrace` library, these fields are simply omitted. No error, no `None` values.

---

## 20. Test Coverage Map

### `test_mikaura_observability.py` — 68 tests

| Test Class | What it covers |
|-----------|---------------|
| `TestInputValidation` | Config/logger reject empty context, missing pipeline_context, invalid statuses |
| `TestDefaults` | Verifies utility does NOT auto-inject `client_name`/`brand_name`/etc. |
| `TestCountryInference` | Parametrized tests for `_infer_country_from_brand()` |
| `TestCorrelationId` | Auto-generation, explicit propagation, config preservation |
| `TestLazyInit` | MetricLogger client is None until first use, falls back to NoOp |
| `TestExceptionLogging` | `log_exception()` structure: status, reason, exception_type, stack_trace |
| `TestFromConfig` | `from_config()` correctly transfers context, correlation_id |
| `TestSampling` | `sample_rate=0.0` skips, `force=True` bypasses, `sample_rate=1.0` always emits |
| `TestTimedContextManager` | `timed()` measures duration and calls `timing()` |
| `TestLogLevelFiltering` | `min_level=ERROR` suppresses info/warning, allows error |
| `TestBatchProgress` | First, last, interval, zero-total edge cases |
| `TestLogSchema` | Required fields (`timestamp`, `status`, `message`, `pipeline_context`, `correlation_id`) |
| `TestMetricAggregation` | `aggregate_gauge` + `flush` → avg/max/min gauges, clears state |
| `TestRedaction` | password, api_key, secret scrubbing; normal text unchanged |
| `TestHealthCheck` | Healthy (mock success) and unhealthy (mock OSError) |
| `TestStatusLoggerOutput` | Full context appears in output; pipeline_context is customizable |
| `TestStatusLoggerWithContext` | `with_context()` overrides in child, original unchanged |
| `TestNormalizeToken` | "No File"→"no_file", "NO_FILE"→"no_file", "stale-data"→"stale_data" |
| `TestFactoryNotSingleton` | `create_status_logger("p1")` returns distinct instances; no-arg raises ValueError |
| `TestMetricLoggerAutoTags` | Context keys become normalized tags on metrics |
| `TestDatadogAPM` | No trace IDs without ddtrace; trace IDs when mocked |
| `TestEnvironmentAutoDetect` | Reads from `ENVIRONMENT` env var; explicit override wins |
| `TestGenericContext` | Arbitrary keys (e.g., `model_version`) appear in log JSON |
| `TestEmitSafety` | `_emit()` never raises even on JSON serialization failure |

### `test_mikaura_level_filtering.py` — 10 tests

| Test Class | What it covers |
|-----------|---------------|
| `TestDefaultMinLevelInfo` | Default: lifecycle emits, `log_debug` suppressed |
| `TestMinLevelDebug` | `min_level=DEBUG`: `log_debug` emits with `level=DEBUG`, `status=debug`, includes `debug_event` extra |
| `TestMinLevelWarning` | Suppresses running/success; allows failed/exception |
| `TestMinLevelError` | Suppresses info/warning/running/success; allows error/failed/exception |
| `TestInvalidMinLevelFallback` | Unknown min_level string → falls back to INFO (numeric 20) |
| `TestLogDebugSampleRate` | `sample_rate=0.0` suppresses debug; `force=True` bypasses |
| `TestLogDebugVsStrictValidation` | `log_debug()` bypasses `allowed_statuses`; `log_status("debug", ...)` is validated |
| `TestLogStatusBypassesMinLevel` | `log_running()` helper respects min_level; `log_status("running", ...)` does not |

### Lambda-level test coverage

| Test File | MikAura-related test classes |
|----------|------------------------------|
| `test_data_transfer_lambda.py` | `TestTransferStructuredDebug` (verifies `log_debug` with `debug_event`), `TestPhase4TransferSinglePath` (verifies no duplicate stdlib logs when MikAura is on) |
| `test_get_client_lambda.py` | `TestObservabilitySinglePath` (verifies MikAura-only path) |
| `test_data_ingestion_slack_lambda.py` | `TestSlackSinglePathLogging` |

---

## 21. Complete Call Flow: End-to-End Example

### Scenario: `mmm_dev_data_transfer` processes a file

```
1. EventBridge → Step Function → Lambda 2 invocation
   └── event = {client_id: "madebygather", brand_id: "bella-US", retailer_id: "amazon", ...}

2. lambda_handler() starts
   ├── execution_id = context.aws_request_id  (e.g., "abc-123-456")
   ├── obs_country = _infer_country_from_brand("bella-US") → "US"  [standalone helper]
   │
   ├── MikAuraObservabilityConfig(
   │     context={"pipeline_context":"Data Ingestion Pipeline",
   │              "client_name":"madebygather", "brand_name":"bella-US",
   │              "retailer_name":"amazon", "country":"US"},
   │     environment="dev",
   │     correlation_id="abc-123-456",
   │     allowed_statuses={"running","success","failed","warning","info","no_file"},
   │   )
   │
   ├── _mikaura_sl = MikAuraStatusLogger.from_config(_config, min_level="INFO")
   │     └── __init__: context copied, correlation_id="abc-123-456",
   │         min_level=20, allowed_statuses frozen, redaction compiled,
   │         ddtrace span captured (if available)
   │
   └── _mikaura_ml = MikAuraMetricLogger.from_config(_config)
         └── __init__: context copied, _client=None (lazy)

3. _run_data_transfer() starts
   │
   ├── _transfer_running(_mikaura_sl, "Lambda 2 started: ...")
   │     └── _mikaura_sl.log_running("Lambda 2 started: ...")
   │           ├── _should_emit("INFO") → True (20 >= 20)
   │           ├── log_status("running", "Lambda 2 started: ...")
   │           │     ├── _normalize_token("running") → "running"
   │           │     ├── "running" in allowed_statuses → OK
   │           │     ├── _build_entry("running", "Lambda 2 started: ...", "INFO")
   │           │     │     → {"pipeline_context":"Data Ingestion Pipeline",
   │           │     │        "client_name":"madebygather", ...,
   │           │     │        "timestamp":"2026-03-31T12:00:00.000000Z",
   │           │     │        "correlation_id":"abc-123-456",
   │           │     │        "environment":"dev",
   │           │     │        "status":"running",
   │           │     │        "message":"Lambda 2 started: madebygather/bella_us/amazon",
   │           │     │        "level":"INFO"}
   │           │     └── _emit(entry)
   │           │           └── print('{"pipeline_context":"Data Ingestion Pipeline",...}', flush=True)
   │           │                 → CloudWatch → Datadog Log Forwarder → Datadog facets
   │           └── Returns the entry dict
   │
   ├── [file discovery, download, validation, processing...]
   │     Each step calls _transfer_info/_warning/_error/_debug
   │     All carry the same correlation_id="abc-123-456"
   │
   ├── _mikaura_ml.increment("data_ingestion.transfer.outcome", extra_tags=["result:success"])
   │     └── client (lazy init) → MetricsUtils → UDP to 127.0.0.1:8125
   │           Tags: ["pipeline_context:data_ingestion_pipeline",
   │                   "client_name:madebygather", "brand_name:bella_us",
   │                   "retailer_name:amazon", "country:us", "env:dev",
   │                   "result:success"]
   │
   └── return {statusCode: 200, files_processed: 1, ...}
```

### What appears in Datadog

**Logs** (Log Explorer):
- Every `print()` call from `_emit()` becomes a structured log entry
- Filterable by `@correlation_id`, `@client_name`, `@status`, `@level`, etc.
- The `correlation_id` ties every log from this invocation together

**Metrics** (Metrics Explorer):
- `data_ingestion.transfer.outcome` counter with `result:success` tag
- `data_ingestion.transfer.duration_ms` timing metric
- All tagged with `pipeline_context`, `client_name`, `env`, etc.

**APM** (if `ddtrace` is installed):
- `dd.trace_id` and `dd.span_id` in log entries link to distributed traces
