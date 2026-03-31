# RCA: Plain-Text Warning Appearing in Data Transfer Lambda Logs

**Date:** 2026-03-31
**Severity:** Low (cosmetic / observability gap)
**Status:** Root cause identified, fix not yet applied

---

## 1. Observed Symptom

During a production `mmm_dev_data_transfer` Lambda invocation, the following **plain-text** warning appeared in CloudWatch instead of structured MikAura JSON:

```
[WARNING] 2026-03-31T14:13:16.150Z 655d63bf-bdd4-4ab0-b83e-a79ac8b1ca4a
✓ Updated pipeline info using discovered key: madebygather/bella-US#amazon.
ACTION REQUIRED: Migrate record to normalized format 'bella_us#amazon'.
Migration steps: 1) Update create_pipeline_info() to auto-normalize keys,
2) Run migration script to update existing records,
3) Remove discovery logic from update_pipeline_info().
| brand_id=bella-US brand_name=Bella-US client_id=madebygather country=unknown
  data_source_bucket=prd-mm-vendor-sync execution_id=arn:aws:states:eu-west-1:...
  pipeline_component=data_transfer_lambda retailer_id=amazon use_case=data_ingestion_transfer
```

**What is wrong with this output:**

- Format is `[WARNING] <timestamp> <request_id> <message> | <context suffix>` — this is the **stdlib ContextLogger** format, not MikAura structured JSON.
- Datadog cannot parse this as structured facets; it lands as a raw text log line.
- It breaks the "MikAura as primary logging interface" compliance requirement.

---

## 2. Root Cause

There are **two independent root causes** compounding into this single log line.

### Root Cause A: `pipeline_info_helper.py` uses raw stdlib logging (not MikAura)

**File:** `data_ingestion_pipeline/src/utils/pipeline_info_helper.py`

The helper module has its own module-level `ContextLogger`:

```python
# line 122
logger = get_context_logger(__name__)
```

The `update_pipeline_info()` method (line 616) and the discovery fallback logic (lines 732–795) emit all their logs through this raw `logger.warning(...)` / `logger.info(...)` / `logger.error(...)`. The module has **53 total `logger.*` calls** and **zero** `status_logger` parameters on any public method.

Unlike the Lambda functions (`mmm_dev_data_transfer`, `mmm_dev_get_client`, etc.) which were migrated to MikAura wrapper functions (`_transfer_info`, `_transfer_warning`, etc.), `pipeline_info_helper.py` was explicitly listed under **"No-Change Surfaces"** in the MikAura Compliance Fix plan to limit risk scope. As a result, every call into `PipelineInfoHelper` from the Lambda handler exits the MikAura path and falls back to stdlib.

### Root Cause B: DynamoDB `brand_retailer_key` format mismatch triggers the discovery fallback

**The specific warning fires because:**

1. `update_pipeline_info_after_transfer()` in `mmm_dev_data_transfer` (line 4087) calls:
   ```python
   helper.update_pipeline_info(
       client_id="madebygather",
       brand_name="bella-US",     # original brand_id from event
       retailer_id="amazon",
       updates={"last_transfer_row_count": records_processed}
   )
   ```

2. Inside `update_pipeline_info()`, `build_sort_key("bella-US", "amazon")` normalizes to `"bella_us#amazon"` (hyphens → underscores, lowercase).

3. A DynamoDB `get_item` with key `{client_id: "madebygather", brand_retailer_key: "bella_us#amazon"}` is attempted (line 685).

4. **The record does not exist with the normalized key** because it was originally created with the un-normalized key `"bella-US#amazon"` (hyphens preserved, mixed case).

5. The discovery fallback (`_discover_brand_retailer_key_format`, line 327) queries all records for `client_id="madebygather"`, iterates them, and finds the matching record with key `"bella-US#amazon"`.

6. The update succeeds using the discovered key, and the warning is emitted (line 774):
   ```
   ✓ Updated pipeline info using discovered key: madebygather/bella-US#amazon.
   ACTION REQUIRED: Migrate record to normalized format 'bella_us#amazon'.
   ```

**This means:** The DynamoDB records for this client were created before `build_sort_key()` normalization was introduced. The existing records still use the legacy un-normalized format (`bella-US#amazon`), while the code now expects the normalized format (`bella_us#amazon`).

---

## 3. Call Chain

```
mmm_dev_data_transfer.lambda_handler()
  └── _run_data_transfer()
        └── update_pipeline_info_after_transfer(
                client_id="madebygather",
                brand_name="bella-US",      ← brand_id from Step Function event
                retailer_id="amazon",
                status_logger=_mikaura_sl   ← MikAura logger exists here
            )
              └── helper.update_pipeline_info(           ← crosses into pipeline_info_helper.py
                      "madebygather", "bella-US", "amazon", {...}
                  )
                    ├── build_sort_key("bella-US", "amazon") → "bella_us#amazon"
                    ├── get_item(key="bella_us#amazon")      → NOT FOUND
                    ├── _discover_brand_retailer_key_format() → finds "bella-US#amazon"
                    ├── update_item(key="bella-US#amazon")   → SUCCESS
                    └── logger.warning("✓ Updated using discovered key...")
                          │
                          │  This is ContextLogger (stdlib), NOT MikAura.
                          │  The ingestion_context() block from the Lambda handler
                          │  is still active, so ContextLogger appends the
                          │  "| brand_id=... client_id=..." suffix.
                          ▼
                    CloudWatch: [WARNING] ... plain text ... | context suffix
```

The `status_logger=_mikaura_sl` parameter stops at `update_pipeline_info_after_transfer()` — it is never passed into `helper.update_pipeline_info()` because `PipelineInfoHelper` does not accept a `status_logger` parameter.

---

## 4. Impact

| Area | Impact |
|------|--------|
| **Datadog structured facets** | This log line cannot be queried by `@status`, `@level`, `@correlation_id` in Datadog Log Explorer. It is a raw text line. |
| **Observability compliance** | Violates the "MikAura as primary logging interface" requirement for any code path that touches `PipelineInfoHelper`. |
| **Pipeline functionality** | **None.** The DynamoDB update succeeds via discovery fallback. The data transfer completes normally. |
| **Frequency** | Fires on **every invocation** for any client whose DynamoDB record uses the legacy un-normalized `brand_retailer_key` format. |
| **Performance** | Minor: the discovery fallback issues an extra `Query` to DynamoDB (fetches all records for the client) before the update. Results are cached per Lambda invocation. |

---

## 5. Why It Wasn't Caught Earlier

- `pipeline_info_helper.py` was explicitly scoped out of the MikAura Compliance Fix plan under "No-Change Surfaces" to limit risk, since it has 53 `logger.*` calls across 1,554 lines and is a shared utility used by multiple pipelines.
- The `brand_retailer_key` format mismatch is a pre-existing data issue — the warning is intentional (it tells operators to migrate the DynamoDB records), but the logging channel is wrong.
- Unit tests mock `PipelineInfoHelper` at the Lambda level, so the helper's internal logging path is not exercised in Lambda-level test suites.

---

## 6. Recommended Fix (Two Tracks)

### Track 1: Eliminate the warning entirely (data migration)

**Root fix for Root Cause B.** Migrate existing DynamoDB records from legacy `brand_retailer_key` format to the normalized format:

| Before | After |
|--------|-------|
| `bella-US#amazon` | `bella_us#amazon` |

Steps:
1. Write a one-time migration script that scans `mmm-{env}-pipeline-infos`, normalizes each `brand_retailer_key`, and writes the record with the new key (delete old + put new, in a transaction).
2. Verify `create_pipeline_info()` already uses `build_sort_key()` for new records (it does).
3. After migration, the normalized `get_item` lookup succeeds on the first try, the discovery fallback never fires, and this warning disappears.

### Track 2: Migrate `pipeline_info_helper.py` to MikAura (logging path)

**Root fix for Root Cause A.** Add an optional `status_logger` parameter to the public methods of `PipelineInfoHelper` and route logs through it when available:

1. Add `status_logger: Optional[Any] = None` to `update_pipeline_info()`, `mark_data_updated()`, `get_pipeline_info()`, and other high-frequency methods.
2. Create `_helper_info` / `_helper_warning` / `_helper_error` wrappers (same pattern as `_transfer_info` in the Lambda).
3. Convert the 53 `logger.*` calls to use wrappers.
4. Pass `status_logger=_mikaura_sl` from all call sites in `mmm_dev_data_transfer` and other Lambdas.

### Recommended priority

- **Track 1 first** — eliminates the noisy warning and the extra DynamoDB query. Lower risk, smaller scope.
- **Track 2 second** — completes the MikAura migration for this shared utility. Larger scope but aligns with full observability compliance.
