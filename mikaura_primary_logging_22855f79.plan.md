---
name: MikAura primary logging
overview: "Make MikAura the sole operational logger across all four ingestion Lambdas: add a `log_debug` method, define compact level semantics (debug/info/warning/error), enforce per-environment level filtering via `LOG_LEVEL` env var, and migrate remaining stdlib `logger.*` calls in get_client, Slack, and data_transfer (stale_data_check is already done)."
todos:
  - id: add-log-debug
    content: Add log_debug method to MikAuraStatusLogger; add DEBUG to _should_emit gate; update DEFAULT_STATUSES if needed; unit test
    status: pending
  - id: create-helpers
    content: Create mikaura_log_helpers.py with ContextVar + mik_debug/info/warning/error/exception wrappers
    status: pending
  - id: migrate-get-client
    content: "mmm_dev_get_client: remove stdlib logger; replace 13 logger.* with MikAura; pass min_level=LOG_LEVEL"
    status: pending
  - id: migrate-slack
    content: "data_ingestion_slack: remove stdlib logger; replace ~39 logger.* with MikAura helpers"
    status: pending
  - id: migrate-transfer
    content: "mmm_dev_data_transfer: remove stdlib logger; ContextVar at handler start; replace ~100 logger.* with helpers"
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


| Lambda                                                                                              | stdlib `logger.*` calls                  | MikAura calls                                                            | Status                                           |
| --------------------------------------------------------------------------------------------------- | ---------------------------------------- | ------------------------------------------------------------------------ | ------------------------------------------------ |
| `[stale_data_check](data_ingestion_pipeline/lambdas/stale_data_check/lambda_function.py)`           | 0                                        | All                                                                      | **Done** (already migrated, no `import logging`) |
| `[mmm_dev_get_client](data_ingestion_pipeline/lambdas/mmm_dev_get_client/lambda_function.py)`       | 13 (`logger.debug/info/warning/error`)   | MikAura constructed but handler still dups with stdlib                   | Needs migration                                  |
| `[data_ingestion_slack](data_ingestion_pipeline/lambdas/data_ingestion_slack/lambda_function.py)`   | ~39 (`logger.info/warning/error/debug`)  | MikAura constructed at handler start, barely used                        | Needs migration                                  |
| `[mmm_dev_data_transfer](data_ingestion_pipeline/lambdas/mmm_dev_data_transfer/lambda_function.py)` | ~100 (`logger.debug/info/warning/error`) | MikAura for structured `emit_ingestion_observability_log` + metrics only | Needs migration (largest)                        |


## What changes

### 1. Add `log_debug` to `MikAuraStatusLogger`

**File:** `[mikaura_observability.py](data_ingestion_pipeline/src/utils/mikaura_observability.py)`

The utility already has `_LOG_LEVELS = {"DEBUG": 10, ...}` and `_should_emit` checks level >= `min_level`. But there is **no `log_debug` method** — callers were using `log_info(msg, force=True, detail_level="debug")` as a workaround. Add a proper method:

```python
def log_debug(self, message: str, **extra: Any) -> Optional[Dict[str, Any]]:
    if not self._should_emit("DEBUG"):
        return None
    return self.log_status("info", message, level_override="DEBUG", **extra)
```

This requires a small tweak to `_build_entry` or `log_status` to accept an optional `level_override` so the JSON `level` field says `"DEBUG"` while the status remains `"info"`. The `_should_emit("DEBUG")` gate means `min_level="INFO"` (the default) suppresses debug lines automatically.

Add `"debug"` to `DEFAULT_STATUSES` so strict validation does not reject it. (Alternatively, use status `"info"` with level `"DEBUG"` — pick one; the key point is that `_should_emit` gates it.)

### 2. Compact level-mapping contract

All four Lambdas use this single rule set:


| Level       | What goes here                                                                                   | MikAura method                                            |
| ----------- | ------------------------------------------------------------------------------------------------ | --------------------------------------------------------- |
| **Debug**   | File matching detail, column mapping, parsing internals, internal decisions                      | `log_debug(...)`                                          |
| **Info**    | started, file found, processed, uploaded, completed, summary counts                              | `log_info(...)`, `log_running(...)`, `log_success(...)`   |
| **Warning** | Fallback mode, degraded behavior, anomaly detected, alert send failed but continuing, data drift | `log_warning(...)`                                        |
| **Error**   | Step failed, blocked ingestion, upload/download/processing error, unhandled exception            | `log_error(...)`, `log_failed(...)`, `log_exception(...)` |


### 3. Per-environment level filtering

Already built in: `MikAuraStatusLogger(min_level=...)`. Each Lambda reads `LOG_LEVEL` from the env and passes it to `from_config(..., min_level=LOG_LEVEL)` (stale_data_check already does this at line 195).

Terraform / deploy sets:


| Env  | `LOG_LEVEL`                                 | Effect                                                      |
| ---- | ------------------------------------------- | ----------------------------------------------------------- |
| dev  | `DEBUG`                                     | debug + info + warning + error                              |
| qa   | `INFO`                                      | info + warning + error                                      |
| prod | `WARNING` (or `INFO` if you want prod info) | warning + error only (or meaningful info + warning + error) |


No code change in the utility is needed for this — just env-var configuration at deploy time.

### 4. Shared `_mikaura_log` helpers (reusable ContextVar pattern)

**New file:** `[mikaura_log_helpers.py](data_ingestion_pipeline/src/utils/mikaura_log_helpers.py)`

Small module with a `ContextVar[MikAuraStatusLogger]` and thin wrappers (`mik_debug`, `mik_info`, `mik_warning`, `mik_error`, `mik_exception`). Each Lambda sets the ContextVar at handler start and clears in `finally`. Deep helper functions call the wrappers without threading `status_logger` through every signature (critical for the 3500-line data-transfer Lambda).

### 5. Migrate `mmm_dev_get_client` (Phase 2, ~13 sites)

- Remove `import logging`, `get_context_logger`, module `logger`, `logger.setLevel`.
- Import helpers or pass `status_logger` directly (small file, either works).
- Replace each `logger.`* with the matching MikAura call per the level mapping above.
- Remove duplicate MikAura + `logger` lines in handler (e.g. `log_running` + `logger.info`).
- Pass `min_level=LOG_LEVEL` to `from_config`.

### 6. Migrate `data_ingestion_slack` (Phase 3, ~39 sites)

- Same removal of stdlib logger.
- Import helpers or thread `status_logger` through `handle`_* functions (or use ContextVar).
- Map every `logger.info("alert sent")` to `mik_info(...)`, every `logger.error("Failed to send alert")` to `mik_error(...)`, etc.
- Remove `if status_logger:` guards — MikAura is now required (import at module level).

### 7. Migrate `mmm_dev_data_transfer` (Phase 4, ~100 sites)

- Remove `import logging`, module `logger`, `logger.setLevel`.
- Use **ContextVar** pattern from helpers: set at handler start (~line 3320 where `_mikaura_sl` is created), clear in `finally`.
- Bulk replace: `logger.debug(` -> `mik_debug(`, `logger.info(` -> `mik_info(`, `logger.warning(` -> `mik_warning(`, `logger.error(` -> `mik_error(`.
- For `logger.error(..., exc_info=True)` patterns, use `mik_exception(msg, e)` when the exception is available, or `mik_error(msg)` when it is not.
- Import-time diagnostics (lines 199–205) that fire before any handler: convert to `print(..., file=sys.stderr)` since no MikAura context exists yet (cold-start-only, not operational).

### 8. Cleanup shared libs

- `[s3_utils.py](data_ingestion_pipeline/src/s3_utils.py)` and `[pipeline_info_helper.py](data_ingestion_pipeline/src/utils/pipeline_info_helper.py)` have dead `_MIKAURA_AVAILABLE` imports. Remove those imports, or wire through the ContextVar helpers if those modules should also emit structured logs.

### 9. Tests and verification

- Update `[test_stale_data_check_observability.py](data_ingestion_pipeline/tests/layers/observability/unit/test_stale_data_check_observability.py)` if the `log_debug` method changes test assertions.
- Add/update test for the new `log_debug` method in `[test_mikaura_observability.py](data_ingestion_pipeline/tests/layers/observability/unit/test_mikaura_observability.py)`.
- Per-Lambda grep: `rg "logger\.|get_context_logger|import logging"` on each migrated file should return zero matches.
- Staging: invoke each Lambda once; CloudWatch shows only MikAura JSON lines, no ContextLogger human-readable duplicates.

