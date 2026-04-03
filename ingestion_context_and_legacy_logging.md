# `ingestion_context` vs MikAura (data ingestion pipeline)

This note explains what `ingestion_context` from `utils.context_logger` does, what depends on it in this repository, and what to expect if you remove it from Lambdas (for example to satisfy the observability compliance scan in `scripts/observability_compliance_evaluator.py`).

## What `ingestion_context` affects

`ingestion_context` is a context manager that binds fields into a **`ContextVar`** for the duration of a `with` block, then restores the previous value on exit. That variable is read by **`ContextLogger`** when it formats messages: `get_ingestion_context()` is used inside `format_message` in `src/utils/context_logger.py`.

It does **not**:

- Change MikAura loggers (`MikAuraStatusLogger`, `MikAuraMetricLogger`)
- Affect DynamoDB, S3, or business logic
- Feed **`metrics_utils`** (no use of ingestion context there)

## What would break in this codebase?

For **normal execution** of the Lambdas that currently use `with ingestion_context(...):` (`stale_data_check`, `mmm_dev_get_client`, `mmm_dev_data_transfer`), **removing `ingestion_context` should not break runtime behavior** in the current `data_ingestion_pipeline` tree, because:

1. Under `src/` and `lambdas/`, the only **runtime** imports from `utils.context_logger` are those three Lambda entrypoints, and they import **`ingestion_context` only** (not `get_context_logger`).
2. No other module under `src/` calls `get_context_logger()` or imports `context_logger` for logging.
3. Therefore, during a handler run, there is no other first-party code that both runs inside your `with ingestion_context(...):` block **and** logs through `ContextLogger` in a way that would read that ContextVar.

In other words, you are not removing a dependency that other in-repo modules rely on today.

## What you might lose (not a crash)

**Hypothetical legacy logging:** If code **outside** this tree (for example an older Lambda layer, a forked utility, or future code) called `get_context_logger()` while the handler was inside an `ingestion_context` block, log lines would **no longer** get the extra contextual suffix built from bound fields (`client_id`, `use_case`, etc.). That is a **shape of log output** change, not an application logic failure. With the current repository layout, that path does not appear in first-party code.

## What breaks if the change is done incorrectly

| Mistake | Result |
|--------|--------|
| Remove the import but keep `with ingestion_context(...):` | **`NameError`** at runtime |
| Change shared helpers used by `test_context_logger_and_metrics.py` without updating tests | Test failures (those tests exercise `ingestion_context` / `get_ingestion_context` directly; they are not tied to the Lambdas unless you alter the shared helpers they cover) |

## Bottom line

Dropping `ingestion_context` from those three Lambdas **and** removing or replacing the surrounding `with` block is **very unlikely to break application behavior** here. The main residual risk is **losing optional context on legacy `ContextLogger` lines** if something not visible in this repo (for example a private layer) still used `get_context_logger()` during the handler.

## Preferred alternative for scoped fields (MikAura)

If you want the same **idea**—extra fields scoped to a block—but on the supported observability path, use MikAura’s **`status_logger.with_context(...)`** or **`derive(...)`** on `MikAuraStatusLogger` / `MikAuraMetricLogger` as defined in `src/utils/mikaura_observability.py`, instead of binding the legacy ContextVar.

## Related

- Compliance scan: `scripts/observability_compliance_evaluator.py` (flags imports from modules whose name contains `context_logger`).
- Legacy implementation: `src/utils/context_logger.py`.
- Example unit coverage: `tests/layers/observability/unit/test_context_logger_and_metrics.py`.
