# MikAura Observability Guide

## Overview

MikAura Observability is a pipeline-agnostic framework for structured logging and Datadog metrics across all MikMak pipelines. It lives in a single file:

```
data_ingestion_pipeline/src/utils/mikaura_observability.py
```

It provides two main classes:

| Class | Purpose | Output |
|-------|---------|--------|
| `MikAuraStatusLogger` | Structured JSON status logging | `stdout` (CloudWatch Logs) |
| `MikAuraMetricLogger` | DogStatsD metrics (counters, timers, gauges) | UDP to Datadog Agent (`127.0.0.1:8125`) |

Both share a common `MikAuraObservabilityConfig` that defines the pipeline context, environment, and correlation ID.

---

## Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                       Lambda Function                           │
│                                                                 │
│  MikAuraObservabilityConfig                                     │
│  ┌──────────────────────────┐                                   │
│  │ pipeline_context          │                                   │
│  │ client_name               │                                   │
│  │ brand_name                │                                   │
│  │ retailer_name             │                                   │
│  │ country                   │                                   │
│  │ environment                │                                   │
│  │ correlation_id             │                                   │
│  └────────┬─────────┬────────┘                                   │
│           │         │                                            │
│           ▼         ▼                                            │
│  ┌────────────┐  ┌────────────────┐                              │
│  │StatusLogger│  │ MetricLogger   │                              │
│  │            │  │                │                              │
│  │ log_running│  │ increment()   │                              │
│  │ log_success│  │ timing()      │                              │
│  │ log_failed │  │ gauge()       │                              │
│  │ log_info   │  │ histogram()   │                              │
│  │ log_warning│  │ timed()       │◄─── context manager          │
│  │ log_debug  │  │ flush()       │                              │
│  │ log_error  │  │ health_check()│                              │
│  └─────┬──────┘  └───────┬───────┘                              │
│        │                 │                                       │
│        ▼                 ▼                                       │
│   stdout (JSON)    UDP 127.0.0.1:8125                           │
│        │                 │                                       │
└────────┼─────────────────┼───────────────────────────────────────┘
         │                 │
         ▼                 ▼
   CloudWatch Logs    Datadog Agent
         │                 │
         ▼                 ▼
   Log Insights       Datadog Dashboards
   Alarms             Monitors / Alerts
```

---

## How It Is Used in the Data Ingestion Pipeline

The data ingestion pipeline has 4 Lambda functions, all using MikAura:

| Lambda | Status Logger Var | Metric Logger Var | Fallback Metric Var |
|--------|-------------------|-------------------|---------------------|
| `mmm_dev_data_transfer` | `_mikaura_sl` | `_mikaura_ml` | `_pipeline_metrics` |
| `mmm_dev_get_client` | `status_logger` | `metric_logger` | `_get_client_metrics` |
| `data_ingestion_slack` | `status_logger` | `metric_logger` | `_slack_metrics` |
| `stale_data_check` | `status_logger` | `metric_logger` | `_stale_metrics` |

### Import Pattern (all Lambdas)

Every Lambda follows this pattern at module level:

```python
# Primary: MikAura (structured logging + Datadog metrics)
try:
    from utils.mikaura_observability import (
        MikAuraObservabilityConfig,
        MikAuraStatusLogger,
        MikAuraMetricLogger,
    )
    _MIKAURA_AVAILABLE = True
except ImportError:
    _MIKAURA_AVAILABLE = False

# Fallback: raw DogStatsD (metrics only, no structured logging)
try:
    from utils.metrics_utils import get_metrics_utils
    _fallback_metrics = get_metrics_utils()
except ImportError:
    _fallback_metrics = None
```

### Initialization (inside lambda_handler)

Each Lambda creates MikAura instances at the start of its handler:

```python
def lambda_handler(event, context):
    execution_id = context.aws_request_id

    status_logger = None
    metric_logger = None

    if _MIKAURA_AVAILABLE:
        config = MikAuraObservabilityConfig(
            context={
                "pipeline_context": "Data Ingestion Pipeline",
                "client_name": client_id or "unknown",
                "brand_name": brand_name or "unknown",
                "retailer_name": retailer_id or "unknown",
                "country": country or "unknown",
            },
            environment=ENVIRONMENT,
            correlation_id=execution_id,
            allowed_statuses={"running", "success", "failed", "warning", "info"},
        )
        status_logger = MikAuraStatusLogger.from_config(config, min_level=LOG_LEVEL)
        metric_logger = MikAuraMetricLogger.from_config(config)
```

### Status Logging Pattern

Each Lambda defines thin wrapper functions to safely call the logger. A shared private helper `_check_logger_required()` handles the fallback when the status logger is unavailable:

```python
def _check_logger_required(message: str) -> None:
    """Write stderr when status_logger is unavailable; optionally hard-fail via env."""
    sys.stderr.write(f"[STATUS_LOGGER_UNAVAILABLE] {message}\n")
    if os.environ.get("FAIL_ON_MISSING_LOGGER", "false").lower() == "true":
        raise RuntimeError(f"Status logger was not available: {message}")

def _my_lambda_info(status_logger, message, **extra):
    if status_logger:
        status_logger.log_info(message, force=True, **extra)

def _my_lambda_warning(status_logger, message, **extra):
    if status_logger:
        status_logger.log_warning(message, **extra)

def _my_lambda_failed(status_logger, message, reason, **extra):
    if status_logger:
        status_logger.log_failed(message, reason=reason, **extra)
    else:
        _check_logger_required(f"{message}: {reason}")

def _my_lambda_exception(status_logger, message, exc, **extra):
    if status_logger:
        status_logger.log_exception(message, exc, **extra)
    else:
        _check_logger_required(f"{message}: {exc}")
```

The `if status_logger:` guard ensures the code works even when MikAura is unavailable. When the logger is missing, `_check_logger_required` writes a structured `[STATUS_LOGGER_UNAVAILABLE]` line to stderr (visible in CloudWatch Logs) and optionally raises `RuntimeError` if the `FAIL_ON_MISSING_LOGGER` environment variable is set to `true`.

### Metric Emission Pattern

Metrics use MikAura as primary, with a raw DogStatsD fallback:

```python
if metric_logger:
    metric_logger.increment(
        "data_ingestion.transfer.outcome",
        extra_tags=["result:success"],
    )
elif _fallback_metrics:
    _fallback_metrics.increment(
        "data_ingestion.transfer.outcome",
        tags=[f"env:{ENVIRONMENT}", "result:success"],
    )
```

The MikAura path automatically enriches tags with context (pipeline, client, brand, retailer, env). The fallback path requires manual tag construction.

---

## MikAuraStatusLogger Reference

### Log Methods

| Method | Level | Status | When to use |
|--------|-------|--------|-------------|
| `log_running(msg)` | INFO | `running` | Lambda/task started |
| `log_success(msg)` | INFO | `success` | Lambda/task completed successfully |
| `log_info(msg)` | INFO | `info` | Informational events (file found, rows processed) |
| `log_warning(msg)` | WARNING | `warning` | Non-fatal issues (missing optional field, retry) |
| `log_failed(msg, reason)` | ERROR | `failed` | Fatal failure with reason |
| `log_error(msg, reason)` | ERROR | `failed` | Alias for `log_failed` |
| `log_exception(msg, exc)` | ERROR | `failed` | Exception with full stack trace |
| `log_debug(msg)` | DEBUG | `debug` | Verbose diagnostics (requires `min_level="DEBUG"`) |
| `log_batch_progress(i, n, op)` | INFO | `info` | Progress reporting (every Nth item) |

### JSON Output Format

Every log line is a single JSON object printed to stdout:

```json
{
  "pipeline_context": "Data Ingestion Pipeline",
  "client_name": "acme_corp",
  "brand_name": "acme-US",
  "retailer_name": "walmart",
  "country": "US",
  "timestamp": "2026-04-04T12:30:45.123456Z",
  "correlation_id": "exec-abc-123",
  "environment": "prod",
  "status": "success",
  "message": "Uploaded 3 files (142 records)",
  "level": "INFO",
  "files_processed": 3,
  "records_processed": 142
}
```

CloudWatch Logs captures this and allows querying via Log Insights.

### Scoped Context (derive / with_context)

Create child loggers with additional or overridden context:

```python
# Method 1: derive (returns new logger)
child = status_logger.derive(retailer_name="target", file_key="data.csv")
child.log_info("Processing file")

# Method 2: with_context (context manager, auto-scoped)
with status_logger.with_context(retailer_name="target") as child:
    child.log_info("Processing target data")
# Original status_logger is unchanged
```

### Allowed Statuses

Each Lambda defines its own vocabulary:

```python
ALLOWED_STATUSES = {"running", "success", "failed", "warning", "info", "no_file"}
```

If `strict_validation=True` (default), logging an unknown status raises `ValueError`. This prevents typos and enforces consistency.

### Redaction

Sensitive values (passwords, API keys, secrets) are automatically redacted:

```python
logger.log_info("Connecting with password=mysecret123")
# Output: "Connecting with password=***"
```

### Sample Rate

For high-volume DEBUG/INFO logs, use sample rate to reduce volume:

```python
logger = MikAuraStatusLogger.from_config(config, sample_rate=0.1)
logger.log_info("Processed row")  # Only 10% of calls actually emit
logger.log_info("Critical info", force=True)  # Always emits
```

---

## MikAuraMetricLogger Reference

### Metric Methods

| Method | DogStatsD Type | Example |
|--------|----------------|---------|
| `increment(name, value, extra_tags)` | Counter (`c`) | Count invocations, outcomes |
| `gauge(name, value, extra_tags)` | Gauge (`g`) | Current queue size, row count |
| `histogram(name, value, extra_tags)` | Histogram (`h`) | Value distribution |
| `timing(name, value_ms, extra_tags)` | Timer (`ms`) | Execution duration |
| `timed(name, extra_tags)` | Timer (`ms`) | Context manager that auto-measures duration |

### Auto-Tagging

MikAuraMetricLogger automatically builds tags from the context:

```python
# You write:
metric_logger.increment("data_ingestion.transfer.outcome", extra_tags=["result:success"])

# Datadog receives:
# data_ingestion.transfer.outcome:1|c|#pipeline_context:data_ingestion_pipeline,
#   client_name:acme_corp,brand_name:acme_us,retailer_name:walmart,
#   country:us,env:prod,result:success
```

No manual tag construction needed. Every metric automatically carries the full pipeline context.

### Timed Context Manager

Automatically measure and report execution duration:

```python
with metric_logger.timed("data_ingestion.s3_upload.duration_ms"):
    s3_client.put_object(Bucket=bucket, Key=key, Body=data)
# Duration is automatically sent to Datadog
```

### Aggregation (Gauge Batching)

Collect values and flush summary stats:

```python
for file in files:
    metric_logger.aggregate_gauge("data_ingestion.file_size_kb", file.size / 1024)

metric_logger.flush()
# Sends: data_ingestion.file_size_kb.avg, .max, .min
```

### Health Check

Verify Datadog connectivity:

```python
result = metric_logger.health_check()
# {"status": "healthy", "datadog": "connected"}
```

---

## Environment Variables

| Variable | Default | Used by | Purpose |
|----------|---------|---------|---------|
| `ENVIRONMENT` | `dev` | Both | Environment tag (`dev`, `staging`, `prod`) |
| `LOG_LEVEL` | `INFO` | StatusLogger | Minimum log level (`DEBUG`, `INFO`, `WARNING`, `ERROR`) |
| `DD_METRICS_ENABLED` | auto | MetricLogger | Explicit enable/disable Datadog metrics |
| `DD_AGENT_HOST` | `127.0.0.1` | MetricLogger | DogStatsD host |
| `DD_DOGSTATSD_PORT` | `8125` | MetricLogger | DogStatsD port |
| `DD_METRIC_TAGS` | (empty) | MetricLogger | Comma-separated global tags (e.g., `team:data,service:ingestion`) |
| `AWS_LAMBDA_FUNCTION_NAME` | (set by AWS) | MetricLogger | Auto-enables metrics when running in Lambda |
| `FAIL_ON_MISSING_LOGGER` | `false` | StatusLogger fallback | When `true`, raises `RuntimeError` if status logger is unavailable instead of silently degrading |

---

## Fallback Behavior

```
Is mikaura_observability.py available?
├── YES
│   ├── StatusLogger: JSON logs to stdout (CloudWatch)
│   └── MetricLogger: DogStatsD via _InlinedMetricsClient (built-in)
│       └── No dependency on metrics_utils.py
│
└── NO
    ├── StatusLogger: not available
    │   └── _check_logger_required() writes "[STATUS_LOGGER_UNAVAILABLE]" to stderr
    │       └── FAIL_ON_MISSING_LOGGER=true? → RuntimeError raised
    │       └── FAIL_ON_MISSING_LOGGER=false (default)? → pipeline continues
    └── Is metrics_utils.py available?
        ├── YES → DogStatsD via MetricsUtils (basic tags only)
        └── NO  → No metrics sent (silent no-op, no crash)
```

### Logger-Missing Behavior Matrix

| `FAIL_ON_MISSING_LOGGER` | MikAura available | Result |
|---|---|---|
| `false` (default) | Yes | Normal structured logging |
| `false` (default) | No | `[STATUS_LOGGER_UNAVAILABLE]` on stderr, pipeline continues |
| `true` | Yes | Normal structured logging |
| `true` | No | `[STATUS_LOGGER_UNAVAILABLE]` on stderr + `RuntimeError` raised |

---

## How to Plug MikAura Into Another Pipeline

### Step 1: Ensure mikaura_observability.py is accessible

Copy or symlink `data_ingestion_pipeline/src/utils/mikaura_observability.py` into your pipeline's utils:

```
your_pipeline/
├── src/
│   └── utils/
│       └── mikaura_observability.py    ← copy this file
├── lambdas/
│   └── your_lambda/
│       └── lambda_function.py
```

For Lambda deployment, ensure `src/` is included in the ZIP package (see `deploy.ps1` for reference).

### Step 2: Import and configure

```python
from utils.mikaura_observability import (
    MikAuraObservabilityConfig,
    MikAuraStatusLogger,
    MikAuraMetricLogger,
)

config = MikAuraObservabilityConfig(
    context={
        "pipeline_context": "My New Pipeline",     # REQUIRED: identifies your pipeline
        "client_name": client_id,                   # your pipeline's dimensions
        "model_name": model_name,                   # any key-value pairs you want
        "dataset": dataset_name,
    },
    environment="prod",                             # or os.getenv("ENVIRONMENT", "dev")
    correlation_id=execution_id,                    # trace across steps
)

status_logger = MikAuraStatusLogger.from_config(config, min_level="INFO")
metric_logger = MikAuraMetricLogger.from_config(config)
```

The only required context key is `pipeline_context`. Everything else is your choice.

### Step 3: Define helper wrappers (recommended)

```python
def _check_logger_required(message: str) -> None:
    """Write stderr when status_logger is unavailable; optionally hard-fail via env."""
    sys.stderr.write(f"[STATUS_LOGGER_UNAVAILABLE] {message}\n")
    if os.environ.get("FAIL_ON_MISSING_LOGGER", "false").lower() == "true":
        raise RuntimeError(f"Status logger was not available: {message}")

def _my_pipeline_info(status_logger, message, **extra):
    if status_logger:
        status_logger.log_info(message, force=True, **extra)

def _my_pipeline_warning(status_logger, message, **extra):
    if status_logger:
        status_logger.log_warning(message, **extra)

def _my_pipeline_failed(status_logger, message, reason, **extra):
    if status_logger:
        status_logger.log_failed(message, reason=reason, **extra)
    else:
        _check_logger_required(f"{message}: {reason}")

def _my_pipeline_exception(status_logger, message, exc, **extra):
    if status_logger:
        status_logger.log_exception(message, exc, **extra)
    else:
        _check_logger_required(f"{message}: {exc}")
```

By default `_check_logger_required` is a soft failure (stderr only). Set `FAIL_ON_MISSING_LOGGER=true` in the Lambda environment to make it a hard failure.

### Step 4: Use in your handler

```python
def lambda_handler(event, context):
    execution_id = context.aws_request_id

    config = MikAuraObservabilityConfig(
        context={"pipeline_context": "Training Pipeline", "model_id": event["model_id"]},
        environment=os.getenv("ENVIRONMENT", "dev"),
        correlation_id=execution_id,
    )
    sl = MikAuraStatusLogger.from_config(config)
    ml = MikAuraMetricLogger.from_config(config)

    sl.log_running("Training pipeline started")

    try:
        with ml.timed("training.duration_ms"):
            result = train_model(event["model_id"])

        ml.increment("training.outcome", extra_tags=["result:success"])
        sl.log_success(f"Model trained: accuracy={result['accuracy']:.4f}")
        return {"statusCode": 200, "accuracy": result["accuracy"]}

    except Exception as e:
        ml.increment("training.outcome", extra_tags=["result:failed"])
        sl.log_exception("Training failed", e)
        raise
```

### Step 5: Add a metrics_utils.py fallback (optional)

If you want a raw DogStatsD fallback when MikAura is unavailable:

```python
try:
    from utils.metrics_utils import get_metrics_utils
    _fallback_metrics = get_metrics_utils()
except ImportError:
    _fallback_metrics = None
```

Then at each metric emission point:

```python
if metric_logger:
    metric_logger.increment("my_pipeline.step.outcome", extra_tags=["result:success"])
elif _fallback_metrics:
    _fallback_metrics.increment("my_pipeline.step.outcome", tags=[f"env:{ENVIRONMENT}", "result:success"])
```

### Step 6: Deploy

Ensure your Lambda ZIP includes:
- `lambda_function.py`
- `src/utils/mikaura_observability.py`
- `src/utils/metrics_utils.py` (optional fallback)

The Datadog Lambda extension must be attached as a Lambda layer for metrics to reach Datadog.

---

## Complete Minimal Example (New Pipeline)

```python
"""
Example: Prediction Pipeline Lambda with MikAura observability.
"""
import os
import sys
import time

_lambda_dir = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(_lambda_dir, "src"))

try:
    from utils.mikaura_observability import (
        MikAuraObservabilityConfig,
        MikAuraStatusLogger,
        MikAuraMetricLogger,
    )
    _MIKAURA_AVAILABLE = True
except ImportError:
    _MIKAURA_AVAILABLE = False

ENVIRONMENT = os.environ.get("ENVIRONMENT", "dev")


def _check_logger_required(message: str) -> None:
    sys.stderr.write(f"[STATUS_LOGGER_UNAVAILABLE] {message}\n")
    if os.environ.get("FAIL_ON_MISSING_LOGGER", "false").lower() == "true":
        raise RuntimeError(f"Status logger was not available: {message}")


def _predict_running(sl, message, **extra):
    if sl:
        sl.log_running(message, **extra)
    else:
        _check_logger_required(message)


def _predict_exception(sl, message, exc, **extra):
    if sl:
        sl.log_exception(message, exc, **extra)
    else:
        _check_logger_required(f"{message}: {exc}")


def lambda_handler(event, context):
    execution_id = context.aws_request_id

    # --- Set up observability ---
    sl = None
    ml = None
    if _MIKAURA_AVAILABLE:
        config = MikAuraObservabilityConfig(
            context={
                "pipeline_context": "Prediction Pipeline",
                "client_name": event.get("client_id", "unknown"),
                "model_name": event.get("model_name", "unknown"),
            },
            environment=ENVIRONMENT,
            correlation_id=execution_id,
        )
        sl = MikAuraStatusLogger.from_config(config, min_level="INFO")
        ml = MikAuraMetricLogger.from_config(config)

    # --- Run pipeline ---
    _predict_running(sl, "Prediction pipeline started")

    try:
        start = time.monotonic()
        predictions = run_prediction(event)
        duration_ms = (time.monotonic() - start) * 1000

        if ml:
            ml.increment("prediction.outcome", extra_tags=["result:success"])
            ml.timing("prediction.duration_ms", duration_ms)

        if sl:
            sl.log_success(
                f"Prediction complete: {len(predictions)} rows",
                rows=len(predictions),
                duration_ms=round(duration_ms, 2),
            )

        return {"statusCode": 200, "predictions": len(predictions)}

    except Exception as e:
        if ml:
            ml.increment("prediction.outcome", extra_tags=["result:failed"])
        _predict_exception(sl, "Prediction pipeline failed", e)
        raise
```

---

## Metrics Emitted by the Data Ingestion Pipeline

| Metric Name | Type | Lambda | Tags |
|------------|------|--------|------|
| `data_ingestion.transfer.outcome` | counter | data_transfer | `result:success/failed/no_files` |
| `data_ingestion.transfer.duration_ms` | timer | data_transfer | `result:success/failed/no_files` |
| `data_ingestion.get_client.outcome` | counter | get_client | `result:success/no_active_clients/failed` |
| `data_ingestion.slack.invocation` | counter | slack | (context tags only) |
| `data_ingestion.stale_data.check` | counter | stale_data_check | `result:success/error` |
| `data_ingestion.stale_data.absence_breach` | counter | stale_data_check | (context tags only) |

All metrics automatically carry context tags: `pipeline_context`, `client_name`, `brand_name`, `retailer_name`, `country`, `env`.

---

## Key Design Decisions

1. **Pipeline-agnostic**: MikAura knows nothing about ingestion, training, or prediction. It only knows about `pipeline_context` and arbitrary key-value pairs.

2. **Self-contained DogStatsD**: `MikAuraMetricLogger` has an inlined UDP client (`_InlinedMetricsClient`). It does not depend on `metrics_utils.py`.

3. **Immutable loggers**: `derive()` and `with_context()` return new instances. The parent logger is never mutated.

4. **Fail-safe with configurable strictness**: Every metric call is wrapped in try/except. When the status logger is unavailable, `_check_logger_required()` writes a structured `[STATUS_LOGGER_UNAVAILABLE]` line to stderr (soft failure by default). Set `FAIL_ON_MISSING_LOGGER=true` to opt in to hard failure (`RuntimeError`). This lets operators choose between resilience and strict observability enforcement without code changes.

5. **Sensitive data redaction**: Passwords, API keys, and secrets are automatically scrubbed from log messages.

6. **Datadog trace correlation**: If the `ddtrace` library is available, `dd.trace_id` and `dd.span_id` are automatically injected into log entries.

7. **No raw `print()` in fallback paths**: All status-logger-missing paths use `sys.stderr.write` with the `[STATUS_LOGGER_UNAVAILABLE]` prefix. This ensures every fallback message is structured and searchable in CloudWatch Logs.
