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
LOO_UPPER_BOUND = 50
MIN_R_THRESHOLD = 0.3
GOLDEN_SCORE_THRESHOLD = 50

DEFAULT_CHUNK_SIZE = 20
DEFAULT_INTERRUPT_FLAG_NAME = "interrupt.flag"
PARTIAL_RESULTS_FILENAME = "correlation_partial_results.json"


# ==========================================================================
# گام ۱: بارگذاری داده‌ها
# ==========================================================================

def load_signatures(signatures_dir: str, signatures_filter: Optional[str] = None) -> pd.DataFrame:
    """تمام فایل‌های .jsonl را از دایرکتوری signatures بخوان و یکپارچه کن.

    در صورتی که signatures_filter داده شده باشد، فقط رکوردهایی که فیلد
    signature آنها در لیست موجود در فایل JSON فیلتر قرار دارد نگه داشته می‌شوند.
    """
    signatures_path = Path(signatures_dir)
    if not signatures_path.exists():
        raise FileNotFoundError(f"مسیر signatures پیدا نشد: {signatures_dir}")

    records: List[Dict[str, Any]] = []
    jsonl_files = sorted(signatures_path.rglob("*.jsonl"))
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

    # اگر ستون signature وجود نداشت، آن را از فیلدهای موجود بساز
    if "signature" not in df.columns:
        logger.info("ستون signature یافت نشد؛ با build_signature ساخته می‌شود.")
        df["signature"] = df.apply(build_signature, axis=1)

    if signatures_filter and os.path.exists(signatures_filter):
        with open(signatures_filter, "r", encoding="utf-8") as f:
            _filter_data = json.load(f)
        # filter.json ممکن است آرایه‌ای از dict (با کلید path) یا آرایه‌ای از string باشد
        if _filter_data and isinstance(_filter_data[0], dict):
            allowed_signatures = set(item["path"] for item in _filter_data)
        else:
            allowed_signatures = set(_filter_data)
        if "signature" in df.columns:
            before = len(df)
            df = df[df["signature"].isin(allowed_signatures)].copy()
            logger.info(
                "اعمال signatures-filter: %d -> %d رکورد (%d امضای مجاز).",
                before, len(df), len(allowed_signatures),
            )
        else:
            logger.warning("ستون signature در داده‌ها وجود ندارد؛ signatures-filter نادیده گرفته شد.")
    elif signatures_filter:
        logger.warning("فایل signatures-filter پیدا نشد: %s؛ همه‌ی داده‌ها پردازش می‌شوند.", signatures_filter)

    return df


def load_golden_scores(golden_scores_path: Optional[str]) -> Optional[pd.DataFrame]:
    """فایل golden_scores.parquet را بارگذاری کن (در صورت وجود)."""
    if not golden_scores_path or not os.path.exists(golden_scores_path):
        return None
    df = pd.read_parquet(golden_scores_path)
    logger.info("golden_scores.parquet با %d رکورد بارگذاری شد.", len(df))
    return df


def find_column_index(header: List[str], keywords: List[str]) -> int:
    """اولین ستونی که نام آن شامل یکی از کلیدواژه‌ها باشد را پیدا کن.

    جستجو case-insensitive و partial match است.
    در صورت عدم وجود، -1 برمی‌گرداند.
    """
    header_lower = [h.lower() for h in header]
    for kw in keywords:
        kw_lower = kw.lower()
        for i, h in enumerate(header_lower):
            if kw_lower in h:
                return i
    return -1


def parse_percent(value_str) -> Optional[float]:
    """رشته عددی با کاراکترهای اضافی را به float تبدیل کن.

    کاراکترهای %, $, B (میلیارد), کاما، پرانتز و فاصله حذف می‌شوند.
    در صورت مقدار خالی یا نامعتبر، None برمی‌گرداند.
    """
    if value_str is None:
        return None
    if isinstance(value_str, (int, float)):
        return float(value_str) if not (isinstance(value_str, float) and np.isnan(value_str)) else None
    s = str(value_str).strip()
    if not s or s in ("-", "—", "--", "N/A", "n/a", ""):
        return None
    # حذف کاراکترهای غیرعددی به‌جز نقطه و علامت منفی
    import re
    s = re.sub(r"[%$B,()]", "", s).strip()
    if not s or s in ("-", ""):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def detect_indicator_from_filename(filename: str) -> str:
    """نام اندیکاتور را از نام فایل CSV استخراج کن."""
    stem = Path(filename).stem.lower()
    # نگاشت نام فایل به نام اندیکاتور استاندارد
    mapping = {
        "fomc": "FOMC",
        "cpi": "CPI",
        "nfp": "NFP",
        "gdp": "GDP",
        "ppi": "PPI",
        "pce": "PCE",
        "ism": "ISM",
        "retail": "Retail Sales",
        "unemployment": "Unemployment",
        "housing": "Housing",
        "jobs": "Jobs",
        "trade": "Trade Balance",
        "durable": "Durable Goods",
        "consumer": "Consumer Confidence",
    }
    for key, val in mapping.items():
        if key in stem:
            return val
    # اگر نگاشتی پیدا نشد، از نام فایل استفاده کن
    return Path(filename).stem.replace("_", " ").title()


def load_news(
    news_pickle_path: Optional[str] = None,
    news_dir: Optional[str] = None,
) -> Optional[pd.DataFrame]:
    """داده‌های خبری را بارگذاری کن.

    اولویت:
    ۱. news_pickle_path — فایل pickle (در صورت وجود، اولویت اول)
    ۲. news_dir  — فایل‌های CSV پوشه اخبار با پشتیبانی از ساختار واقعی
    """
    # --- اولویت اول: pickle ---
    if news_pickle_path and os.path.exists(news_pickle_path):
        with open(news_pickle_path, "rb") as f:
            news_obj = pickle.load(f)
        df = pd.DataFrame(news_obj) if not isinstance(news_obj, pd.DataFrame) else news_obj
        df["date"] = pd.to_datetime(df["date"])
        logger.info("news.pickle با %d رکورد بارگذاری شد.", len(df))
        return df

    # --- اولویت دوم: CSV از news_dir ---
    if news_dir:
        news_dir_path = Path(news_dir)
        if not news_dir_path.exists():
            logger.warning("مسیر news_dir وجود ندارد: %s", news_dir)
        else:
            csv_files = sorted(news_dir_path.glob("*.csv"))
            if not csv_files:
                logger.warning("هیچ فایل CSV در %s پیدا نشد.", news_dir)
            else:
                frames = []
                for p in csv_files:
                    try:
                        # خواندن با utf-8-sig برای حذف BOM
                        raw_df = pd.read_csv(p, encoding="utf-8-sig", dtype=str)
                        header = list(raw_df.columns)

                        # یافتن ستون تاریخ
                        date_idx = find_column_index(header, ["date", "expected", "impact"])
                        if date_idx == -1:
                            date_idx = 0  # ستون اول به‌عنوان پیش‌فرض
                        date_col = header[date_idx]

                        # یافتن ستون‌های اصلی
                        actual_idx = find_column_index(header, ["actual"])
                        forecast_idx = find_column_index(header, ["forecast", "consensus"])
                        previous_idx = find_column_index(header, ["previous", "prior"])
                        reference_idx = find_column_index(header, ["reference", "event", "name"])

                        if actual_idx == -1:
                            logger.warning(
                                "فایل CSV %s فاقد ستون Actual است؛ نادیده گرفته شد.", p.name
                            )
                            continue
                        if forecast_idx == -1:
                            logger.warning(
                                "فایل CSV %s فاقد ستون Forecast/Consensus است؛ نادیده گرفته شد.", p.name
                            )
                            continue

                        actual_col = header[actual_idx]
                        forecast_col = header[forecast_idx]
                        previous_col = header[previous_idx] if previous_idx != -1 else None
                        reference_col = header[reference_idx] if reference_idx != -1 else None

                        # تشخیص اندیکاتور از نام فایل
                        indicator_name = detect_indicator_from_filename(p.name)
                        is_fomc = "fomc" in p.name.lower()

                        # فیلتر FOMC: فقط رکوردهای مرتبط با نرخ بهره
                        chunk_df = raw_df.copy()
                        if is_fomc and reference_col is not None:
                            ref_lower = chunk_df[reference_col].fillna("").str.lower()
                            mask = ref_lower.str.contains("interest rate|fomc", na=False)
                            chunk_df = chunk_df[mask].copy()
                            if chunk_df.empty:
                                logger.warning(
                                    "فایل FOMC %s پس از فیلتر Interest Rate/FOMC خالی شد؛ نادیده گرفته شد.",
                                    p.name,
                                )
                                continue

                        # parse تاریخ با فرمت‌های مختلف
                        def parse_date(val):
                            if not val or str(val).strip() in ("", "nan", "NaN"):
                                return pd.NaT
                            for fmt in ("%b %d, %Y", "%Y-%m-%d", "%m/%d/%Y", "%d/%m/%Y"):
                                try:
                                    return datetime.strptime(str(val).strip(), fmt)
                                except ValueError:
                                    continue
                            try:
                                return pd.to_datetime(str(val).strip(), errors="coerce")
                            except Exception:
                                return pd.NaT

                        chunk_df["date"] = chunk_df[date_col].apply(parse_date)
                        chunk_df["indicator"] = indicator_name
                        chunk_df["actual"] = chunk_df[actual_col].apply(parse_percent)
                        chunk_df["forecast"] = chunk_df[forecast_col].apply(parse_percent)
                        if previous_col:
                            chunk_df["previous"] = chunk_df[previous_col].apply(parse_percent)
                        else:
                            chunk_df["previous"] = np.nan

                        # فقط ستون‌های مورد نیاز را نگه دار
                        keep_cols = ["date", "indicator", "actual", "forecast", "previous"]
                        result_df = chunk_df[keep_cols].dropna(subset=["date"])
                        if result_df.empty:
                            logger.warning("فایل CSV %s پس از parse تاریخ خالی شد.", p.name)
                            continue

                        frames.append(result_df)
                        logger.info(
                            "فایل %s: %d رکورد بارگذاری شد (indicator=%s).",
                            p.name, len(result_df), indicator_name,
                        )
                    except Exception as e:
                        logger.warning("خطا در خواندن CSV %s: %s", p.name, e)

                if frames:
                    df = pd.concat(frames, ignore_index=True)
                    df["date"] = pd.to_datetime(df["date"], errors="coerce")
                    df = df.dropna(subset=["date"])
                    logger.info(
                        "اخبار از %d فایل CSV در %s با مجموع %d رکورد بارگذاری شد.",
                        len(frames), news_dir, len(df),
                    )
                    return df
                logger.warning("هیچ فایل CSV معتبری در %s پیدا نشد.", news_dir)

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
        logger.warning("ستون strategy_folder در داده‌های signatures وجود ندارد.")
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
        for i, p in enumerate(parts):
            if p in ("pre", "post"):
                parsed["position"] = p
                if i + 1 < len(parts) and parts[i + 1].isdigit():
                    parsed["distance_days"] = int(parts[i + 1])
                break
    return parsed


def build_signature(row: pd.Series) -> Optional[str]:
    """از فیلدهای کلیدی یک رشته‌ی امضای منحصربه‌فرد بساز.

    فرمت: {coin_composition}__{dominant_indicator}__{position}__{distance_days}d__{model}__{market_regime}
    هر فیلد موجود نباشد با «unknown» جایگزین می‌شود.
    """
    coin = str(row.get("coin_composition") or "unknown")
    indicator = str(row.get("dominant_indicator") or "unknown")
    position = str(row.get("position") or "unknown")
    distance = str(row.get("distance_days") or "unknown")
    model = str(row.get("model") or "unknown")
    regime = str(row.get("market_regime") or "unknown")
    return f"{coin}__{indicator}__{position}__{distance}d__{model}__{regime}"


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
    test_idx = sorted_idx[half:]
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

    # جلوگیری از ConstantInputWarning: اگر واریانس صفر باشد، نتیجه nan است
    valid = x.notna() & y.notna()
    if valid.sum() < 3:
        return np.nan, np.nan, int(valid.sum())
    if x[valid].nunique() <= 1 or y[valid].nunique() <= 1:
        return np.nan, np.nan, int(valid.sum())

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
# مدیریت وضعیت (Status) برای وقفه/ادامه (Resume)
# ==========================================================================

def default_status_path(output_dir: str) -> str:
    return os.path.join(output_dir, "correlation_status.json")


def load_status(status_file: str) -> Optional[Dict[str, Any]]:
    """فایل وضعیت قبلی را بارگذاری کن (اگر وجود داشته و قابل ادامه باشد)."""
    if not status_file or not os.path.exists(status_file):
        return None
    try:
        with open(status_file, "r", encoding="utf-8") as f:
            status = json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        logger.warning("فایل وضعیت قابل خواندن نیست (%s)؛ از ابتدا شروع می‌شود.", e)
        return None

    if status.get("status") in ("interrupted", "running"):
        logger.info(
            "وضعیت قبلی پیدا شد: status=%s, last_chunk_index=%s, "
            "processed_signatures=%d",
            status.get("status"),
            status.get("last_chunk_index"),
            len(status.get("processed_signatures", [])),
        )
        return status

    logger.info("وضعیت قبلی status=%s است؛ ادامه لازم نیست (از ابتدا شروع می‌شود).", status.get("status"))
    return None


def save_status(
    status_file: str,
    processed_signatures: List[List[str]],
    last_chunk_index: int,
    total_chunks: int,
    status: str,
    chunk_size: int = DEFAULT_CHUNK_SIZE,
) -> None:
    """وضعیت فعلی پایپ‌لاین را در فایل JSON ذخیره کن."""
    os.makedirs(os.path.dirname(status_file) or ".", exist_ok=True)
    payload = {
        "processed_signatures": processed_signatures,
        "last_chunk_index": last_chunk_index,
        "total_chunks": total_chunks,
        "chunk_size": chunk_size,
        "status": status,
        "last_updated": datetime.now(timezone.utc).isoformat(),
    }
    tmp_path = status_file + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    os.replace(tmp_path, status_file)
    logger.info(
        "وضعیت ذخیره شد: status=%s, chunk=%d/%d, processed=%d",
        status, last_chunk_index, total_chunks, len(processed_signatures),
    )


def check_interrupt_flag(output_dir: str, interrupt_flag_path: Optional[str] = None) -> bool:
    """بررسی وجود فایل interrupt.flag (در مسیر مشخص یا در output_dir)."""
    candidates = []
    if interrupt_flag_path:
        candidates.append(interrupt_flag_path)
    candidates.append(os.path.join(output_dir, DEFAULT_INTERRUPT_FLAG_NAME))

    env_path = os.environ.get("THIRD_REPO_INTERRUPT_FLAG")
    if env_path:
        candidates.append(env_path)

    for path in candidates:
        if path and os.path.exists(path):
            logger.warning("فایل interrupt.flag پیدا شد در %s؛ پردازش متوقف می‌شود.", path)
            return True
    return False


def load_partial_results(output_dir: str) -> List[Dict[str, Any]]:
    """نتایج موقتِ ذخیره‌شده از اجرای قبلی (در صورت resume) را بارگذاری کن."""
    path = os.path.join(output_dir, PARTIAL_RESULTS_FILENAME)
    if not os.path.exists(path):
        return []
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        logger.warning("نتایج موقت قابل خواندن نیست (%s)؛ نادیده گرفته می‌شود.", e)
        return []


def save_partial_results(output_dir: str, results: List[Dict[str, Any]]) -> None:
    """نتایج موقت تجمیع‌شده را ذخیره کن تا در صورت وقفه از دست نروند."""
    os.makedirs(output_dir, exist_ok=True)
    path = os.path.join(output_dir, PARTIAL_RESULTS_FILENAME)

    def _default(o):
        if isinstance(o, (np.integer,)):
            return int(o)
        if isinstance(o, (np.floating,)):
            return float(o)
        if isinstance(o, (datetime,)):
            return o.isoformat()
        return str(o)

    tmp_path = path + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, default=_default)
    os.replace(tmp_path, path)


def get_unique_signatures(df: pd.DataFrame) -> List[Tuple[str, str]]:
    """لیست منحصربه‌فرد (coin_composition, signature) را استخراج کن."""
    required = {"coin_composition", "signature"}
    if not required.issubset(df.columns):
        return []
    pairs = (
        df[["coin_composition", "signature"]]
        .dropna()
        .drop_duplicates()
        .apply(tuple, axis=1)
        .tolist()
    )
    return sorted(set(pairs))


def chunk_list(items: List[Any], chunk_size: int) -> List[List[Any]]:
    """یک لیست را به قطعات با اندازه ثابت تقسیم کن."""
    if chunk_size <= 0:
        chunk_size = DEFAULT_CHUNK_SIZE
    return [items[i:i + chunk_size] for i in range(0, len(items), chunk_size)]


def compute_conditional_correlations_for_signatures(
    df: pd.DataFrame,
    signatures: List[Tuple[str, str]],
    min_sample_count: int,
) -> List[Dict[str, Any]]:
    """نسخه‌ی محدودشده‌ی سطح ۲ که فقط روی یک زیرمجموعه از امضاها اجرا می‌شود (برای chunking)."""
    results: List[Dict[str, Any]] = []
    required_cols = {"coin_composition", "signature"}
    if not required_cols.issubset(df.columns):
        return results

    sig_set = set(signatures)
    subset = df[
        df.apply(lambda r: (r["coin_composition"], r["signature"]) in sig_set, axis=1)
    ]
    if subset.empty:
        return results

    for (coin, sig), group in subset.groupby(["coin_composition", "signature"]):
        sample_count = len(group)
        if sample_count < LOO_LOWER_BOUND:
            continue

        method = _choose_method(sample_count)
        if method is None:
            continue

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

    return results


def save_summary(
    output_dir: str,
    status: str,
    total_signatures: int,
    processed_signatures: int,
    output_df: pd.DataFrame,
    output_files: List[str],
    version_schema: Dict[str, Any],
) -> str:
    """فایل خلاصه correlation_summary.json را تولید و ذخیره کن."""
    summary = {
        "status": status,
        "total_signatures": total_signatures,
        "processed_signatures": processed_signatures,
        "global_correlations": int((output_df["level"] == "global").sum()) if not output_df.empty else 0,
        "conditional_correlations": int((output_df["level"] == "conditional").sum()) if not output_df.empty else 0,
        "lag_correlations": int((output_df["level"] == "lag").sum()) if not output_df.empty else 0,
        "output_files": output_files,
        "version_id": version_schema.get("version_id") or version_schema.get("version"),
        "completed_at": datetime.now(timezone.utc).isoformat(),
    }
    os.makedirs(output_dir, exist_ok=True)
    summary_path = os.path.join(output_dir, "correlation_summary.json")
    with open(summary_path, "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)
    logger.info("فایل خلاصه در %s ذخیره شد.", summary_path)
    return summary_path


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
        # فیلتر حداقل تعداد دوره حذف شد: همه کوین‌ها (حتی با نمونه کم) محاسبه می‌شوند.

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
            continue

        method = _choose_method(sample_count)
        if method is None:
            continue

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

        for lag in LAGS_DAYS:
            for feature in ALL_FEATURES:
                if feature not in group.columns:
                    continue

                lagged_feature_vals = []
                current_return_vals = []

                for i, row in group.iterrows():
                    target_date = row["period_start"] - timedelta(days=lag)
                    candidates = group[group["period_start"] <= target_date]
                    if candidates.empty:
                        continue
                    lag_row = candidates.iloc[-1]

                    feat_val = lag_row[feature]
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
    try:
        df.to_parquet(output_path, index=False)
        logger.info("خروجی در %s ذخیره شد (%d رکورد).", output_path, len(df))
        return output_path
    except (ImportError, ValueError) as e:
        logger.warning(
            "ذخیره Parquet ناموفق بود (%s)؛ به‌جای آن CSV ذخیره می‌شود. "
            "برای رفع این مشکل pyarrow یا fastparquet را نصب کنید.", e
        )
        csv_path = os.path.join(output_dir, "correlations.csv")
        df.to_csv(csv_path, index=False)
        logger.info("خروجی (هشدار: CSV) در %s ذخیره شد (%d رکورد).", csv_path, len(df))
        return csv_path


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
# تابع اصلی (بازنویسی شده با مدیریت بهتر resume و chunking)
# ==========================================================================

def run_correlation_pipeline(
    signatures_dir: str,
    golden_scores_path: Optional[str],
    news_pickle_path: Optional[str],
    news_dir: Optional[str],
    strategies_json_path: Optional[str],
    version_schema_path: Optional[str],
    output_dir: str,
    min_sample_count: int = MIN_SAMPLE_GLOBAL_CONDITIONAL_DEFAULT,
    status_file: Optional[str] = None,
    resume: bool = False,
    chunk_size: int = DEFAULT_CHUNK_SIZE,
    interrupt_flag_path: Optional[str] = None,
    signatures_filter: Optional[str] = None,
) -> str:

    status_file = status_file or default_status_path(output_dir)
    os.makedirs(output_dir, exist_ok=True)

    # ---- گام ۱: بارگذاری داده‌ها ----
    df = load_signatures(signatures_dir, signatures_filter)
    if df.empty:
        raise ValueError("هیچ داده‌ای از signatures بارگذاری نشد؛ پایپ‌لاین متوقف شد.")

    golden_df = load_golden_scores(golden_scores_path)
    news_df = load_news(news_pickle_path=news_pickle_path, news_dir=news_dir)
    _strategies_meta = load_strategies_metadata(strategies_json_path)
    version_schema = load_version_schema(version_schema_path)

    df = apply_golden_prefilter(df, golden_df)
    if df.empty:
        raise ValueError("پس از پیش‌فیلتر Golden، هیچ رکوردی باقی نماند.")

    # ---- گام ۲: استخراج/تکمیل ویژگی‌های خبری ----
    df = enrich_with_news_features(df, news_df)

    # ---- گام ۰: بارگذاری وضعیت قبلی (در صورت --resume) ----
    prev_status = load_status(status_file) if resume else None
    processed_signatures: List[List[str]] = (
        prev_status.get("processed_signatures", []) if prev_status else []
    )
    processed_set = {tuple(p) for p in processed_signatures}
    start_chunk_index = (prev_status.get("last_chunk_index", -1) + 1) if prev_status else 0

    all_results: List[Dict[str, Any]] = load_partial_results(output_dir) if prev_status else []

    # ---- گام ۳: لیست امضاهای منحصربه‌فرد و تقسیم به چانک ----
    all_signatures = get_unique_signatures(df)
    total_signatures = len(all_signatures)
    remaining_signatures = [s for s in all_signatures if s not in processed_set]

    chunks = chunk_list(remaining_signatures, chunk_size)
    total_chunks = start_chunk_index + len(chunks)

    logger.info(
        "تعداد کل امضاها: %d | از قبل پردازش‌شده: %d | باقی‌مانده: %d | تعداد قطعات جدید: %d",
        total_signatures, len(processed_set), len(remaining_signatures), len(chunks),
    )

    # ---- سطح ۱ (Global): فقط یک بار در کل اجرا ----
    already_has_global = any(r.get("level") == "global" for r in all_results)
    if not already_has_global:
        if check_interrupt_flag(output_dir, interrupt_flag_path):
            save_status(status_file, processed_signatures, start_chunk_index - 1, total_chunks, "interrupted", chunk_size)
            save_partial_results(output_dir, all_results)
            logger.warning("interrupt.flag قبل از شروع پیدا شد؛ خروج graceful.")
            return ""
        global_results = compute_global_correlations(df, min_sample_count)
        all_results.extend(global_results)
        save_partial_results(output_dir, all_results)

    # ---- سطح ۴ (Lag): فقط یک بار در کل اجرا ----
    already_has_lag = any(r.get("level") == "lag" for r in all_results)
    if not already_has_lag:
        if check_interrupt_flag(output_dir, interrupt_flag_path):
            save_status(status_file, processed_signatures, start_chunk_index - 1, total_chunks, "interrupted", chunk_size)
            save_partial_results(output_dir, all_results)
            logger.warning("interrupt.flag قبل از تحلیل lag پیدا شد؛ خروج graceful.")
            return ""
        lag_results = compute_lag_correlations(df)
        all_results.extend(lag_results)
        save_partial_results(output_dir, all_results)

    # ---- سطح ۲ (Conditional): پردازش چانک به چانک ----
    interrupted = False
    current_chunk_index = start_chunk_index - 1
    for offset, chunk in enumerate(chunks):
        current_chunk_index = start_chunk_index + offset

        if check_interrupt_flag(output_dir, interrupt_flag_path):
            interrupted = True
            current_chunk_index -= 1  # این چانک پردازش نشد
            break

        chunk_results = compute_conditional_correlations_for_signatures(df, chunk, min_sample_count)
        all_results.extend(chunk_results)

        processed_signatures.extend([list(s) for s in chunk])
        save_partial_results(output_dir, all_results)
        save_status(
            status_file, processed_signatures, current_chunk_index, total_chunks, "running", chunk_size
        )
        logger.info(
            "قطعه %d/%d پردازش شد (%d امضا، %d همبستگی جدید).",
            current_chunk_index + 1, total_chunks, len(chunk), len(chunk_results),
        )

    if interrupted:
        save_status(status_file, processed_signatures, current_chunk_index, total_chunks, "interrupted", chunk_size)
        logger.warning("اجرا به دلیل وجود interrupt.flag متوقف شد (graceful). برای ادامه از --resume استفاده کنید.")
        return ""

    # ---- پس از اتمام همه چانک‌ها ----
    output_df = build_output_dataframe(all_results, version_schema)
    output_path = save_output(output_df, output_dir)

    save_status(status_file, processed_signatures, current_chunk_index, total_chunks, "completed", chunk_size)
    save_summary(
        output_dir=output_dir,
        status="completed",
        total_signatures=total_signatures,
        processed_signatures=len(processed_signatures),
        output_df=output_df,
        output_files=[os.path.basename(output_path)],
        version_schema=version_schema,
    )

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
    parser.add_argument(
        "--news-dir",
        default=None,
        help="مسیر پوشه‌ی CSVهای اخبار (مثل combo_10day؛ اولویت بر --news-pickle دارد)",
    )
    parser.add_argument("--strategies-json", default=None, help="مسیر strategies_metadata.json")
    parser.add_argument("--version-schema", default=None, help="مسیر version_schema.json (اختیاری)")
    parser.add_argument("--output-dir", required=True, help="مسیر پوشه خروجی برای ذخیره Parquet")
    parser.add_argument(
        "--min-sample-count",
        type=int,
        default=MIN_SAMPLE_GLOBAL_CONDITIONAL_DEFAULT,
        help="آستانه نمونه برای سطح ۱ و ۲ (پیش‌فرض ۵۰)",
    )
    parser.add_argument(
        "--status-file",
        default=None,
        help="مسیر فایل وضعیت برای مدیریت وقفه/ادامه (پیش‌فرض: correlation_status.json در output-dir)",
    )
    parser.add_argument(
        "--resume",
        action="store_true",
        help="در صورت فعال بودن، از آخرین وضعیت ذخیره‌شده ادامه می‌دهد",
    )
    parser.add_argument(
        "--chunk-size",
        type=int,
        default=DEFAULT_CHUNK_SIZE,
        help="تعداد امضاها در هر قطعه پردازشی (پیش‌فرض ۲۰)",
    )
    parser.add_argument(
        "--interrupt-flag",
        default=None,
        help="مسیر فایل interrupt.flag برای توقف graceful (اختیاری)",
    )
    parser.add_argument(
        "--signatures-filter",
        type=str,
        default=None,
        help="مسیر فایل JSON شامل آرایه‌ای از امضاها برای پردازش زیرمجموعه‌ای (اختیاری)",
    )
    return parser.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)
    try:
        run_correlation_pipeline(
            signatures_dir=args.signatures_dir,
            golden_scores_path=args.golden_scores,
            news_pickle_path=args.news_pickle,
            news_dir=args.news_dir,
            strategies_json_path=args.strategies_json,
            version_schema_path=args.version_schema,
            output_dir=args.output_dir,
            min_sample_count=args.min_sample_count,
            status_file=args.status_file,
            resume=args.resume,
            chunk_size=args.chunk_size,
            interrupt_flag_path=args.interrupt_flag,
            signatures_filter=args.signatures_filter,
        )
        return 0
    except Exception as e:
        logger.error("اجرای پایپ‌لاین با خطا متوقف شد: %s", e, exc_info=True)
        return 1


if __name__ == "__main__":
    sys.exit(main())
