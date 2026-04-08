"""
Dynamic Drift Profile Builder for per-retailer drift detection.

Ported from: Weekly_MMM_Guardrails_With_Injected_Cases.ipynb (Seema's script)

Purpose:
    Dynamically infer schema and build a reference profile from each
    retailer's own historical data, instead of relying on a static
    profile.json that was built from a single retailer (Target).

Usage (inside lambda_function.py):
    from dynamic_drift_profile import (
        infer_schema,
        build_reference_profile,
        prepare_drift_data,
        MIN_ROWS_FOR_DRIFT,
    )

    historical_df, latest_df, is_eligible = prepare_drift_data(df)
    if is_eligible:
        schema = infer_schema(df)
        profile = build_reference_profile(historical_df, schema)
        # pass `profile` to detect_spend_regime_shift() / detect_kpi_behavior_break()

See also:
    RCA_drift_detection_false_alerts.md — Root cause analysis for ME-5401/ME-5402
"""

import math
from typing import Any, Dict, List, Optional, Tuple

# pandas is expected to be available via Lambda layer
try:
    import pandas as pd
    PANDAS_AVAILABLE = True
except ImportError:
    PANDAS_AVAILABLE = False

# numpy is expected to be available via Lambda layer (bundled with pandas)
try:
    import numpy as np
    NUMPY_AVAILABLE = True
except ImportError:
    NUMPY_AVAILABLE = False


# ============================================================================
# Constants
# ============================================================================

# Column naming conventions (match Seema's notebook)
SPEND_SUFFIX = "_spend"
IMPR_SUFFIX = "_impression"
SALES_SUFFIX = "_sales"

# Known control and event columns
KNOWN_CONTROL_COLS = ["GQV", "Seasonality"]
KNOWN_EVENT_COLS = [
    "Valentine", "Easter", "Thanksgiving", "Christmas",
    "SuperBowl", "CyberMonday", "NewYear", "BackToSchool",
]

# Profile building parameters
MIN_ROWS_FOR_DRIFT = 20              # minimum historical rows required
MIN_ACTIVE_WEEKS_FOR_STABILITY = 8   # minimum weeks a channel must be active
RIDGE_LAMBDA = 10.0                  # regularization parameter for ridge regression

# Drift detection thresholds (must match lambda_function.py defaults)
Z_OUTLIER_YELLOW = 3.0
Z_OUTLIER_RED = 4.0
MIX_SHIFT_YELLOW = 0.20
MIX_SHIFT_RED = 0.30
JS_YELLOW = 0.06
JS_RED = 0.10
RESID_Z_YELLOW = 3.0
RESID_Z_RED = 4.0
CONSEC_ZERO_WEEKS_RED = 4


# ============================================================================
# Schema Inference
# ============================================================================

def _is_binary_series(series: Any) -> bool:
    """Check if a pandas Series contains only 0/1 values (binary indicator)."""
    if not PANDAS_AVAILABLE or not NUMPY_AVAILABLE:
        return False
    vals = pd.to_numeric(series.dropna(), errors="coerce")
    if len(vals) == 0:
        return False
    unique_vals = set(np.unique(vals))
    return unique_vals.issubset({0, 1, 0.0, 1.0})


def infer_schema(
    df: Any,
    date_col: str = "Date",
    kpi_col: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Dynamically infer column roles from a retailer's DataFrame columns.

    This function mirrors the `infer_schema()` logic from Seema's notebook:
    - Spend columns: end with '_spend'
    - Impression columns: end with '_impression'
    - KPI column: auto-detected as first '*_sales' column, or 'Sales' fallback
    - Control columns: known names (GQV, Seasonality)
    - Event columns: known event names + any remaining binary 0/1 columns

    Args:
        df: pandas DataFrame with retailer data
        date_col: name of the date column (default: "Date")
        kpi_col: explicit KPI column name, or None for auto-detection

    Returns:
        dict with keys: date_col, kpi_col, spend_cols, control_cols, event_cols
    """
    cols = list(df.columns)

    # Detect spend and impression columns by suffix
    spend_cols = sorted([c for c in cols if c.endswith(SPEND_SUFFIX)])
    impr_cols = sorted([c for c in cols if c.endswith(IMPR_SUFFIX)])

    # Auto-detect KPI column if not provided
    if kpi_col is None:
        # First try: look for a column ending in '_sales'
        sales_candidates = [c for c in cols if c.endswith(SALES_SUFFIX)]
        if sales_candidates:
            kpi_col = sales_candidates[0]
        elif "Sales" in cols:
            kpi_col = "Sales"
        # else: kpi_col remains None → drift checks will be skipped

    # Detect control columns (known names only)
    control_cols = sorted([c for c in KNOWN_CONTROL_COLS if c in cols])

    # Detect event columns: start with known names, then discover binary cols
    event_cols = [c for c in KNOWN_EVENT_COLS if c in cols]

    # Discover additional binary columns not already classified
    exclude = (
        {date_col, kpi_col}
        | set(spend_cols)
        | set(impr_cols)
        | set(control_cols)
        | set(event_cols)
    )
    # Remove None from exclude set if kpi_col was None
    exclude.discard(None)

    for c in cols:
        if c in exclude:
            continue
        if _is_binary_series(df[c]):
            event_cols.append(c)

    event_cols = sorted(set(event_cols))

    return {
        "date_col": date_col,
        "kpi_col": kpi_col,
        "spend_cols": spend_cols,
        "control_cols": control_cols,
        "event_cols": event_cols,
    }


# ============================================================================
# Profile Building
# ============================================================================

def _safe_log1p_scalar(value: float) -> float:
    """Numerically safe log1p for a single value."""
    if value <= -1:
        value = -0.999999
    return math.log1p(max(value, 0.0))


def _safe_log1p_array(arr: Any) -> Any:
    """Numerically safe log1p for a numpy array."""
    arr = np.asarray(arr, dtype=float)
    arr = np.clip(arr, 0, None)
    return np.log1p(arr)


def _robust_percentile(arr: Any, p: float) -> float:
    """Compute percentile, ignoring non-finite values."""
    arr = np.asarray(arr, dtype=float)
    arr = arr[np.isfinite(arr)]
    if len(arr) == 0:
        return float("nan")
    return float(np.quantile(arr, p))


def _ridge_fit(X: Any, y: Any, lam: float = RIDGE_LAMBDA) -> Tuple[Any, Any]:
    """
    Fit ridge regression: y = X @ beta, with regularization.

    Returns:
        beta: coefficient vector (numpy array)
        yhat: predicted values (numpy array)
    """
    X = np.asarray(X, dtype=float)
    y = np.asarray(y, dtype=float).reshape(-1, 1)
    d = X.shape[1]
    identity = np.eye(d)
    beta = np.linalg.solve(X.T @ X + lam * identity, X.T @ y).ravel()
    yhat = (X @ beta).ravel()
    return beta, yhat


def build_reference_profile(df: Any, schema: Dict[str, Any]) -> Dict[str, Any]:
    """
    Build a complete drift reference profile from a single retailer's
    historical DataFrame.

    This function mirrors `build_reference_profile()` from Seema's notebook.
    All computation is in-memory — no file I/O.

    Args:
        df: pandas DataFrame containing historical rows (all rows except latest),
            sorted by date.
        schema: dict from infer_schema() with date_col, kpi_col, spend_cols, etc.

    Returns:
        dict compatible with detect_spend_regime_shift() and
        detect_kpi_behavior_break() in lambda_function.py, containing:
          - schema
          - spend_stats
          - control_stats
          - mix_profile
          - residual_profile
          - thresholds
    """
    work = df.copy()

    # Ensure all relevant columns are numeric
    all_numeric_cols = (
        schema["spend_cols"]
        + schema["control_cols"]
        + schema["event_cols"]
    )
    if schema["kpi_col"]:
        all_numeric_cols.append(schema["kpi_col"])

    for c in all_numeric_cols:
        if c in work.columns:
            work[c] = pd.to_numeric(work[c], errors="coerce").fillna(0.0)

    # ---- Spend statistics ----
    spend_stats = {}
    for c in schema["spend_cols"]:
        if c not in work.columns:
            continue
        x = work[c].to_numpy(dtype=float)
        std_val = float(np.std(x, ddof=1)) if len(x) > 1 else 0.0
        spend_stats[c] = {
            "mean": float(np.mean(x)),
            "std": std_val,
            "p99": _robust_percentile(x, 0.99),
            "p995": _robust_percentile(x, 0.995),
            "active_weeks": int(np.sum(x > 0)),
        }

    # ---- Control statistics ----
    control_stats = {}
    for c in schema["control_cols"]:
        if c not in work.columns:
            continue
        x = work[c].to_numpy(dtype=float)
        std_val = float(np.std(x, ddof=1)) if len(x) > 1 else 0.0
        control_stats[c] = {
            "mean": float(np.mean(x)),
            "std": std_val,
            "p01": _robust_percentile(x, 0.01),
            "p99": _robust_percentile(x, 0.99),
        }

    # ---- Spend mix baseline ----
    avg_share = []
    if schema["spend_cols"]:
        present_spend_cols = [c for c in schema["spend_cols"] if c in work.columns]
        if present_spend_cols:
            spend_mat = work[present_spend_cols].to_numpy(dtype=float)
            total = spend_mat.sum(axis=1)
            mask = total > 0
            if mask.any():
                shares = (spend_mat[mask].T / total[mask]).T
                avg_share = shares.mean(axis=0).tolist()
            else:
                avg_share = [1.0 / len(present_spend_cols)] * len(present_spend_cols)

    # ---- KPI residual profile (ridge regression) ----
    feature_names = []
    beta = []
    resid_mean = 0.0
    resid_std = 0.0

    kpi_col = schema["kpi_col"]
    if kpi_col and kpi_col in work.columns:
        y = _safe_log1p_array(work[kpi_col].to_numpy(dtype=float))

        x_parts = []

        # Log-transformed spend features
        for c in schema["spend_cols"]:
            if c in work.columns:
                x_parts.append(
                    _safe_log1p_array(work[c].to_numpy(dtype=float)).reshape(-1, 1)
                )
                feature_names.append(f"log1p({c})")

        # Z-scored control features
        for c in schema["control_cols"]:
            if c in work.columns:
                col_data = work[c].to_numpy(dtype=float)
                mu = col_data.mean()
                sd = col_data.std(ddof=1) if len(col_data) > 1 else 1.0
                sd = sd if sd > 1e-12 else 1.0
                x_parts.append(((col_data - mu) / sd).reshape(-1, 1))
                feature_names.append(f"z({c})")

        # Binary event features
        for c in schema["event_cols"]:
            if c in work.columns:
                x_parts.append(work[c].to_numpy(dtype=float).reshape(-1, 1))
                feature_names.append(c)

        # Intercept
        x_parts.append(np.ones((len(work), 1)))
        feature_names.append("intercept")

        if x_parts:
            X = np.hstack(x_parts)
            beta_arr, yhat = _ridge_fit(X, y, lam=RIDGE_LAMBDA)
            residuals = y - yhat
            beta = beta_arr.tolist()
            resid_mean = float(residuals.mean())
            resid_std = float(residuals.std(ddof=1)) if len(residuals) > 1 else 0.0

    # ---- Assemble profile ----
    profile = {
        "schema": {
            "date_col": schema["date_col"],
            "kpi_col": schema["kpi_col"],
            "spend_cols": schema["spend_cols"],
            "control_cols": schema["control_cols"],
            "event_cols": schema["event_cols"],
        },
        "spend_stats": spend_stats,
        "control_stats": control_stats,
        "mix_profile": {
            "avg_share": avg_share,
            "spend_cols": schema["spend_cols"],
        },
        "residual_profile": {
            "feature_names": feature_names,
            "beta": beta,
            "resid_mean": resid_mean,
            "resid_std": resid_std,
        },
        "thresholds": {
            "Z_OUTLIER_YELLOW": Z_OUTLIER_YELLOW,
            "Z_OUTLIER_RED": Z_OUTLIER_RED,
            "MIX_SHIFT_YELLOW": MIX_SHIFT_YELLOW,
            "MIX_SHIFT_RED": MIX_SHIFT_RED,
            "JS_YELLOW": JS_YELLOW,
            "JS_RED": JS_RED,
            "RESID_Z_YELLOW": RESID_Z_YELLOW,
            "RESID_Z_RED": RESID_Z_RED,
            "MIN_ACTIVE_WEEKS_FOR_STABILITY": MIN_ACTIVE_WEEKS_FOR_STABILITY,
            "CONSEC_ZERO_WEEKS_RED": CONSEC_ZERO_WEEKS_RED,
        },
    }

    return profile


# ============================================================================
# Data Preparation
# ============================================================================

def prepare_drift_data(
    df: Any,
    date_col: str = "Date",
) -> Tuple[Any, Any, bool]:
    """
    Sort DataFrame by date, split into historical rows and latest row.

    Args:
        df: pandas DataFrame with retailer data (must include a date column)
        date_col: name of the date column

    Returns:
        historical_df: all rows except the last (baseline for profile building)
        latest_df: DataFrame containing just the latest row (to be scored)
        is_eligible: True if historical_df has enough rows for drift detection
    """
    # Ensure date column is datetime
    work = df.copy()
    if date_col in work.columns:
        work[date_col] = pd.to_datetime(work[date_col], errors="coerce")
        work = work.sort_values(date_col).reset_index(drop=True)

    # Split: all rows except last = historical, last row = latest
    if len(work) < 2:
        return work, work.iloc[0:0], False

    historical_df = work.iloc[:-1].copy()
    latest_df = work.iloc[-1:].copy()
    is_eligible = len(historical_df) >= MIN_ROWS_FOR_DRIFT

    return historical_df, latest_df, is_eligible
