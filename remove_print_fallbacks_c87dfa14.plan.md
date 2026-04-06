---
name: Remove Print Fallbacks
overview: Replace all print() fallbacks in Lambda helper functions with a structured sys.stderr write that clearly states the status logger was unavailable, keeping the message visible in CloudWatch without relying on print().
todos:
  - id: fix-data-transfer-prints
    content: Replace 3 print() fallbacks in mmm_dev_data_transfer (_transfer_error, _transfer_failed, _transfer_exception)
    status: pending
  - id: fix-slack-prints
    content: Replace 3 print() fallbacks in data_ingestion_slack (_slack_error, _slack_exception, lambda_handler inline)
    status: pending
  - id: fix-get-client-prints
    content: Replace 3 print() fallbacks in mmm_dev_get_client (_get_client_error, _get_client_exception, S3 logging fallback)
    status: pending
isProject: false
---

# Remove print() Fallbacks from Lambda Helper Functions

Replace every `else: print(...)` fallback (used when `status_logger` is `None`) with a `sys.stderr.write(...)` line that emits a minimal structured note. This keeps the event visible in CloudWatch Logs at the `ERROR` stream level while making clear MikAura was unavailable.

The replacement pattern for every `else` branch is:

```python
else:
    sys.stderr.write(f"[STATUS_LOGGER_UNAVAILABLE] {message}\n")
```

For exceptions, include the exception text:

```python
else:
    sys.stderr.write(f"[STATUS_LOGGER_UNAVAILABLE] {message}: {exc}\n")
```

For informational starts (like the Slack lambda `log_running` fallback), use `sys.stdout.write` since it is not an error:

```python
else:
    sys.stdout.write(f"[STATUS_LOGGER_UNAVAILABLE] {message}\n")
```

---

## Files and exact locations to change

### 1. [mmm_dev_data_transfer/lambda_function.py](data_ingestion_pipeline/lambdas/mmm_dev_data_transfer/lambda_function.py)

3 helper functions, lines 228-252:

- `_transfer_error` (line 233-234): `print(f"[ERROR] {message}")` → `sys.stderr.write(...)`
- `_transfer_failed` (line 242-243): `print(f"[ERROR] {message}: {reason}")` → `sys.stderr.write(...)`
- `_transfer_exception` (line 251-252): `print(f"[ERROR] {message}: {exc}")` → `sys.stderr.write(...)`

All 3 are error-level, so use `sys.stderr`.

### 2. [data_ingestion_slack/lambda_function.py](data_ingestion_pipeline/lambdas/data_ingestion_slack/lambda_function.py)

3 locations:

- `_slack_error` (line 85-86): `print(f"[ERROR] {message}")` → `sys.stderr.write(...)`
- `_slack_exception` (line 97-98): `print(f"[ERROR] {message}: {exc}")` → `sys.stderr.write(...)`
- `lambda_handler` inline (line 1677-1678): `print(f"Slack notification Lambda started: {execution_id}")` → `sys.stdout.write(...)` (informational, not an error)

### 3. [mmm_dev_get_client/lambda_function.py](data_ingestion_pipeline/lambdas/mmm_dev_get_client/lambda_function.py)

3 locations:

- `_get_client_error` (line 168-169): `print(f"[ERROR] {message}")` → `sys.stderr.write(...)`
- `_get_client_exception` (line 187-188): `print(f"[ERROR] {message}: {exc}")` → `sys.stderr.write(...)`
- S3 logging fallback (line 225-226): `print(f"[S3 Logging Error] Failed to write log to S3: {e}")` → `sys.stderr.write(...)`

### 4. `stale_data_check/lambda_function.py`

No `print()` fallbacks in helper functions -- already clean. No changes needed.

---

## What each replacement looks like

`**_transfer_error` (data_transfer):**

```python
def _transfer_error(status_logger, message, reason=None, **fields):
    if status_logger:
        status_logger.log_error(message, reason=reason or message, **fields)
    else:
        sys.stderr.write(f"[STATUS_LOGGER_UNAVAILABLE] {message}\n")
```

`**_transfer_exception` (data_transfer):**

```python
def _transfer_exception(status_logger, message, exc, **fields):
    if status_logger:
        status_logger.log_exception(message, exc, **fields)
    else:
        sys.stderr.write(f"[STATUS_LOGGER_UNAVAILABLE] {message}: {exc}\n")
```

**Slack lambda_handler inline (informational):**

```python
if status_logger:
    status_logger.log_running("Slack notification Lambda started", execution_id=execution_id)
else:
    sys.stdout.write(f"[STATUS_LOGGER_UNAVAILABLE] Slack notification Lambda started: {execution_id}\n")
```

---

## What does NOT change

- Import-time `print()` calls (lines 256-263 in data_transfer) -- these fire before any logger exists and are intentional startup diagnostics. Leave as-is.
- Any `print()` that is NOT inside an `else: status_logger is None` branch.
- `sys` is already imported in all 4 Lambda files -- no new imports needed.
- `stale_data_check` has no such fallbacks -- no changes needed there.

