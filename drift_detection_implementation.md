# Data Drift Detection — Implementation Guide

> **JIRA tickets:** ME-5401 (Spend Regime Shift), ME-5402 (KPI Behavior Break)
>
> **Source files:**
> | File | Purpose |
> |------|---------|
> | `lambdas/mmm_dev_data_transfer/lambda_function.py` | Detection logic, alerting, action handlers |
> | `lambdas/mmm_dev_data_transfer/dynamic_drift_profile.py` | Schema inference, profile building, data preparation |

---

## 1. Architecture Overview

```
  ┌──────────────────────────────────────────────────────────┐
  │                    S3 Tracer Bucket                       │
  │  tracer/2026-04-client_brand_retailer_123.csv            │
  └────────────────────────┬─────────────────────────────────┘
                           │  S3 Event triggers Lambda
                           ▼
  ┌──────────────────────────────────────────────────────────┐
  │             mmm_dev_data_transfer Lambda                  │
  │                                                          │
  │  1. Download CSV from tracer bucket                      │
  │  2. Validate file content                                │
  │  3. Transform + split by retailer                        │
  │  4. Upload processed data to client VIP bucket           │
  │  5. ── DRIFT DETECTION (runs per-retailer) ──            │
  │     a. prepare_drift_data()  → historical + latest       │
  │     b. infer_schema()        → column roles              │
  │     c. build_reference_profile() → statistical baseline  │
  │     d. detect_spend_regime_shift()  (ME-5401)            │
  │     e. detect_kpi_behavior_break()  (ME-5402)            │
  │  6. Alert + persist on detection                         │
  └────────┬────────────────────────┬────────────────────────┘
           │                        │
           ▼                        ▼
  ┌────────────────┐     ┌──────────────────────┐
  │  Slack Lambda   │     │  DynamoDB             │
  │  (async alert)  │     │  pipeline-infos table │
  │                 │     │  retraining_required  │
  │                 │     │  drift_metric_current │
  └────────────────┘     └──────────────────────┘
```

The entire drift detection pipeline runs **inline** during the data transfer Lambda execution. There is no separate scheduled job — drift is evaluated each time a new CSV lands in the tracer bucket.

---

## 2. Data Preparation — `prepare_drift_data()`

**File:** `dynamic_drift_profile.py` lines 383–413

Before any drift check runs, the incoming DataFrame is split into two parts:

```
┌───────────────────────────────────────────────────────────┐
│  Full DataFrame (N rows, sorted by date)                   │
│                                                            │
│  Row 1  ─┐                                                 │
│  Row 2   │  historical_df  (N-1 rows)                      │
│  ...     │  → Used to build the reference profile           │
│  Row N-1 ┘                                                 │
│  Row N   ── latest_df (1 row)                              │
│             → The "new" data point being scored             │
└───────────────────────────────────────────────────────────┘
```

**Steps:**

1. If the `Date` column exists, parse it to datetime and sort chronologically.
2. Split: all rows except the last = `historical_df`, last row = `latest_df`.
3. Check eligibility: `historical_df` must have at least `MIN_ROWS_FOR_DRIFT = 20` rows.

```python
historical_df, latest_df, is_eligible = prepare_drift_data(df)
```

**Eligibility gate:** If there are fewer than 20 historical rows, drift detection is skipped entirely with an info log.

---

## 3. Schema Inference — `infer_schema()`

**File:** `dynamic_drift_profile.py` lines 95–165

The schema inference automatically classifies every column in the retailer's CSV into one of five roles. This makes drift detection **retailer-agnostic** — no hardcoded column names.

### Classification Rules

| Role | Detection Logic | Examples |
|------|----------------|----------|
| **Date** | Defaults to `"Date"` (configurable) | `Date`, `Week (From Mon)` |
| **KPI** | First column ending with `_sales`, else fallback to `Sales` | `Target_sales`, `Bestbuy_sales` |
| **Spend** | Columns ending with `_spend` | `Google_spend`, `TV_spend`, `OOH_spend` |
| **Control** | Known names: `GQV`, `Seasonality` | `GQV`, `Seasonality` |
| **Event** | Known holidays + any binary (0/1) column not already classified | `Christmas`, `SuperBowl`, `Valentine` |

### Known Column Lists

```python
KNOWN_CONTROL_COLS = ["GQV", "Seasonality"]
KNOWN_EVENT_COLS = [
    "Valentine", "Easter", "Thanksgiving", "Christmas",
    "SuperBowl", "CyberMonday", "NewYear", "BackToSchool",
]
```

### Binary Column Discovery

Any unclassified column that contains only `{0, 1}` values is automatically added as an event column. The check uses `_is_binary_series()`.

### Output

```python
{
    "date_col": "Date",
    "kpi_col": "Target_sales",
    "spend_cols": ["Google_spend", "Meta_spend", "OOH_spend", "TV_spend"],
    "control_cols": ["GQV", "Seasonality"],
    "event_cols": ["BackToSchool", "Christmas", "CyberMonday", "Easter", ...]
}
```

**Skip conditions:** If no KPI column or no spend columns are detected, drift checks are skipped with a warning log.

---

## 4. Reference Profile Building — `build_reference_profile()`

**File:** `dynamic_drift_profile.py` lines 212–376

This function takes the `historical_df` (all rows except the latest) and the inferred `schema`, and computes a complete statistical baseline. Everything runs **in-memory** with no file I/O.

The profile has five sections:

### 4.1 Spend Statistics (`spend_stats`)

For each spend column, compute descriptive statistics from the historical data:

```python
spend_stats[channel] = {
    "mean":  np.mean(values),           # historical average
    "std":   np.std(values, ddof=1),    # sample standard deviation
    "p99":   np.quantile(values, 0.99), # 99th percentile
    "p995":  np.quantile(values, 0.995),# 99.5th percentile
    "active_weeks": count(values > 0),  # weeks with nonzero spend
}
```

These are consumed by ME-5401 (Spend Regime Shift).

### 4.2 Control Statistics (`control_stats`)

For each control column (GQV, Seasonality):

```python
control_stats[col] = {
    "mean": np.mean(values),
    "std":  np.std(values, ddof=1),
    "p01":  np.quantile(values, 0.01),
    "p99":  np.quantile(values, 0.99),
}
```

Used for z-scoring control features in the ridge regression.

### 4.3 Spend Mix Baseline (`mix_profile`)

Computes the average spend allocation across channels:

```
For each row where total_spend > 0:
    share[channel] = channel_spend / total_spend

avg_share = mean(share) across all eligible rows
```

This captures the typical channel mix (e.g., 40% Google, 30% TV, 30% OOH).

### 4.4 KPI Residual Profile (`residual_profile`) — Ridge Regression

This is the core of ME-5402. A **ridge regression** model is fitted to predict `log1p(KPI)` from historical features:

**Target variable:** `y = log1p(KPI_sales)`

**Feature matrix columns:**

| Feature Type | Transformation | Example |
|---|---|---|
| Spend | `log1p(spend_value)` | `log1p(Google_spend)` |
| Control | `z-score = (value - mean) / std` | `z(GQV)` |
| Event | Raw binary (0 or 1) | `Christmas` |
| Intercept | Always 1.0 | `intercept` |

**Ridge regression formula:**

```
beta = solve( X^T X + lambda * I,  X^T y )
yhat = X @ beta
residuals = y - yhat
```

Where `lambda = 10.0` (regularization to prevent overfitting).

**Stored profile values:**

```python
residual_profile = {
    "feature_names": ["log1p(Google_spend)", "z(GQV)", "Christmas", "intercept"],
    "beta": [0.12, 0.05, 0.03, 12.8],   # regression coefficients
    "resid_mean": 0.001,                   # mean of training residuals
    "resid_std": 0.045,                    # std of training residuals
}
```

### 4.5 Thresholds

All detection thresholds are bundled into the profile:

```python
thresholds = {
    "Z_OUTLIER_YELLOW": 3.0,    # ME-5401 yellow threshold
    "Z_OUTLIER_RED": 4.0,       # ME-5401 red threshold
    "RESID_Z_YELLOW": 3.0,      # ME-5402 yellow threshold
    "RESID_Z_RED": 4.0,         # ME-5402 red threshold
    "MIX_SHIFT_YELLOW": 0.20,   # spend mix shift (reserved)
    "MIX_SHIFT_RED": 0.30,
    "JS_YELLOW": 0.06,          # Jensen-Shannon divergence (reserved)
    "JS_RED": 0.10,
    "CONSEC_ZERO_WEEKS_RED": 4, # consecutive zero-spend weeks (reserved)
    "MIN_ACTIVE_WEEKS_FOR_STABILITY": 8,
}
```

---

## 5. ME-5401 — Spend Regime Shift Detection

**File:** `lambda_function.py` lines 326–400
**Function:** `detect_spend_regime_shift(retailer_df, profile)`

### Purpose

Detects when the latest week's spend for any channel is statistically abnormal compared to the retailer's own historical distribution. This catches scenarios like:
- 3x inflated spend values (data entry errors, campaign anomalies)
- Sudden spend drops or spikes

### Algorithm

For **each spend channel** in the profile:

```
1. Extract the latest row's spend value (current_value)
2. Get historical mean, std, p99, p995 from the profile
3. Compute z-score:
       z = (current_value - mean) / std
4. Check trigger conditions:
       YELLOW trigger:  |z| >= 3.0
       RED trigger:     |z| >= 4.0   OR   value > p995
5. If yellow_trigger is True → add to anomalies list
```

### Trigger Logic (Current)

```
YELLOW:  |z| >= z_yellow           (z-score alone)
RED:     |z| >= z_red  OR  value > p995   (z-score OR extreme percentile)
```

**Important:** p99 exceedance alone does NOT trigger an alert. On small datasets (~150 rows), p99 sits near the top 1–2 values, causing false positives for normal data near the historical maximum. p99 is recorded in diagnostics but not used as a standalone trigger.

p995 CAN standalone-trigger RED because it represents an extremely rare historical value.

### Default Thresholds

| Threshold | Value | Meaning |
|-----------|-------|---------|
| `Z_OUTLIER_YELLOW` | 3.0 | ~0.3% chance in normal distribution |
| `Z_OUTLIER_RED` | 4.0 | ~0.006% chance in normal distribution |

### Output

```python
{
    "detected": True,
    "severity": "RED",           # worst severity across all channels
    "drift_metric_current": 9.57, # max |z| across all channels
    "evaluated_channels": 4,
    "anomalies": [
        {
            "channel": "OOH_spend",
            "value": 385647.66,
            "mean": 72000.0,
            "std": 20600.0,
            "z_score": 9.5666,
            "p99": 131240.53,
            "p995": 133000.0,
            "threshold_breaches": {
                "z_yellow": True,
                "z_red": True,
                "p99": True,
                "p995": True
            },
            "severity": "RED"
        }
    ]
}
```

### Visual Flow

```
For each spend channel:
    ┌──────────────┐
    │ current_value │
    └──────┬───────┘
           │
    z = (current - mean) / std
           │
           ▼
    ┌──────────────────┐     ┌──────────────────┐
    │ |z| >= 3.0?      │──NO─│ Not an anomaly   │
    │ (YELLOW trigger)  │     └──────────────────┘
    └──────┬───────────┘
           │ YES
           ▼
    ┌──────────────────┐
    │ |z| >= 4.0?      │──YES── severity = RED
    │  OR val > p995?  │
    └──────┬───────────┘
           │ NO
           ▼
      severity = YELLOW
```

---

## 6. ME-5402 — KPI Behavior Break Detection

**File:** `lambda_function.py` lines 604–742
**Function:** `detect_kpi_behavior_break(retailer_df, profile)`

### Purpose

Detects when the relationship between media spend and KPI (sales) has changed in a way the model doesn't expect. This catches:
- KPI dropping despite stable or increasing spend (model degradation)
- KPI spiking without corresponding spend increases (external factors)
- Directional mismatches between spend and KPI trends

### Algorithm

#### Step 1: Zero-Spend Guard

```python
if latest_total_spend < 1e-9:
    return not_detected  # model has no signal when all spend is zero
```

When all spend channels are zero, the ridge regression model prediction is unreliable (reduces to intercept only), producing large spurious residuals. Detection is skipped.

#### Step 2: Predict Expected KPI

Using the ridge regression coefficients from the profile:

```
predicted_log_kpi = Σ (beta[i] * feature[i])

Where features are built from the latest row:
  - log1p(channel_spend) for each spend column
  - z-scored control values using historical mean/std
  - binary event flags
  - intercept = 1.0
```

**Function:** `_predict_log_kpi_from_profile(latest_row, profile)`

If the prediction fails (missing profile), falls back to `log1p(previous_kpi)`.

#### Step 3: Compute Residual Z-Score

```
actual_log_kpi = log1p(latest_kpi)
residual = actual_log_kpi - predicted_log_kpi
resid_z = (residual - resid_mean) / resid_std
```

A high `|resid_z|` means the actual KPI deviated significantly from what the model predicted given the spend levels.

#### Step 4: Check Opposite Direction

```
spend_delta = latest_total_spend - previous_total_spend
kpi_delta   = latest_kpi - previous_kpi

opposite_direction = (
    spend and kpi moved in opposite directions
    AND |resid_z| >= 1.5  (minimum evidence threshold)
    AND |spend_delta| / mean_total_spend >= 15%  (spend change is material)
)
```

#### Step 5: Event Transition Suppression

When a holiday event transitions from active (1) to inactive (0), KPI typically drops even if spend also drops. This is expected seasonal behavior, not a real anomaly.

```python
if event_transition_ended AND |resid_z| <= 1.0:
    opposite_direction = False  # suppress the false positive
```

**Function:** `_is_event_transition_ended(previous_row, latest_row, event_cols)`

#### Step 6: Final Decision

```
detected = (|resid_z| >= resid_yellow)  OR  opposite_direction

severity:
  RED    if |resid_z| >= 4.0
  YELLOW if |resid_z| >= 3.0
```

### Default Thresholds

| Threshold | Value | Purpose |
|-----------|-------|---------|
| `RESID_Z_YELLOW` | 3.0 | Residual z-score for yellow alert |
| `RESID_Z_RED` | 4.0 | Residual z-score for red alert |
| `OPPOSITE_MIN_RESID_Z` | 1.5 | Minimum |z| for opposite-direction to count |
| `OPPOSITE_MIN_SPEND_DELTA_PCT` | 0.15 | Spend change must be >= 15% of mean |
| `EVENT_TRANSITION_SUPPRESS_MAX_RESID_Z` | 1.0 | Suppress opposite-direction if |z| <= this after event ends |

### Output

```python
{
    "detected": True,
    "severity": "RED",
    "drift_metric_current": 6.066,   # |resid_z|
    "opposite_direction": False,
    "event_transition_ended": False,
    "anomalies": [
        {
            "kpi_column": "Target_sales",
            "latest_kpi": 840012.54,
            "previous_kpi": 813384.82,
            "kpi_delta": 26627.72,
            "latest_total_spend": 555921.66,
            "previous_total_spend": 343998.18,
            "spend_delta": 211923.48,
            "spend_delta_pct": 1.523,
            "residual": 0.285,
            "residual_z": 6.066,
            "threshold_breaches": {
                "residual_yellow": True,
                "residual_red": True,
                "raw_opposite_direction": False,
                "opposite_direction": False,
                "event_transition_ended": False
            },
            "severity": "RED"
        }
    ]
}
```

### Visual Flow

```
                ┌─────────────────────────────┐
                │ latest row from DataFrame    │
                └─────────────┬───────────────┘
                              │
             ┌────────────────┴────────────────┐
             │ total spend == 0?               │──YES── SKIP (no signal)
             └────────────────┬────────────────┘
                              │ NO
                              ▼
              ┌──────────────────────────────┐
              │ Predict log(KPI) using ridge │
              │ regression from profile       │
              └──────────────┬───────────────┘
                              │
                              ▼
              ┌──────────────────────────────┐
              │ residual = actual - predicted │
              │ resid_z = (r - mean) / std   │
              └──────────────┬───────────────┘
                              │
              ┌───────────────┴──────────────────┐
              │                                   │
              ▼                                   ▼
    ┌─────────────────┐             ┌──────────────────────┐
    │ |resid_z| >= 3.0│             │ Opposite direction?  │
    │ (residual trig) │             │ spend↑ + kpi↓ (or ↓↑)│
    └────────┬────────┘             │ AND |z| >= 1.5       │
             │                      │ AND Δspend >= 15%    │
             │                      └──────────┬───────────┘
             │                                  │
             │    ┌─────────────────────────┐   │
             │    │ Event transition ended?  │   │
             │    │ AND |z| <= 1.0?          │───┘
             │    │ → suppress opposite_dir  │
             │    └─────────────────────────┘
             │                 │
             ▼                 ▼
       ┌───────────────────────────┐
       │ detected = residual_trig  │
       │          OR opposite_dir  │
       └───────────┬───────────────┘
                   │
         ┌─────────┴──────────┐
         │ |z| >= 4.0 → RED   │
         │ |z| >= 3.0 → YELLOW│
         └────────────────────┘
```

---

## 7. Actions on Detection

When drift is detected, two actions are taken:

### 7.1 Slack Alert (Async)

The data transfer Lambda invokes the Slack Lambda (`mmm_{env}_data_ingestion_slack`) asynchronously via `InvocationType='Event'`.

**ME-5401 payload:**

```python
{
    "alert_type": "SPEND_REGIME_SHIFT",
    "client_id": "aurademo",
    "brand_name": "target",
    "retailer_id": "walmart",
    "filename": "tracer/2026-04-aurademo_target_walmart_123.csv",
    "severity": "RED",
    "drift_metric_current": 9.57,
    "evaluated_channels": 4,
    "anomalies": [...],
    "message": "Spend regime shift detected. Retraining has been marked as required."
}
```

**ME-5402 payload:**

```python
{
    "alert_type": "KPI_BEHAVIOR_BREAK",
    "client_id": "aurademo",
    "brand_name": "target",
    "retailer_id": "walmart",
    "filename": "tracer/...",
    "severity": "YELLOW",
    "drift_metric_current": 3.4,
    "anomalies": [...],
    "correlation_id": "kpi-break-a1b2c3d4e5f6",
    "message": "KPI behavior break detected. Retraining has been marked as required."
}
```

ME-5402 alerts include retry logic (default 3 retries with exponential backoff) and payload validation before invoking.

### 7.2 DynamoDB Persistence

Using `PipelineInfoHelper`, the Lambda updates the `pipeline-infos` DynamoDB table:

```python
helper.update_drift_metrics(
    client_id="aurademo",
    brand_name="target",
    retailer_id="walmart",
    drift_metric_current=9.57,       # the max |z| or |resid_z|
    retraining_required=True          # flags the model for retraining
)
```

This sets `retraining_required=True` so downstream systems know the model needs to be retrained with newer data.

---

## 8. False Positive Guards

Several guards are in place to prevent false alerts:

| Guard | Applies To | What It Prevents |
|-------|-----------|-----------------|
| **MIN_ROWS_FOR_DRIFT = 20** | Both | Skips drift checks when historical data is too small for reliable statistics |
| **p99 not a standalone trigger** | ME-5401 | On ~150-row datasets, p99 sits near the max. Normal high values would false-trigger. Only z-score triggers YELLOW. |
| **p995 standalone for RED only** | ME-5401 | p995 is rare enough (top 0.5%) to standalone-trigger at RED severity |
| **Zero total spend guard** | ME-5402 | When all spend = 0, ridge model has no signal. Residual is noise, not drift. |
| **Event transition suppression** | ME-5402 | After a holiday ends (Christmas 1→0), KPI drop with spend drop is expected, not anomalous |
| **Opposite direction requires evidence** | ME-5402 | Needs |resid_z| >= 1.5 AND spend change >= 15% of mean — prevents weak signals from firing |
| **Payload validation** | ME-5402 | Validates alert payload before invoking Slack Lambda to prevent bad data from reaching alerts |

---

## 9. Lambda Handler Integration

**File:** `lambda_function.py` lines 4083–4180

The drift detection block runs inside the main `process_file()` function, after data transformation and upload:

```python
if PANDAS_AVAILABLE and DYNAMIC_DRIFT_AVAILABLE:
    # 1. Read CSV into DataFrame
    df = pd.read_csv(io.BytesIO(retailer_data))

    # 2. Split into historical + latest
    historical_df, latest_df, is_eligible = prepare_drift_data(df)

    if is_eligible:
        # 3. Infer schema from column names
        schema = infer_schema(df)

        if schema['kpi_col'] and schema['spend_cols']:
            # 4. Build profile from this retailer's history
            drift_profile = build_reference_profile(historical_df, schema)

            # 5. ME-5401: Spend Regime Shift
            spend_result = detect_spend_regime_shift(df, drift_profile)
            if spend_result['detected']:
                handle_spend_regime_shift_actions(...)

            # 6. ME-5402: KPI Behavior Break
            kpi_result = detect_kpi_behavior_break(df, drift_profile)
            if kpi_result['detected']:
                handle_kpi_behavior_break_actions(...)
```

The entire block is wrapped in `try/except` — drift detection failures are logged as warnings but do **not** fail the data transfer.

### Import Guard

`dynamic_drift_profile.py` is imported conditionally:

```python
DYNAMIC_DRIFT_AVAILABLE = False
try:
    from dynamic_drift_profile import (
        infer_schema, build_reference_profile,
        prepare_drift_data, MIN_ROWS_FOR_DRIFT,
    )
    DYNAMIC_DRIFT_AVAILABLE = True
except ImportError:
    DYNAMIC_DRIFT_AVAILABLE = False
```

If the module is not deployed in the Lambda package, drift detection is silently skipped.

---

## 10. Key Mathematical Formulas

### Z-Score (ME-5401)

```
z = (current_value - historical_mean) / historical_std
```

Measures how many standard deviations the current value is from the historical average. A z-score of 3.0 means the value is 3 standard deviations away — this has a ~0.3% probability under a normal distribution.

### Ridge Regression (ME-5402 Profile Building)

```
beta = (X^T X + λI)^{-1} X^T y

Where:
  X = feature matrix [log1p(spend), z(controls), events, intercept]
  y = log1p(KPI) vector
  λ = 10.0 (regularization parameter)
  I = identity matrix
```

The regularization term `λI` prevents overfitting by penalizing large coefficients, which is important when:
- Features are correlated (multiple spend channels)
- Sample sizes are small (~50–150 rows)

### Residual Z-Score (ME-5402 Detection)

```
predicted_log_kpi = Σ beta[i] * feature[i]
actual_log_kpi = log1p(actual_kpi)
residual = actual_log_kpi - predicted_log_kpi
resid_z = (residual - resid_mean) / resid_std
```

A high |resid_z| means the actual KPI deviated significantly from what the model expected given the spend levels.

### Spend Delta Percentage (ME-5402 Opposite Direction)

```
spend_delta_pct = |latest_total_spend - previous_total_spend| / mean_total_spend
```

Where `mean_total_spend` is the sum of all channels' historical mean values from the profile.

---

## 11. Threshold Reference Table

| Constant | Value | Used By | Purpose |
|----------|-------|---------|---------|
| `SPEND_REGIME_Z_YELLOW_DEFAULT` | 3.0 | ME-5401 | Yellow alert threshold for spend z-score |
| `SPEND_REGIME_Z_RED_DEFAULT` | 4.0 | ME-5401 | Red alert threshold for spend z-score |
| `KPI_BEHAVIOR_RESID_Z_YELLOW_DEFAULT` | 3.0 | ME-5402 | Yellow alert threshold for residual z-score |
| `KPI_BEHAVIOR_RESID_Z_RED_DEFAULT` | 4.0 | ME-5402 | Red alert threshold for residual z-score |
| `KPI_BEHAVIOR_OPPOSITE_MIN_RESID_Z_DEFAULT` | 1.5 | ME-5402 | Minimum |z| for opposite-direction signal |
| `KPI_BEHAVIOR_OPPOSITE_MIN_SPEND_DELTA_PCT_DEFAULT` | 0.15 | ME-5402 | Spend change must be >= 15% of mean |
| `KPI_BEHAVIOR_EVENT_TRANSITION_SUPPRESS_MAX_RESID_Z_DEFAULT` | 1.0 | ME-5402 | Suppress after event ends if |z| <= this |
| `MIN_ROWS_FOR_DRIFT` | 20 | Both | Minimum historical rows required |
| `RIDGE_LAMBDA` | 10.0 | Profile | Ridge regression regularization parameter |
| `KPI_ALERT_MAX_RETRIES` | 3 | ME-5402 | Max retries for Slack alert invocation |
| `KPI_ALERT_RETRY_BASE_SECONDS` | 1.0 | ME-5402 | Base delay for exponential backoff |

---

## 12. File-Level Code Map

### `dynamic_drift_profile.py`

| Lines | Function | Purpose |
|-------|----------|---------|
| 84–92 | `_is_binary_series()` | Checks if a column is binary (0/1) |
| 95–165 | `infer_schema()` | Auto-classifies column roles |
| 172–176 | `_safe_log1p_scalar()` | Numerically safe log1p for scalars |
| 179–183 | `_safe_log1p_array()` | Numerically safe log1p for arrays |
| 186–192 | `_robust_percentile()` | Percentile ignoring non-finite values |
| 195–209 | `_ridge_fit()` | Ridge regression fit |
| 212–376 | `build_reference_profile()` | Builds complete statistical profile |
| 383–413 | `prepare_drift_data()` | Sorts by date, splits historical/latest |

### `lambda_function.py` (drift-related functions only)

| Lines | Function | Purpose |
|-------|----------|---------|
| 297–303 | Threshold constants | Default values for all drift thresholds |
| 313–320 | `_to_float()` | Safe float conversion |
| 326–400 | `detect_spend_regime_shift()` | ME-5401 detection logic |
| 403–452 | `send_spend_regime_shift_alert()` | Sends Slack alert for ME-5401 |
| 455–502 | `handle_spend_regime_shift_actions()` | Alert + DynamoDB update for ME-5401 |
| 505–509 | `_safe_log1p()` | Numerically safe log1p |
| 512–519 | `_is_event_transition_ended()` | Checks if event flag went 1→0 |
| 522–530 | `_get_total_spend_mean()` | Sums mean spend across channels |
| 533–548 | `_validate_kpi_alert_payload()` | Validates alert payload |
| 551–601 | `_predict_log_kpi_from_profile()` | Ridge prediction for latest row |
| 604–742 | `detect_kpi_behavior_break()` | ME-5402 detection logic |
| 745–854 | `send_kpi_behavior_break_alert()` | Sends Slack alert (with retries) for ME-5402 |
| 857–906 | `handle_kpi_behavior_break_actions()` | Alert + DynamoDB update for ME-5402 |
| 4083–4180 | Handler integration block | Orchestrates drift checks in process_file() |

---

## 13. Example: End-to-End Walkthrough

### Input

A CSV with 158 rows lands in the tracer bucket:
`tracer/2026-03-aurademo_aurademobrand_target_1234.csv`

The last row is an injected test case with 3x inflated spend (CASE_1_SPEND_SHOCK_3X).

### Step 1: Data Preparation

```
prepare_drift_data(df) →
  historical_df: 157 rows (2023-01-02 to 2025-12-29)
  latest_df: 1 row (2026-01-05, the injected row)
  is_eligible: True (157 >= 20)
```

### Step 2: Schema Inference

```
infer_schema(df) →
  kpi_col: "Target_sales"
  spend_cols: ["Google_spend", "Meta_spend", "OOH_spend",
               "Paid_Social_spend", "Target_spend", "TV_spend"]
  control_cols: ["GQV", "Seasonality"]
  event_cols: ["BackToSchool", "Christmas", "CyberMonday",
               "Easter", "NewYear", "SuperBowl", "Thanksgiving", "Valentine"]
```

### Step 3: Profile Building

```
build_reference_profile(historical_df, schema) →
  spend_stats:
    OOH_spend: {mean: 72103, std: 22744, p99: 131240, p995: 133150}
    TV_spend:  {mean: 119487, std: 36891, p99: 235862, p995: 240000}
    ...
  residual_profile:
    beta: [0.12, 0.08, 0.15, ..., 13.2]
    resid_std: 0.047
```

### Step 4: ME-5401 Detection

For OOH_spend in the injected row:
```
current = 385647.66
z = (385647.66 - 72103) / 22744 = 13.79
|z| = 13.79 >= 3.0 → YELLOW trigger ✓
|z| = 13.79 >= 4.0 → RED ✓
```

Result: **RED alert** with 3 flagged channels (OOH_spend, TV_spend, Target_spend).

### Step 5: ME-5402 Detection

```
total spend latest = 687,762 (non-zero → passes guard)
predicted log(KPI) = 13.45 (from ridge model)
actual log(KPI) = log1p(840012) = 13.64
residual = 13.64 - 13.45 = 0.19
resid_z = (0.19 - 0.001) / 0.047 = 4.02
|resid_z| = 4.02 >= 3.0 → detected
|resid_z| = 4.02 >= 4.0 → RED
```

Result: **RED alert** with residual_z = 4.02.

### Step 6: Actions

1. Slack Lambda invoked async with SPEND_REGIME_SHIFT payload
2. Slack Lambda invoked async with KPI_BEHAVIOR_BREAK payload
3. DynamoDB `pipeline-infos` updated: `retraining_required = true`
