# RCA Report: Drift Detection False Alerts Across All Retailers

**Date:** 2026-04-04
**Severity:** High
**Ticket:** ME-5401 / ME-5402
**Author:** Engineering Team
**Status:** Root cause identified; fix pending

---

## 1. Problem Statement

Drift detection (ME-5401 Spend Regime Shift and ME-5402 KPI Behavior Break) fires alerts for **almost every dataset** processed by the `mmm_dev_data_transfer` Lambda, regardless of retailer. The intent was to detect genuine anomalies; instead, the system generates a high volume of false positives, rendering the alerts unreliable.

---

## 2. Root Cause

**The single `profile.json` deployed with the Lambda was built from one specific retailer's data (Target) but is applied to every retailer's data during ingestion.**

Seema's reference script (`Weekly_MMM_Guardrails_With_Injected_Cases.ipynb`) is designed to produce a **per-retailer** profile. The `build_reference_profile()` function takes a single retailer's DataFrame, infers its schema dynamically, computes statistics from that retailer's historical data, and trains a ridge regression model specific to that retailer. The output is a profile that is only valid for that one retailer.

Our implementation took the output of running this script on the **Target** retailer dataset and packaged it as a static `profile.json` file deployed alongside the Lambda. The Lambda then loads this single file and applies it universally to Amazon, Walmart, Target, and any other retailer.

---

## 3. Why This Causes False Alerts

### 3.1 Column Name Mismatch (Schema)

The profile's `schema.kpi_col` is `Target_sales`. The `spend_cols` include `Target_spend`, `Google_spend`, `Meta_spend`, `OOH_spend`, `PaidSocial_spend`, `TV_spend`.

Other retailers' datasets have different column names. When the Lambda processes an Amazon dataset:
- `Target_sales` may not exist (KPI detection silently skips or uses fallback `Sales`)
- `Target_spend` may not exist (channel is skipped)
- Amazon-specific channels are not in the profile (never evaluated)

**Result:** Some channels are never checked, others are checked against the wrong baseline.

### 3.2 Statistical Baseline Mismatch (Spend Stats)

The `spend_stats` (mean, std, p99, p995) reflect Target's historical spend distribution. For example:

| Channel | Target Profile Mean | Target Profile Std |
|---------|--------------------|--------------------|
| Google_spend | $29,997 | $12,520 |
| TV_spend | $119,612 | $35,385 |
| OOH_spend | $71,217 | $21,426 |

When the Lambda evaluates Amazon's data using these baselines:
- If Amazon's `Google_spend` is $60,000 (normal for Amazon, which spends more on Google), the z-score against Target's mean of $29,997 is **(60000 - 29997) / 12520 = 2.40**. A slightly higher week pushes past the z=3.0 YELLOW threshold.
- Conversely, if another retailer spends less, a perfectly normal week can still appear anomalous.

**Result:** z-scores are meaningless because the reference distribution belongs to a different retailer.

### 3.3 Regression Coefficients Are Retailer-Specific (KPI Residual)

The `residual_profile.beta` vector (17 coefficients) encodes the relationship `log(Target_sales) ~ log(spend_channels) + z(controls) + events`. This relationship is unique to Target:
- Different retailers have different KPI response curves to spend
- Different base sales volumes
- Different seasonal patterns

When applied to Amazon's data, the predicted KPI is systematically wrong, producing large residuals. The residual z-score (using Target's `resid_mean=0.0036` and `resid_std=0.1738`) will almost always exceed the threshold.

**Result:** Near-100% false positive rate for ME-5402 KPI Behavior Break on non-Target retailers.

### 3.4 Mix Profile Is Single-Retailer

The `mix_profile.avg_share` captures Target's average channel spend allocation:
```
Google: 9.6%, Meta: 9.5%, OOH: 23.0%, PaidSocial: 8.0%, TV: 38.2%, Target_spend: 11.7%
```

Other retailers have entirely different marketing mixes. Any retailer with a different allocation will trigger MIX_SHIFT alerts (though our Lambda currently only checks SPEND_OUTLIER and KPI_RESIDUAL_SHOCK, the profile still encodes the wrong mix).

### 3.5 Control Statistics Are Dataset-Specific

`GQV` and `Seasonality` means/stds were computed from Target's data. If another retailer's dataset has different GQV ranges or seasonality patterns, control drift will fire incorrectly.

---

## 4. Evidence

### 4.1 Code Path

The Lambda loads the profile once per cold start and reuses it for every file:

```python
# lambda_function.py line 283-294
DRIFT_PROFILE_PATH = os.environ.get("DRIFT_PROFILE_PATH", "")
_DRIFT_PROFILE_CACHE: Optional[Dict[str, Any]] = None
_DRIFT_PROFILE_LOADED = False
```

```python
# lambda_function.py line 330-369
def load_drift_profile(status_logger=None):
    global _DRIFT_PROFILE_CACHE, _DRIFT_PROFILE_LOADED
    if _DRIFT_PROFILE_LOADED:
        return _DRIFT_PROFILE_CACHE  # returns same profile for every retailer
    ...
```

```python
# lambda_function.py line 4130-4132
drift_profile = load_drift_profile(status_logger=status_logger)
if drift_profile:
    spend_regime_result = detect_spend_regime_shift(df, drift_profile)
```

No retailer-specific logic exists. The profile is loaded once and applied to whatever DataFrame is current.

### 4.2 Profile Provenance

The `profile.json` in the repo contains:
- `kpi_col: "Target_sales"` -- explicitly Target-specific
- `spend_cols` include `"Target_spend"` -- a Target-only channel
- 157 `active_weeks` -- matches Seema's Target dataset (157 historical rows)
- `residual_profile.beta` has 17 coefficients trained on Target's data

### 4.3 Notebook vs Lambda

| Aspect | Notebook (Seema's script) | Lambda (our implementation) |
|--------|--------------------------|---------------------------|
| Profile scope | Per-retailer, per-dataset | Single static file for all |
| Schema inference | Dynamic (`infer_schema()`) | Hardcoded in profile.json |
| Profile computation | From retailer's own historical rows | Pre-computed from Target only |
| Ridge regression | Trained on each retailer's data | Coefficients from Target only |
| Checks implemented | 7 checks (spend outlier, channel activation, mix shift, KPI residual, control drift, channel deactivation, event toggle) | 2 checks (spend outlier, KPI residual) |

---

## 5. Impact

1. **Alert fatigue:** Near-100% false positive rate on non-Target retailers causes operations to ignore all drift alerts.
2. **Incorrect retraining flags:** `retraining_required=True` is set in DynamoDB for retailers that don't actually have drift, wasting compute resources.
3. **Missed genuine drift:** Because alerts fire for everything, real drift events on Target itself get buried in noise.
4. **Incorrect drift metrics in DynamoDB:** `drift_metric_current` values stored are meaningless for non-Target retailers since they measure z-scores against the wrong baseline.

---

## 6. Recommended Fix

Replace the static `profile.json` approach with **dynamic, per-retailer profile generation** at processing time inside the Lambda.

### Approach

1. **Sort the incoming DataFrame by date.**
2. **Split into historical rows (all but last) and the latest row.**
3. **Dynamically infer the schema** from the DataFrame columns (spend cols, KPI col, control cols, event cols) -- just like `infer_schema()` in the notebook.
4. **Build a reference profile in-memory** from the historical rows -- compute spend stats, control stats, and ridge regression coefficients.
5. **Score the latest row** against this dynamically-built profile.
6. **No file I/O:** The profile is computed and consumed within the same Lambda invocation. No `profile.json` is read or written.

This approach matches Seema's original intent: every retailer is evaluated against its own historical baseline.

### Requirements

- Minimum N rows of historical data before drift checks run (e.g., 20+ weeks).
- Zero-variance columns must be skipped (avoid division by zero).
- Ridge regularization (lambda=10.0) to avoid overfitting on small datasets.
- Entire computation stays in-memory; no S3/file writes.

---

## 7. What Does NOT Change

- Alert delivery mechanism (Slack Lambda invocation)
- DynamoDB persistence (`retraining_required`, `drift_metric_current`)
- Alert payload structure
- Lambda handler structure and S3 upload logic
- Threshold values (Z_OUTLIER_YELLOW=3.0, Z_OUTLIER_RED=4.0, etc.)

---

## 8. Timeline

| Phase | Action | Status |
|-------|--------|--------|
| RCA | Identify root cause | **Complete** |
| Plan | Design dynamic per-retailer drift detection | Pending |
| Implement | Refactor `detect_spend_regime_shift` and `detect_kpi_behavior_break` | Pending |
| Test | Validate against known-good and injected-case datasets | Pending |
| Deploy | Release to dev, then staging/prod | Pending |
