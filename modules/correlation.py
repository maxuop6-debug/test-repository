#!/usr/bin/env python3
"""
correlation.py - ماژول همبستگی خبری (Correlation)

این ماژول رابطه بین ویژگی‌های رویدادهای خبری و بازده استراتژی‌ها را در
چهار سطح (در عمل سه سطح فعال: global, conditional, lag) محاسبه می‌کند.

خروجی: correlations.parquet
"""

import argparse
import json
import logging
import os
import pickle
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd
from scipy.stats import spearmanr

# --------------------------------------------------------------------------
# تنظیمات لاگ
# --------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("correlation")

# --------------------------------------------------------------------------
# ثابت‌ها
# --------------------------------------------------------------------------
LAGS_DAYS = [1, 3, 5, 7, 10, 15]

NUMERIC_FEATURES = [
    "distance_days",
    "diff_avg",
    "diff_std",
    "event_count",
    "indicator_diversity",
    "dominant_indicator_importance",
]
CATEGORICAL_FEATURES = ["dominant_indicator", "position"]
ALL_FEATURES = NUMERIC_FEATURES + CATEGORICAL_FEATURES

MIN_SAMPLE_GLOBAL_CONDITIONAL_DEFAULT = 50
MIN_SAMPLE_LAG = 30
LOO_LOWER_BOUND = 10
LOO_UPPER_BOUND = 50  # سطح ۲: 10 <= n < 50 => LOO ; n >= 50 => fold-based
MIN_R_THRESHOLD = 0.3
GOLDEN_SCORE_THRESHOLD = 70


# ==========================================================================
# گام ۱: بارگذاری داده‌ها
# ==========================================================================

def load_signatures(signatures_dir: str) -> pd.DataFrame:
    """تمام فایل‌های .jsonl را از دایرکتوری signatures بخوان و یکپارچه کن."""
    signatures_path = Path(signatures_dir)
    if not signatures_path.exists():
        raise FileNotFoundError(f"مسیر signatures پیدا نشد: {signatures_dir}")

    records: List[Dict[str, Any]] = []
    jsonl_files = sorted(signatures_path.glob("*.jsonl"))
    if not jsonl_files:
        logger.warning("هیچ فایل .jsonl در %s پیدا نشد.", signatures_dir)

    for file_path in jsonl_files:
        with open(file_path, "r", encoding="utf-8") as f:
            for line_no, line in enumerate(f, start=1):
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                    rec["_source_file"] = file_path.name
                    records.append(rec)
                except json.JSONDecodeError as e:
                    logger.warning(
                        "خطای JSON در فایل %s خط %d: %s", file_path.name, line_no, e
                    )

    if not records:
        return pd.DataFrame()

    df = pd.DataFrame(records)
    logger.info("تعداد %d رکورد از %d فایل signatures بارگذاری شد.", len(df), len(jsonl_files))
    return df


def load_golden_scores(golden_scores_path: Optional[str]) -> Optional[pd.DataFrame]:
    """فایل golden_scores.parquet را بارگذاری کن (در صورت وجود)."""
    if not golden_scores_path or not os.path.exists(golden_scores_path):
        return None
    df = pd.read_parquet(golden_scores_path)
    logger.info("golden_scores.parquet با %d رکورد بارگذاری شد.", len(df))
    return df


def load_news(news_pickle_path: Optional[str], fallback_dir: str = "data/news") -> Optional[pd.DataFrame]:
    """news.pickle را بارگذاری کن؛ در صورت عدم وجود از fallback_dir استفاده کن."""
    if news_pickle_path and os.path.exists(news_pickle_path):
        with open(news_pickle_path, "rb") as f:
            news_obj = pickle.load(f)
        df = pd.DataFrame(news_obj) if not isinstance(news_obj, pd.DataFrame) else news_obj
        df["date"] = pd.to_datetime(df["date"])
        logger.info("news.pickle با %d رکورد بارگذاری شد.", len(df))
        return df

    fallback_path = Path(fallback_dir)
    if fallback_path.exists():
        frames = []
        for p in fallback_path.glob("*.pickle"):
            with open(p, "rb") as f:
                obj = pickle.load(f)
            frames.append(pd.DataFrame(obj) if not isinstance(obj, pd.DataFrame) else obj)
        if frames:
            df = pd.concat(frames, ignore_index=True)
            df["date"] = pd.to_datetime(df["date"])
            logger.info("اخبار از %s با %d رکورد بارگذاری شد.", fallback_dir, len(df))
            return df

    logger.warning("هیچ داده خبری (news) پیدا نشد.")
    return None


def load_strategies_metadata(path: Optional[str]) -> Dict[str, Any]:
    if not path or not os.path.exists(path):
        return {}
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def load_version_schema(path: Optional[str]) -> Dict[str, Any]:
    if not path or not os.path.exists(path):
        return {}
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def apply_golden_prefilter(
    df: pd.DataFrame, golden_df: Optional[pd.DataFrame], threshold: float = GOLDEN_SCORE_THRESHOLD
) -> pd.DataFrame:
    """فقط استراتژی‌های با امتیاز Golden >= threshold را نگه دار."""
    if golden_df is None or golden_df.empty:
        return df

    score_col = None
    for candidate in ["score", "golden_score", "final_score"]:
        if candidate in golden_df.columns:
            score_col = candidate
            break
    id_col = None
    for candidate in ["strategy_folder", "strategy_id", "strategy"]:
        if candidate in golden_df.columns:
            id_col = candidate
            break

    if score_col is None or id_col is None:
        logger.warning("ستون‌های لازم در golden_scores پیدا نشد؛ پیش‌فیلتر رد شد.")
        return df

    valid_strategies = set(golden_df.loc[golden_df[score_col] >= threshold, id_col])
    if "strategy_folder" not in df.columns:
        return df

    before = len(df)
    df = df[df["strategy_folder"].isin(valid_strategies)].copy()
    logger.info(
        "پیش‌فیلتر Golden: %d -> %d رکورد (آستانه >= %s)", before, len(df), threshold
    )
    return df


# ==========================================================================
# گام ۲: استخراج ویژگی‌های خبری
# ==========================================================================

def _parse_signature_string(sig: str) -> Dict[str, Any]:
    """
    در صورت отсутствие فیلدهای جداگانه، تلاش برای استخراج اطلاعات از رشته
    signature با فرمت تقریبی:
    "{coin}_{indicator}_{position}_{distance_days}_{model}_{extra}"
    """
    parts = sig.split("_") if isinstance(sig, str) else []
    parsed: Dict[str, Any] = {}
    if len(parts) >= 4:
        # تلاش برای پیدا کردن position (pre/post) و distance_days (عدد) در میان قطعات
        for i, p in enumerate(parts):
            if p in ("pre", "post"):
                parsed["position"] = p
                # عدد بعدی معمولاً distance_days است
                if i + 1 < len(parts) and parts[i + 1].isdigit():
                    parsed["distance_days"] = int(parts[i + 1])
                break
    return parsed


def extract_news_window_stats(
    news_df: pd.DataFrame, period_start: pd.Timestamp, period_end: pd.Timestamp
) -> Dict[str, Any]:
    """diff_avg, diff_std, event_count, indicator_diversity را از news.pickle محاسبه کن."""
    window = news_df[(news_df["date"] >= period_start) & (news_df["date"] <= period_end)]
    if window.empty:
        return {
            "diff_avg": np.nan,
            "diff_std": np.nan,
            "event_count": 0,
            "indicator_diversity": 0,
        }

    diffs = (window["actual"] - window["forecast"]).dropna()
    return {
        "diff_avg": diffs.mean() if not diffs.empty else np.nan,
        "diff_std": diffs.std() if not diffs.empty else np.nan,
        "event_count": len(window),
        "indicator_diversity": window["indicator"].nunique(),
    }


def enrich_with_news_features(df: pd.DataFrame, news_df: Optional[pd.DataFrame]) -> pd.DataFrame:
    """فیلدهای موجود را اولویت بده؛ موارد缺失 را از news.pickle (در صورت وجود) پر کن."""
    df = df.copy()

    # استخراج fallback از رشته signature برای position/distance_days
    if "signature" in df.columns:
        needs_position = "position" not in df.columns or df["position"].isna().any()
        needs_distance = "distance_days" not in df.columns or df["distance_days"].isna().any()
        if needs_position or needs_distance:
            parsed = df["signature"].apply(_parse_signature_string)
            if needs_position:
                df["position"] = df.get("position")
                df["position"] = df["position"].fillna(parsed.apply(lambda d: d.get("position")))
            if needs_distance:
                df["distance_days"] = df.get("distance_days")
                df["distance_days"] = pd.to_numeric(
                    df["distance_days"], errors="coerce"
                ).fillna(parsed.apply(lambda d: d.get("distance_days")))

    # اگر فیلدهای عددی خبری缺失 هستند و news_df موجود است، از پنجره زمانی پر کن
    needed_cols = ["diff_avg", "diff_std", "event_count", "indicator_diversity"]
    missing_any = any(c not in df.columns or df[c].isna().any() for c in needed_cols)

    if missing_any and news_df is not None and "period_start" in df.columns and "period_end" in df.columns:
        logger.info("تکمیل ویژگی‌های缺失 از news.pickle برای رکوردهای ناقص...")
        starts = pd.to_datetime(df["period_start"], errors="coerce")
        ends = pd.to_datetime(df["period_end"], errors="coerce")

        for col in needed_cols:
            if col not in df.columns:
                df[col] = np.nan

        for idx in df.index:
            if any(pd.isna(df.at[idx, c]) for c in needed_cols):
                s, e = starts.at[idx], ends.at[idx]
                if pd.isna(s) or pd.isna(e):
                    continue
                stats = extract_news_window_stats(news_df, s, e)
                for col, val in stats.items():
                    if pd.isna(df.at[idx, col]):
                        df.at[idx, col] = val

    for col in NUMERIC_FEATURES:
        if col not in df.columns:
            df[col] = np.nan
        else:
            df[col] = pd.to_numeric(df[col], errors="coerce")

    for col in CATEGORICAL_FEATURES:
        if col not in df.columns:
            df[col] = np.nan

    if "total_return" not in df.columns:
        raise ValueError("ستون total_return در داده‌های signatures وجود ندارد.")
    df["total_return"] = pd.to_numeric(df["total_return"], errors="coerce")

    return df


# ==========================================================================
# توابع کمکی محاسبه همبستگی
# ==========================================================================

def _encode_categorical(series: pd.Series) -> pd.Series:
    """تبدیل categorical به کدهای عددی (factorize) برای محاسبه Spearman."""
    codes, _ = pd.factorize(series.astype(str))
    codes = codes.astype(float)
    codes[codes == -1] = np.nan
    return pd.Series(codes, index=series.index)


def _spearman_pair(x: pd.Series, y: pd.Series) -> Tuple[float, float, int]:
    """ضریب Spearman، p-value و تعداد نمونه معتبر را برمی‌گرداند."""
    valid = x.notna() & y.notna()
    n = int(valid.sum())
    if n < 3:
        return np.nan, np.nan, n
    try:
        r, p = spearmanr(x[valid], y[valid])
    except Exception:
        return np.nan, np.nan, n
    return float(r), float(p), n


def _loo_spearman(x: pd.Series, y: pd.Series) -> Tuple[float, float, int]:
    """
    Leave-One-Out: برای هر نمونه، آن را حذف کن، روی بقیه Spearman بزن،
    و میانگین ضرایب/مقادیر p را به عنوان خروجی نهایی برگردان.
    """
    valid_idx = x.index[x.notna() & y.notna()]
    n = len(valid_idx)
    if n < 3:
        return np.nan, np.nan, n

    rs, ps = [], []
    for i in valid_idx:
        sub_idx = valid_idx.drop(i)
        if len(sub_idx) < 3:
            continue
        try:
            r, p = spearmanr(x.loc[sub_idx], y.loc[sub_idx])
            if not np.isnan(r):
                rs.append(r)
                ps.append(p)
        except Exception:
            continue

    if not rs:
        return np.nan, np.nan, n
    return float(np.mean(rs)), float(np.mean(ps)), n


def _fold_based_spearman(x: pd.Series, y: pd.Series) -> Tuple[float, float, int]:
    """
    fold-based split: داده را به دو نیم (۵۰٪/۵۰٪) تقسیم کن و Spearman را
    روی نیم تست محاسبه کن.
    """
    valid_idx = x.index[x.notna() & y.notna()]
    n = len(valid_idx)
    if n < 3:
        return np.nan, np.nan, n

    sorted_idx = list(valid_idx)
    half = len(sorted_idx) // 2
    test_idx = sorted_idx[half:]  # نیم دوم به عنوان تست
    if len(test_idx) < 3:
        test_idx = sorted_idx

    return _spearman_pair(x.loc[test_idx], y.loc[test_idx])


def _compute_feature_correlation(
    df: pd.DataFrame, feature: str, target: str = "total_return", method: str = "full"
) -> Tuple[float, float, int]:
    """یک ویژگی را (عددی یا categorical) با target همبسته کن طبق روش مشخص."""
    if feature not in df.columns:
        return np.nan, np.nan, 0

    if feature in CATEGORICAL_FEATURES:
        x = _encode_categorical(df[feature])
    else:
        x = pd.to_numeric(df[feature], errors="coerce")

    y = pd.to_numeric(df[target], errors="coerce")

    if method == "loo":
        return _loo_spearman(x, y)
    elif method == "fold":
        return _fold_based_spearman(x, y)
    else:
        return _spearman_pair(x, y)


def _choose_method(sample_count: int) -> Optional[str]:
    """بر اساس تعداد نمونه، روش محاسبه را انتخاب کن (یا None برای حذف)."""
    if sample_count < LOO_LOWER_BOUND:
        return None
    if sample_count < LOO_UPPER_BOUND:
        return "loo"
    return "fold"


# ==========================================================================
# گام ۳: سطح ۱ - همبستگی کلی (Global)
# ==========================================================================

def compute_global_correlations(
    df: pd.DataFrame, min_sample_count: int
) -> List[Dict[str, Any]]:
    results = []
    if "coin_composition" not in df.columns:
        logger.warning("ستون coin_composition وجود ندارد؛ سطح ۱ رد شد.")
        return results

    for coin, group in df.groupby("coin_composition"):
        sample_count = len(group)
        if sample_count < min_sample_count:
            continue

        for feature in ALL_FEATURES:
            r, p, n = _compute_feature_correlation(group, feature, method="full")
            if np.isnan(r) or n < min_sample_count:
                continue
            if abs(r) < MIN_R_THRESHOLD:
                continue
            results.append(
                {
                    "level": "global",
                    "strategy_id": None,
                    "coin_composition": coin,
                    "signature": None,
                    "feature": feature,
                    "lag": None,
                    "correlation": r,
                    "p_value": p,
                    "sample_count": n,
                }
            )

    logger.info("سطح ۱ (global): %d همبستگی معنی‌دار پیدا شد.", len(results))
    return results


# ==========================================================================
# گام ۴: سطح ۲ - همبستگی شرطی در هر امضا (Conditional)
# ==========================================================================

def compute_conditional_correlations(
    df: pd.DataFrame, min_sample_count: int
) -> List[Dict[str, Any]]:
    results = []
    required_cols = {"coin_composition", "signature"}
    if not required_cols.issubset(df.columns):
        logger.warning("ستون‌های لازم برای سطح ۲ وجود ندارد.")
        return results

    for (coin, sig), group in df.groupby(["coin_composition", "signature"]):
        sample_count = len(group)
        if sample_count < LOO_LOWER_BOUND:
            continue  # حذف امضاهای با داده ناکافی

        method = _choose_method(sample_count)
        if method is None:
            continue

        # اگر fold اما کمتر از آستانه نمونه global باشد، باز هم fold را قبول کن
        # چون آستانه fold از کانفیگ min_sample_count کلی پیروی نمی‌کند؛ طبق اسپک
        # سطح ۲ از ۱۰ تا ۵۰ LOO و >= ۵۰ fold (یا برابر min_sample_count کانفیگ‌شده).
        effective_full_threshold = max(min_sample_count, LOO_UPPER_BOUND)
        if method == "fold" and sample_count < effective_full_threshold:
            method = "loo" if sample_count >= LOO_LOWER_BOUND else None
        if method is None:
            continue

        for feature in ALL_FEATURES:
            r, p, n = _compute_feature_correlation(group, feature, method=method)
            if np.isnan(r) or n < LOO_LOWER_BOUND:
                continue
            if abs(r) < MIN_R_THRESHOLD:
                continue
            results.append(
                {
                    "level": "conditional",
                    "strategy_id": None,
                    "coin_composition": coin,
                    "signature": sig,
                    "feature": feature,
                    "lag": None,
                    "correlation": r,
                    "p_value": p,
                    "sample_count": n,
                }
            )

    logger.info("سطح ۲ (conditional): %d همبستگی معنی‌دار پیدا شد.", len(results))
    return results


# ==========================================================================
# گام ۵: سطح ۴ - تحلیل تاخیر (Lag Analysis)
# ==========================================================================

def compute_lag_correlations(df: pd.DataFrame) -> List[Dict[str, Any]]:
    results = []
    required = {"strategy_folder", "coin_composition", "period_start", "total_return"}
    if not required.issubset(df.columns):
        logger.warning("ستون‌های لازم برای سطح ۴ (lag) وجود ندارد.")
        return results

    work_df = df.copy()
    work_df["period_start"] = pd.to_datetime(work_df["period_start"], errors="coerce")
    work_df = work_df.dropna(subset=["period_start"])

    for (strategy, coin), group in work_df.groupby(["strategy_folder", "coin_composition"]):
        group = group.sort_values("period_start").reset_index(drop=True)
        if len(group) < MIN_SAMPLE_LAG:
            continue

        date_to_row = {row["period_start"]: i for i, row in group.iterrows()}

        for lag in LAGS_DAYS:
            for feature in ALL_FEATURES:
                if feature not in group.columns:
                    continue

                lagged_feature_vals = []
                current_return_vals = []

                for i, row in group.iterrows():
                    target_date = row["period_start"] - timedelta(days=lag)
                    # نزدیک‌ترین تاریخ موجود را با تطابق دقیق یا نزدیک‌ترین قبلی پیدا کن
                    candidates = group[group["period_start"] <= target_date]
                    if candidates.empty:
                        continue
                    lag_row = candidates.iloc[-1]

                    feat_val = lag_row[feature]
                    if feature in CATEGORICAL_FEATURES:
                        # برای categorical، مقدار را بعداً encode می‌کنیم
                        pass
                    lagged_feature_vals.append(feat_val)
                    current_return_vals.append(row["total_return"])

                if len(lagged_feature_vals) < MIN_SAMPLE_LAG:
                    continue

                x = pd.Series(lagged_feature_vals)
                y = pd.Series(current_return_vals)

                if feature in CATEGORICAL_FEATURES:
                    x = _encode_categorical(x)
                else:
                    x = pd.to_numeric(x, errors="coerce")
                y = pd.to_numeric(y, errors="coerce")

                r, p, n = _spearman_pair(x, y)
                if np.isnan(r) or n < MIN_SAMPLE_LAG:
                    continue
                if abs(r) < MIN_R_THRESHOLD:
                    continue

                results.append(
                    {
                        "level": "lag",
                        "strategy_id": strategy,
                        "coin_composition": coin,
                        "signature": None,
                        "feature": feature,
                        "lag": lag,
                        "correlation": r,
                        "p_value": p,
                        "sample_count": n,
                    }
                )

    logger.info("سطح ۴ (lag): %d همبستگی معنی‌دار پیدا شد.", len(results))
    return results


# ==========================================================================
# گام ۶: ذخیره‌سازی
# ==========================================================================

OUTPUT_COLUMNS = [
    "level",
    "strategy_id",
    "coin_composition",
    "signature",
    "feature",
    "lag",
    "correlation",
    "p_value",
    "sample_count",
    "created_at",
]


def build_output_dataframe(
    all_results: List[Dict[str, Any]], version_schema: Dict[str, Any]
) -> pd.DataFrame:
    if not all_results:
        df = pd.DataFrame(columns=OUTPUT_COLUMNS)
        return df

    df = pd.DataFrame(all_results)
    df["created_at"] = datetime.now(timezone.utc)

    if version_schema:
        version_id = version_schema.get("version_id") or version_schema.get("version")
        if version_id is not None:
            df["version_id"] = version_id

    for col in OUTPUT_COLUMNS:
        if col not in df.columns:
            df[col] = None

    ordered_cols = OUTPUT_COLUMNS + [c for c in df.columns if c not in OUTPUT_COLUMNS]
    return df[ordered_cols]


def save_output(df: pd.DataFrame, output_dir: str) -> str:
    os.makedirs(output_dir, exist_ok=True)
    output_path = os.path.join(output_dir, "correlations.parquet")
    df.to_parquet(output_path, index=False)
    logger.info("خروجی در %s ذخیره شد (%d رکورد).", output_path, len(df))
    return output_path


# ==========================================================================
# گزارش‌گیری
# ==========================================================================

def print_summary_report(df: pd.DataFrame) -> None:
    if df.empty:
        logger.info("هیچ همبستگی معنی‌داری در هیچ سطحی پیدا نشد.")
        return

    print("\n" + "=" * 60)
    print("گزارش خلاصه ماژول Correlation")
    print("=" * 60)
    for level in ["global", "conditional", "lag"]:
        sub = df[df["level"] == level]
        print(f"سطح '{level}': {len(sub)} همبستگی معنی‌دار (|r| >= {MIN_R_THRESHOLD})")
        if not sub.empty:
            top = sub.reindex(sub["correlation"].abs().sort_values(ascending=False).index).head(3)
            for _, row in top.iterrows():
                print(
                    f"   - feature={row['feature']}, coin={row['coin_composition']}, "
                    f"r={row['correlation']:.3f}, n={row['sample_count']}"
                )
    print("=" * 60 + "\n")


# ==========================================================================
# تابع اصلی
# ==========================================================================

def run_correlation_pipeline(
    signatures_dir: str,
    golden_scores_path: Optional[str],
    news_pickle_path: Optional[str],
    strategies_json_path: Optional[str],
    version_schema_path: Optional[str],
    output_dir: str,
    min_sample_count: int = MIN_SAMPLE_GLOBAL_CONDITIONAL_DEFAULT,
) -> str:

    # گام ۱: بارگذاری
    df = load_signatures(signatures_dir)
    if df.empty:
        raise ValueError("هیچ داده‌ای از signatures بارگذاری نشد؛ پایپ‌لاین متوقف شد.")

    golden_df = load_golden_scores(golden_scores_path)
    news_df = load_news(news_pickle_path)
    _strategies_meta = load_strategies_metadata(strategies_json_path)
    version_schema = load_version_schema(version_schema_path)

    df = apply_golden_prefilter(df, golden_df)
    if df.empty:
        raise ValueError("پس از پیش‌فیلتر Golden، هیچ رکوردی باقی نماند.")

    # گام ۲: استخراج/تکمیل ویژگی‌های خبری
    df = enrich_with_news_features(df, news_df)

    # گام ۳ تا ۵: محاسبه همبستگی‌ها
    all_results: List[Dict[str, Any]] = []
    all_results.extend(compute_global_correlations(df, min_sample_count))
    all_results.extend(compute_conditional_correlations(df, min_sample_count))
    all_results.extend(compute_lag_correlations(df))

    # گام ۶: ذخیره‌سازی
    output_df = build_output_dataframe(all_results, version_schema)
    output_path = save_output(output_df, output_dir)

    print_summary_report(output_df)
    return output_path


# ==========================================================================
# CLI
# ==========================================================================

def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="ماژول همبستگی خبری (Correlation) - محاسبه ضرایب همبستگی "
        "بین ویژگی‌های خبری و بازده استراتژی‌ها."
    )
    parser.add_argument("--signatures-dir", required=True, help="مسیر پوشه فایل‌های .jsonl signatures")
    parser.add_argument("--golden-scores", default=None, help="مسیر golden_scores.parquet (اختیاری)")
    parser.add_argument("--news-pickle", default=None, help="مسیر news.pickle")
    parser.add_argument("--strategies-json", default=None, help="مسیر strategies_metadata.json")
    parser.add_argument("--version-schema", default=None, help="مسیر version_schema.json (اختیاری)")
    parser.add_argument("--output-dir", required=True, help="مسیر پوشه خروجی برای ذخیره Parquet")
    parser.add_argument(
        "--min-sample-count",
        type=int,
        default=MIN_SAMPLE_GLOBAL_CONDITIONAL_DEFAULT,
        help="آستانه نمونه برای سطح ۱ و ۲ (پیش‌فرض ۵۰)",
    )
    return parser.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)
    try:
        run_correlation_pipeline(
            signatures_dir=args.signatures_dir,
            golden_scores_path=args.golden_scores,
            news_pickle_path=args.news_pickle,
            strategies_json_path=args.strategies_json,
            version_schema_path=args.version_schema,
            output_dir=args.output_dir,
            min_sample_count=args.min_sample_count,
        )
        return 0
    except Exception as e:
        logger.error("اجرای پایپ‌لاین با خطا متوقف شد: %s", e, exc_info=True)
        return 1


if __name__ == "__main__":
    sys.exit(main())
