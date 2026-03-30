---
name: MikAura primary logging
overview: "Make MikAura the sole operational logger across all four ingestion Lambdas: add a `log_debug` method, define compact level semantics (debug/info/warning/error), enforce per-environment level filtering via `LOG_LEVEL` env var, and migrate remaining stdlib `logger.*` calls in get_client, Slack, and data_transfer (stale_data_check is already done). No extra helper files — each Lambda uses MikAura directly via a module-level `_sl` variable."
todos:
  - id: add-log-debug
    content: Add log_debug method to MikAuraStatusLogger; add DEBUG to _should_emit gate; update DEFAULT_STATUSES if needed; unit test
    status: pending
  - id: migrate-get-client
    content: "mmm_dev_get_client: remove stdlib logger; replace 13 logger.* with _sl.log_* calls; pass min_level=LOG_LEVEL"
    status: pending
  - id: migrate-slack
    content: "data_ingestion_slack: remove stdlib logger; replace ~39 logger.* with _sl.log_* calls"
    status: pending
  - id: migrate-transfer
    content: "mmm_dev_data_transfer: remove stdlib logger; set module-level _sl at handler start; replace ~100 logger.* with _sl.log_* calls"
    status: pending
  - id: cleanup-shared
    content: Remove dead MikAura scaffolding from s3_utils.py and pipeline_info_helper.py
    status: pending
  - id: tests-verify
    content: Update tests; per-file grep verification; staging smoke
    status: pending
isProject: false
---

# MikAura as primary logging interface

## Current state


| Lambda                                                                                            | stdlib `logger.*` calls | MikAura calls                                           | Status                    |
| ------------------------------------------------------------------------------------------------- | ----------------------- | ------------------------------------------------------- | ------------------------- |
| [stale_data_check](data_ingestion_pipeline/lambdas/stale_data_check/lambda_function.py)           | 0                       | All                                                     | Done (already migrated)   |
| [mmm_dev_get_client](data_ingestion_pipeline/lambdas/mmm_dev_get_client/lambda_function.py)       | 13                      | MikAura constructed but handler dups with stdlib        | Needs migration           |
| [data_ingestion_slack](data_ingestion_pipeline/lambdas/data_ingestion_slack/lambda_function.py)   | ~39                     | MikAura constructed but barely used                     | Needs migration           |
| [mmm_dev_data_transfer](data_ingestion_pipeline/lambdas/mmm_dev_data_transfer/lambda_function.py) | ~100                    | MikAura for structured observability log + metrics only | Needs migration (largest) |


---

## Design principle: no extra files, no wrapper functions

Lambda invocations are single-threaded. Each Lambda already constructs `_mikaura_sl` (a `MikAuraStatusLogger`) in its handler. The migration simply:

1. Promotes `_mikaura_sl` to a **module-level variable** `_sl` (set in handler, cleared in `finally`).
2. Replaces every `logger.info(msg)` with `_sl.log_info(msg)`, `logger.debug(msg)` with `_sl.log_debug(msg)`, etc. — calling MikAura methods **directly**, no wrappers.
3. Removes `import logging`, `get_context_logger`, and the stdlib `logger` entirely.

For the handful of import-time diagnostics that fire before the handler (cold start), use `print(..., file=sys.stderr)` since `_sl` is not yet set.

---

## What changes

### 1. Add `log_debug` to `MikAuraStatusLogger`

**File:** [mikaura_observability.py](data_ingestion_pipeline/src/utils/mikaura_observability.py)

The utility has `_LOG_LEVELS = {"DEBUG": 10, ...}` and `_should_emit` checks `level >= min_level`, but there is no `log_debug` method. Add one:

```python
def log_debug(self, message: str, **extra: Any) -> Optional[Dict[str, Any]]:
    if not self._should_emit("DEBUG"):
        return None
    return self.log_status("info", message, level_override="DEBUG", **extra)
```

Small tweak to `_build_entry` / `log_status` to accept `level_override` so the JSON `level` field says `"DEBUG"` while the status remains `"info"`. `_should_emit("DEBUG")` means `min_level="INFO"` suppresses debug lines automatically.

### 2. Compact level-mapping contract

All four Lambdas use this rule set:


| Level   | What goes here                                                                        | MikAura method                                                        |
| ------- | ------------------------------------------------------------------------------------- | --------------------------------------------------------------------- |
| Debug   | File matching, parsing detail, column mapping, internal decisions                     | `_sl.log_debug(...)`                                                  |
| Info    | Started, file found, processed, uploaded, completed, summary counts                   | `_sl.log_info(...)`, `_sl.log_running(...)`, `_sl.log_success(...)`   |
| Warning | Fallback mode, degraded behavior, anomaly, data drift, alert failed but continuing    | `_sl.log_warning(...)`                                                |
| Error   | Step failed, blocked ingestion, upload/download/processing error, unhandled exception | `_sl.log_error(...)`, `_sl.log_failed(...)`, `_sl.log_exception(...)` |


### 3. Per-environment level filtering

Already built in via `MikAuraStatusLogger(min_level=...)`. Each Lambda reads `LOG_LEVEL` from the env and passes it to `from_config(..., min_level=LOG_LEVEL)`:


| Env  | `LOG_LEVEL`         | Effect                                                 |
| ---- | ------------------- | ------------------------------------------------------ |
| dev  | `DEBUG`             | debug + info + warning + error                         |
| qa   | `INFO`              | info + warning + error                                 |
| prod | `WARNING` or `INFO` | warning + error (or meaningful info + warning + error) |


No utility code change needed — deploy-time env-var config only.

### 4. Module-level `_sl` pattern (used in all three Lambdas)

Each Lambda follows this pattern — no helper files, no ContextVar, no wrapper functions:

```python
from utils.mikaura_observability import (
    MikAuraObservabilityConfig, MikAuraStatusLogger, MikAuraMetricLogger,
)

_sl: Optional[MikAuraStatusLogger] = None
_ml: Optional[MikAuraMetricLogger] = None

def lambda_handler(event, context):
    global _sl, _ml
    _config = MikAuraObservabilityConfig(...)
    _sl = MikAuraStatusLogger.from_config(_config, min_level=LOG_LEVEL)
    _ml = MikAuraMetricLogger.from_config(_config)
    try:
        # ... handler body — all functions in the file use _sl directly ...
        _sl.log_running("Lambda started", execution_id=execution_id)
        result = do_work(...)
        _sl.log_success("Lambda completed")
        return result
    except Exception as e:
        _sl.log_exception("Lambda failed", e)
        raise
    finally:
        _sl = None
        _ml = None
```

Every function in the file calls `_sl.log_info(...)`, `_sl.log_debug(...)`, etc. directly. If `_MIKAURA_AVAILABLE` is False, `_sl` stays `None` and callers guard with `if _sl:` (same pattern the code already uses for `_mikaura_sl` today).

### 5. Migrate `mmm_dev_get_client` (~13 call sites, 398 lines)

**File:** [lambda_function.py](data_ingestion_pipeline/lambdas/mmm_dev_get_client/lambda_function.py)

- Remove `import logging`, `get_context_logger`, `logger = get_context_logger(...)`, `logger.setLevel(LOG_LEVEL)` (lines 57, 112-130).
- Rename existing `_mikaura_sl` / `status_logger` in handler to module-level `_sl` set at handler start.
- Replace each `logger.info(...)` with `_sl.log_info(...)`, `logger.error(...)` with `_sl.log_error(...)`, etc. in `write_log_to_s3`, `validate_table_exists`, `fetch_metadata_active_clients`, `format_client_metadata`, and `lambda_handler`.
- Remove duplicate lines where both `logger.`* and `status_logger.*` log the same event.

### 6. Migrate `data_ingestion_slack` (~39 call sites, ~1458 lines)

**File:** [lambda_function.py](data_ingestion_pipeline/lambdas/data_ingestion_slack/lambda_function.py)

- Remove `import logging`, `get_context_logger`, `logger = get_context_logger(...)`, `logger.setLevel(...)` (lines 19, 42-55).
- Promote existing handler-scoped `status_logger` to module-level `_sl`.
- Replace all `logger.info/warning/error/debug(...)` with `_sl.log_info/log_warning/log_error/log_debug(...)`.
- For `logger.error(..., exc_info=True)`, use `_sl.log_exception(msg, e)` when the exception object is available.

### 7. Migrate `mmm_dev_data_transfer` (~100 call sites, 3557 lines)

**File:** [lambda_function.py](data_ingestion_pipeline/lambdas/mmm_dev_data_transfer/lambda_function.py)

- Remove `import logging`, `get_context_logger`, `logger = get_context_logger(...)`, `logger.setLevel(LOG_LEVEL)` (lines 176-195).
- Promote existing `_mikaura_sl` (created at handler line 3286) to module-level `_sl`, cleared in `finally`.
- Systematic replacement:
  - `logger.debug(msg)` -> `_sl.log_debug(msg)`
  - `logger.info(msg)` -> `_sl.log_info(msg)`
  - `logger.warning(msg)` -> `_sl.log_warning(msg)`
  - `logger.error(msg)` -> `_sl.log_error(msg)`
  - `logger.error(msg, exc_info=True)` -> `_sl.log_exception(msg, e)` where `e` is in scope, else `_sl.log_error(msg)`
- Import-time diagnostics (lines 198-205: "CRITICAL: PipelineInfoHelper not available", etc.) fire before handler, so convert those to `print(..., file=sys.stderr)`.

### 8. Cleanup shared libs

- [s3_utils.py](data_ingestion_pipeline/src/s3_utils.py) and [pipeline_info_helper.py](data_ingestion_pipeline/src/utils/pipeline_info_helper.py) have dead `_MIKAURA_AVAILABLE` imports. Remove those dead imports, or wire through `_sl` if those modules should also emit structured logs.

### 9. Tests and verification

- Update [test_stale_data_check_observability.py](data_ingestion_pipeline/tests/layers/observability/unit/test_stale_data_check_observability.py) if `log_debug` changes assertions.
- Add test for new `log_debug` method in [test_mikaura_observability.py](data_ingestion_pipeline/tests/layers/observability/unit/test_mikaura_observability.py).
- Per-Lambda grep: `rg "logger\.|get_context_logger|import logging"` on each migrated file must return zero matches.
- Staging smoke: invoke each Lambda once; CloudWatch shows only MikAura JSON lines, no stdlib duplicates.

