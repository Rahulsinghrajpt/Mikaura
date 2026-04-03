# SOT Schema Injection — Automatic Column Backfill in Data Ingestion

## Overview

The data ingestion pipeline enforces a **universal Source of Truth (SOT) schema** across all destination files, regardless of what columns a client provides in their source CSV. When a source file is missing one or more SOT columns, the pipeline automatically adds those columns to the destination file and fills them with a safe default value (`0` for numeric columns, `''` for text columns).

This behaviour is implemented in the `reorder_columns_sot` function inside:

```
data_ingestion_pipeline/lambdas/mmm_dev_data_transfer/lambda_function.py
```

---

## Universal SOT Schema

The pipeline defines 11 mandatory columns that every destination file must contain, in this exact order:

| # | Column Name | Type | Default if Missing |
|---|---|---|---|
| 1 | `Date` | text (YYYY-MM-DD) | `''` (empty string) |
| 2 | `Sales` | numeric | `0` |
| 3 | `In_stock_Rate` | numeric | `0` |
| 4 | `GQV` | numeric | `0` |
| 5 | `OOH_impressions` | numeric | `0` |
| 6 | `OOH_spend` | numeric | `0` |
| 7 | `PaidSocial_impressions` | numeric | `0` |
| 8 | `PaidSocial_spend` | numeric | `0` |
| 9 | `TV_impressions` | numeric | `0` |
| 10 | `TV_spend` | numeric | `0` |
| 11 | `Promo_flag` | text | `''` (empty string) |

After the 11 SOT columns, the pipeline appends:
- Retailer-specific **spend** columns (e.g. `Amazon_Ads_spend`, `Google_spend`)
- Retailer-specific **impression** columns (e.g. `Amazon_Ads_impression`, `Google_impression`)
- Any remaining extra columns from the source
- `Promo_flag` is always placed **last**, regardless of position above

---

## How It Works — Step by Step

```
Source CSV (client-provided)
         │
         ▼
┌─────────────────────────────────────────────────────────┐
│  reorder_columns_sot(df, retailer_id)                   │
│                                                         │
│  1. Loop through universal_sot_cols (11 columns)        │
│     ├── Column exists in source → keep it               │
│     └── Column missing from source → record as missing  │
│                                                         │
│  2. For each missing SOT column:                        │
│     ├── Promo_flag / Date → fill with ''                │
│     └── All other numeric columns → fill with 0         │
│                                                         │
│  3. Append retailer-specific spend columns              │
│  4. Append retailer-specific impression columns         │
│  5. Append any remaining extra source columns           │
│  6. Move Promo_flag to the very last position           │
└─────────────────────────────────────────────────────────┘
         │
         ▼
Destination CSV (standardised, consistent schema)
```

### Relevant code

```python
# universal_sot_cols definition — lambda_function.py line 2181
universal_sot_cols = [
    'Date', 'Sales', 'In_stock_Rate', 'GQV',
    'OOH_impressions', 'OOH_spend',
    'PaidSocial_impressions', 'PaidSocial_spend',
    'TV_impressions', 'TV_spend',
    'Promo_flag'
]

# Missing column injection — lambda_function.py line 2213
for missing_col in missing_sot_cols:
    if missing_col == 'Promo_flag':
        df[missing_col] = ''        # text column
    elif missing_col == 'Date':
        df[missing_col] = ''        # should exist already, safety fallback
    else:
        df[missing_col] = 0         # numeric columns default to zero
```

Column matching is **case-insensitive** — `ooh_impressions`, `OOH_Impressions`, and `OOH_impressions` are all treated as the same column.

---

## Real Example: auraqa1 (Amazon)

### Source file columns

```
Week (From Mon), Sales, Amazon_Ads_impression, Amazon_Ads_spend,
Google_impression, Google_spend, Meta_impression, Meta_spend,
OOH_impression, OOH_spend, TikTok_impression, TikTok_spend,
TV_impression, TV_spend, GQV
```

### SOT columns present vs missing

| SOT Column | Present in Source? | Action |
|---|---|---|
| `Date` | Derived from `Week (From Mon)` | Renamed and reformatted |
| `Sales` | Yes | Kept as-is |
| `In_stock_Rate` | **No** | **Added with value `0`** |
| `GQV` | Yes | Kept as-is |
| `OOH_impressions` | **No** (source has `OOH_impression` singular) | **Added with value `0`** |
| `OOH_spend` | Yes | Kept as-is |
| `PaidSocial_impressions` | **No** | **Added with value `0`** |
| `PaidSocial_spend` | **No** | **Added with value `0`** |
| `TV_impressions` | **No** (source has `TV_impression` singular) | **Added with value `0`** |
| `TV_spend` | Yes | Kept as-is |
| `Promo_flag` | **No** | **Added with value `''`** |

> Note: `OOH_impression` (singular) and `TV_impression` (singular) from the source are **not** matched to the SOT columns `OOH_impressions` and `TV_impressions` (plural) because they carry different normalized names. Both the source singular and the injected plural columns appear in the destination.

### Destination file columns

```
Date, Sales, In_stock_Rate(=0), GQV, OOH_impressions(=0), OOH_spend,
PaidSocial_impressions(=0), PaidSocial_spend(=0), TV_impressions(=0), TV_spend,
Amazon_Ads_spend, Google_spend, Meta_spend, TikTok_spend,
Amazon_Ads_impression, Google_impression, Meta_impression,
OOH_impression, TikTok_impression, TV_impression,
Promo_flag(='')
```

---

## Why This Design Was Chosen

### 1. Consistent schema for downstream consumers
Model training, dashboards, and monitoring tools all expect a fixed set of columns. Without SOT injection, every client's file would have a different shape and downstream code would need per-client schema handling.

### 2. Zero is mathematically correct for missing media channels
In MMM (Marketing Mix Modelling), a channel that was not active in a given week should have `0` spend and `0` impressions — not `null` or missing. Defaulting to `0` is statistically safe and avoids NaN propagation in model training.

### 3. Retailer-specific columns are additive
Columns like `Amazon_Ads_spend` or `Google_impression` are **not** in the universal SOT — they are appended after the 11 mandatory columns. This keeps the schema extensible without changing the fixed SOT contract.

---

## Important Notes and Edge Cases

### Singular vs. Plural impression column naming

The source file uses singular names (`OOH_impression`, `TV_impression`) while the SOT requires plural (`OOH_impressions`, `TV_impressions`). Because normalized matching compares full column names (not prefixes), these **do not match** and the SOT plural version is injected as `0`, while the source singular version is preserved as an extra column. This results in two similar-looking impression columns in the destination.

**Recommendation:** Clients should be onboarded with plural column names (`OOH_impressions`, `TV_impressions`) to avoid this duplication.

### Schema drift alerting

If a destination file is missing expected SOT columns or has unexpected extra columns, a **schema drift Slack alert** is fired. This helps detect when a client's source schema changes unexpectedly. See `send_schema_drift_alert` in `lambda_function.py`.

### Logging

Every injected column is logged at INFO level with the tag `TASK 5`:
```
TASK 5: Added missing SOT column 'PaidSocial_impressions' with default value
```
These logs are queryable in CloudWatch under the `mmm_{env}_data_transfer` Lambda log group.

---

## Related Code References

| Item | Location |
|---|---|
| `reorder_columns_sot` function | `lambda_function.py` line 2157 |
| `universal_sot_cols` list | `lambda_function.py` line 2181 |
| Missing column injection loop | `lambda_function.py` line 2213 |
| Schema drift detection | `lambda_function.py` line 3694 |
| `send_schema_drift_alert` function | `lambda_function.py` (search `def send_schema_drift_alert`) |
| Column normalisation | `lambda_function.py` `normalize_header_for_matching` |
