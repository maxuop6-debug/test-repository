#!/usr/bin/env python3
"""
portfolios.py - ماژول دوم: سبدهای مکمل (Complementary Portfolios)

این ماژول با استفاده از خروجی ماژول Golden (golden_scores.parquet) و داده‌های
خام per-period (signatures/*.jsonl)، ترکیب‌های بهینه ۲ و ۳ استراتژیِ هم‌گروه
(coin_composition, signature) را پیدا می‌کند: ترکیب‌هایی که بیشترین نرخ بقا
(Survival Rate) و جبران‌سازی متقابل (Compensation) و کمترین همبستگی شرطی را
دارند. خروجی نهایی در portfolios.parquet ذخیره می‌شود.

نیازمندی‌ها:
    pip install pandas numpy scipy pyarrow

اجرا:
    python portfolios.py \
        --signatures-dir /tmp/signatures \
        --golden-scores /tmp/golden_scores.parquet \
        --strategies-json /tmp/strategies_metadata.json \
        --version-schema /tmp/version_schema.json \
        --output-dir /tmp/portfolios_output \
        --top-n 15 \
        --status-file /tmp/portfolios_status.json \
        --resume

نکته درباره‌ی «تاریخ انتشار شاخص غالب» (Step 3 سند):
    رکوردهای signatures که در سند نمونه آمده‌اند فاقد فیلد صریح release_date
    هستند. این پیاده‌سازی، release_date را از روی period_start/period_end و
    موقعیت (pre/post) استخراج‌شده از رشته‌ی signature تخمین می‌زند:
    اگر position == 'post' آنگاه release_date ≈ period_start (دوره از لحظه‌ی
    انتشار شروع می‌شود)، و اگر position == 'pre' آنگاه release_date ≈
    period_end (دوره تا لحظه‌ی انتشار ادامه دارد). اگر در داده‌ی ورودی فیلد
    صریح 'position' یا 'release_date' وجود داشته باشد، آن فیلد به‌جای این
    استنتاج استفاده می‌شود (اولویت با داده‌ی صریح است).
"""

from __future__ import annotations

import argparse
import itertools
import json
import logging
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd
from scipy.stats import spearmanr

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("portfolios")

# -----------------------------------------------------------------------------
# ثابت‌ها
# -----------------------------------------------------------------------------
GOLDEN_SCORE_THRESHOLD = 70.0
MIN_PAIR_OVERLAP = 10
MIN_PORTFOLIO_SAMPLES = 10
CORR_PERCENTILE_THRESHOLD = 25
PORTFOLIO_SIZES = (2, 3)
ABS_MIN_SURVIVAL_RATE = 50.0
ABS_MIN_COMPENSATION_RATIO = 1.0
ABS_MIN_AVG_RETURN = 0.0
SCORE_WEIGHTS = {
    "survival": 0.35,
    "compensation": 0.25,
    "correlation": 0.25,
    "return": 0.15,
}
DEFAULT_VERSION_ID = "v1.0.0"
DEFAULT_CHUNK_SIZE = 20

_POSITION_RE = re.compile(r"_(pre|post)_(\d+)_")

# -----------------------------------------------------------------------------
# بررسی کتابخانه‌ی Parquet
# -----------------------------------------------------------------------------
try:
    import pyarrow  # noqa: F401
    _HAS_PARQUET = True
except ImportError:
    try:
        import fastparquet  # noqa: F401
        _HAS_PARQUET = True
    except ImportError:
        _HAS_PARQUET = False
        log.warning(
            "pyarrow یا fastparquet نصب نیستند — خروجی به‌صورت CSV ذخیره خواهد شد."
        )


def _save_dataframe(df: pd.DataFrame, path: Path) -> Path:
    """ذخیره DataFrame با Parquet یا CSV به‌عنوان fallback."""
    if _HAS_PARQUET:
        out = path.with_suffix(".parquet")
        df.to_parquet(out, index=False)
    else:
        out = path.with_suffix(".csv")
        df.to_csv(out, index=False)
        log.warning("خروجی به‌صورت CSV ذخیره شد (جایگزین Parquet): %s", out)
    return out


def _read_parquet_or_csv(path: Path) -> pd.DataFrame:
    """خواندن فایل Parquet یا CSV."""
    if path.suffix == ".parquet":
        return pd.read_parquet(path)
    elif path.suffix == ".csv":
        return pd.read_csv(path)
    # تلاش با هر دو پسوند
    for suffix in (".parquet", ".csv"):
        candidate = path.with_suffix(suffix)
        if candidate.exists():
            return _read_parquet_or_csv(candidate)
    raise FileNotFoundError(f"فایل {path} پیدا نشد.")


# -----------------------------------------------------------------------------
# مدیریت وضعیت (Status Management)
# -----------------------------------------------------------------------------

def _default_status() -> dict:
    return {
        "processed_signatures": [],
        "last_chunk_index": -1,
        "total_chunks": 0,
        "chunk_size": DEFAULT_CHUNK_SIZE,
        "status": "running",
        "last_updated": datetime.now(timezone.utc).isoformat(),
    }


def load_status(status_file: Path) -> dict:
    """بارگذاری فایل وضعیت در صورت وجود، در غیر این صورت وضعیت پیش‌فرض."""
    if status_file.exists():
        try:
            with open(status_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            log.info("وضعیت قبلی بارگذاری شد از %s (آخرین chunk: %d)",
                     status_file, data.get("last_chunk_index", -1))
            return data
        except (json.JSONDecodeError, OSError) as exc:
            log.warning("خطا در خواندن فایل وضعیت: %s — از ابتدا شروع می‌شود.", exc)
    return _default_status()


def save_status(status_file: Path, status: dict) -> None:
    """ذخیره وضعیت در فایل JSON."""
    status["last_updated"] = datetime.now(timezone.utc).isoformat()
    try:
        status_file.parent.mkdir(parents=True, exist_ok=True)
        tmp = status_file.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(status, f, ensure_ascii=False, indent=2)
        tmp.replace(status_file)
    except OSError as exc:
        log.error("خطا در ذخیره فایل وضعیت: %s", exc)


def check_interrupt_flag(output_dir: Path) -> bool:
    """بررسی وجود فایل interrupt.flag."""
    candidates = [
        output_dir / "interrupt.flag",
        Path("interrupt.flag"),
    ]
    for p in candidates:
        if p.exists():
            log.warning("فایل interrupt.flag شناسایی شد: %s", p)
            return True
    return False


# -----------------------------------------------------------------------------
# گام ۱: بارگذاری داده‌ها
# -----------------------------------------------------------------------------

def load_signatures(signatures_dir: Path) -> pd.DataFrame:
    """تمام فایل‌های .jsonl را از دایرکتوری signatures بارگذاری و یکی می‌کند."""
    files = sorted(signatures_dir.glob("*.jsonl"))
    if not files:
        raise FileNotFoundError(f"هیچ فایل .jsonl در {signatures_dir} پیدا نشد.")

    frames = []
    for fp in files:
        log.info("در حال خواندن %s", fp.name)
        try:
            df = pd.read_json(fp, lines=True)
        except ValueError as exc:
            log.warning("رد شدن از %s به دلیل خطای پارس JSON: %s", fp.name, exc)
            continue
        if df.empty:
            continue
        df["__source_file"] = fp.name
        frames.append(df)

    if not frames:
        raise ValueError("هیچ رکورد معتبری در فایل‌های signatures پیدا نشد.")

    data = pd.concat(frames, ignore_index=True)

    required_cols = {
        "coin_composition", "signature", "strategy_folder",
        "period_start", "period_end", "total_return",
    }
    missing = required_cols - set(data.columns)
    if missing:
        raise ValueError(f"ستون‌های ضروری در داده‌های signatures یافت نشد: {missing}")

    data["strategy_id"] = data["strategy_folder"].astype(str)
    data["period_start"] = pd.to_datetime(data["period_start"])
    data["period_end"] = pd.to_datetime(data["period_end"])
    data["total_return"] = pd.to_numeric(data["total_return"], errors="coerce")
    data = data.dropna(subset=["total_return"])

    log.info("مجموع رکوردهای signatures بارگذاری‌شده: %d", len(data))
    return data


def load_golden_scores(path: Path) -> pd.DataFrame:
    df = _read_parquet_or_csv(path)
    required_cols = {"strategy_id", "coin_composition", "signature", "score"}
    missing = required_cols - set(df.columns)
    if missing:
        raise ValueError(f"ستون‌های ضروری در golden_scores یافت نشد: {missing}")
    df["strategy_id"] = df["strategy_id"].astype(str)
    return df


def load_strategies_metadata(path: Path) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        raw = json.load(f)
    if isinstance(raw, list):
        return {str(item.get("folder")): item for item in raw}
    if isinstance(raw, dict):
        return raw
    raise ValueError("فرمت strategies_metadata.json ناشناخته است.")


def load_version_schema(path: Optional[Path]) -> str:
    if path is None or not path.exists():
        return DEFAULT_VERSION_ID
    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = json.load(f)
        for key in ("version_id", "version", "id"):
            if key in raw:
                return str(raw[key])
        log.warning("کلید version_id در version_schema.json یافت نشد — استفاده از پیش‌فرض.")
        return DEFAULT_VERSION_ID
    except (json.JSONDecodeError, OSError) as exc:
        log.warning("خطا در خواندن version_schema.json: %s — استفاده از پیش‌فرض.", exc)
        return DEFAULT_VERSION_ID


# -----------------------------------------------------------------------------
# گام ۲: پیش‌فیلتر استراتژی‌ها با Golden
# -----------------------------------------------------------------------------

def prefilter_candidates(signatures: pd.DataFrame, golden: pd.DataFrame) -> pd.DataFrame:
    """فقط استراتژی‌هایی با امتیاز Golden >= آستانه را نگه می‌دارد."""
    qualified = golden[golden["score"] >= GOLDEN_SCORE_THRESHOLD][
        ["strategy_id", "coin_composition", "signature"]
    ].drop_duplicates()

    merged = signatures.merge(
        qualified,
        on=["strategy_id", "coin_composition", "signature"],
        how="inner",
    )
    log.info(
        "پیش‌فیلتر Golden (score >= %s): %d/%d رکورد signatures واجد شرایط شدند",
        GOLDEN_SCORE_THRESHOLD, len(merged), len(signatures),
    )
    return merged


# -----------------------------------------------------------------------------
# گام ۳: همبستگی شرطی — تشخیص اشتراک زمانی
# -----------------------------------------------------------------------------

def parse_position(signature: str) -> Optional[str]:
    """استخراج best-effort موقعیت 'pre'/'post' از رشته‌ی signature."""
    m = _POSITION_RE.search(signature)
    return m.group(1) if m else None


def compute_release_date(row: pd.Series) -> pd.Timestamp:
    """تخمین تاریخ انتشار شاخص غالب برای یک رکورد دوره."""
    if "release_date" in row and pd.notna(row["release_date"]):
        return pd.to_datetime(row["release_date"])

    position = row["position"] if "position" in row and pd.notna(row.get("position")) else None
    if position is None:
        position = parse_position(row["signature"])

    if position == "pre":
        return row["period_end"]
    return row["period_start"]


def build_release_dates(group: pd.DataFrame) -> pd.DataFrame:
    group = group.copy()
    group["release_date"] = group.apply(compute_release_date, axis=1)
    return group


def compute_correlation_matrix(group: pd.DataFrame) -> tuple[pd.DataFrame, dict]:
    """
    محاسبه ماتریس همبستگی شرطی Spearman برای یک گروه (coin_composition, signature).

    خروجی:
        corr_df: ستون‌های [a, b, correlation, n]
        valid_periods: دیکشنری {strategy_id: set(release_date های معتبر)}
    """
    pivot = group.pivot_table(
        index="release_date",
        columns="strategy_id",
        values="total_return",
        aggfunc="mean",
    )

    valid_periods = {strat: set(pivot[strat].dropna().index) for strat in pivot.columns}

    strategies = list(pivot.columns)
    rows = []
    for a, b in itertools.combinations(strategies, 2):
        shared = valid_periods[a] & valid_periods[b]
        if len(shared) < MIN_PAIR_OVERLAP:
            continue
        shared_sorted = sorted(shared)
        series_a = pivot.loc[shared_sorted, a]
        series_b = pivot.loc[shared_sorted, b]
        if series_a.nunique() < 2 or series_b.nunique() < 2:
            continue
        corr, _ = spearmanr(series_a, series_b)
        if np.isnan(corr):
            continue
        rows.append({"a": a, "b": b, "correlation": float(corr), "n": len(shared)})

    corr_df = pd.DataFrame(rows, columns=["a", "b", "correlation", "n"])
    return corr_df, valid_periods


# -----------------------------------------------------------------------------
# گام ۴: تعیین آستانه همبستگی (داده‌محور) و فیلتر جفت‌ها
# -----------------------------------------------------------------------------

def filter_pairs_by_correlation(corr_df: pd.DataFrame) -> tuple[pd.DataFrame, float]:
    """جفت‌هایی با همبستگی بیشتر از صدک ۲۵ام را حذف می‌کند."""
    if corr_df.empty:
        return corr_df, float("nan")
    threshold = float(np.percentile(corr_df["correlation"], CORR_PERCENTILE_THRESHOLD))
    kept = corr_df[corr_df["correlation"] <= threshold].copy()
    return kept, threshold


# -----------------------------------------------------------------------------
# گام ۷: معیارهای سبد
# -----------------------------------------------------------------------------

def compensation_ratio(returns: pd.DataFrame) -> float:
    """
    نرخ جبران‌سازی: مجموع سودهای جبران‌کننده / مجموع زیان‌های جبران‌شده.
    اگر مخرج ۰ باشد = ۱.
    """
    gains, losses = 0.0, 0.0
    for _, row in returns.iterrows():
        losers = row[row < 0]
        gainers = row[row > 0]
        if len(losers) > 0 and len(gainers) > 0:
            losses += float(-losers.sum())
            gains += float(gainers.sum())
    if losses == 0:
        return 1.0
    return gains / losses


def survival_rate(returns: pd.DataFrame) -> float:
    """درصد دوره‌هایی که مجموع بازده سبد مثبت بوده است."""
    period_sums = returns.sum(axis=1)
    if len(period_sums) == 0:
        return 0.0
    return float((period_sums > 0).sum()) / float(len(period_sums)) * 100.0


def avg_return(returns: pd.DataFrame) -> float:
    """میانگین مجموع بازده سبد در دوره‌های معتبر."""
    period_sums = returns.sum(axis=1)
    if len(period_sums) == 0:
        return 0.0
    return float(period_sums.mean())


def avg_correlation(members: tuple, corr_lookup: dict) -> float:
    """میانگین همبستگی جفتی بین اعضای سبد."""
    pairs = list(itertools.combinations(sorted(members), 2))
    vals = [corr_lookup[p] for p in pairs if p in corr_lookup]
    if not vals:
        return float("nan")
    return float(np.mean(vals))


# -----------------------------------------------------------------------------
# گام ۹: نرمال‌سازی Percentile Rank
# -----------------------------------------------------------------------------

def percentile_rank(series: pd.Series) -> pd.Series:
    """رتبه‌بندی صدکی بین ۰ تا ۱۰۰ (مقدار بزرگ‌تر => رتبه بالاتر)."""
    if len(series) <= 1:
        return pd.Series(100.0, index=series.index)
    return series.rank(pct=True) * 100.0


# -----------------------------------------------------------------------------
# گام‌های ۵-۹ روی یک گروه (coin_composition, signature)
# -----------------------------------------------------------------------------

def evaluate_group(
    coin_composition: str,
    signature: str,
    group: pd.DataFrame,
    top_n: int,
) -> list[dict]:
    """ارزیابی و رتبه‌بندی سبدهای ۲ و ۳ استراتژی برای یک گروه."""
    group = build_release_dates(group)

    corr_df, valid_periods = compute_correlation_matrix(group)
    if corr_df.empty:
        return []

    kept_pairs, _threshold = filter_pairs_by_correlation(corr_df)
    if kept_pairs.empty:
        return []

    corr_lookup = {(r.a, r.b): r.correlation for r in kept_pairs.itertuples()}
    candidate_strategies = sorted(set(kept_pairs["a"]) | set(kept_pairs["b"]))
    if len(candidate_strategies) < 2:
        return []

    pivot = group.pivot_table(
        index="release_date", columns="strategy_id", values="total_return", aggfunc="mean"
    )

    portfolios = []
    for size in PORTFOLIO_SIZES:
        for members in itertools.combinations(candidate_strategies, size):
            pairs = list(itertools.combinations(sorted(members), 2))
            if not all(p in corr_lookup for p in pairs):
                continue

            shared_periods = set.intersection(*(valid_periods[m] for m in members))
            if len(shared_periods) < MIN_PORTFOLIO_SAMPLES:
                continue

            returns = pivot.loc[sorted(shared_periods), list(members)]

            sr = survival_rate(returns)
            comp = compensation_ratio(returns)
            ar = avg_return(returns)
            ac = avg_correlation(members, corr_lookup)

            # گام ۸: فیلتر مطلق قبل از رنکینگ
            if sr < ABS_MIN_SURVIVAL_RATE:
                continue
            if comp < ABS_MIN_COMPENSATION_RATIO:
                continue
            if ar < ABS_MIN_AVG_RETURN:
                continue

            portfolios.append({
                "coin_composition": coin_composition,
                "signature": signature,
                "members": list(members),
                "survival_rate": sr,
                "compensation_ratio": comp,
                "avg_return": ar,
                "avg_correlation": ac,
                "sample_count": len(shared_periods),
            })

    if not portfolios:
        return []

    pf_df = pd.DataFrame(portfolios)

    # گام ۹: نرمال‌سازی و امتیازدهی
    pf_df["comp_norm"] = percentile_rank(pf_df["compensation_ratio"])
    pf_df["return_norm"] = percentile_rank(pf_df["avg_return"])
    pf_df["corr_norm"] = percentile_rank(-pf_df["avg_correlation"])  # کمتر=بهتر

    pf_df["score"] = (
        SCORE_WEIGHTS["survival"] * pf_df["survival_rate"]
        + SCORE_WEIGHTS["compensation"] * pf_df["comp_norm"]
        + SCORE_WEIGHTS["correlation"] * pf_df["corr_norm"]
        + SCORE_WEIGHTS["return"] * pf_df["return_norm"]
    )

    pf_df = pf_df.sort_values("score", ascending=False).head(top_n)
    pf_df = pf_df.drop(columns=["comp_norm", "corr_norm", "return_norm", "sample_count"])

    return pf_df.to_dict("records")


# -----------------------------------------------------------------------------
# خط‌لوله اصلی با پشتیبانی از chunk-based / resume / interrupt
# -----------------------------------------------------------------------------

def run(
    signatures_dir: Path,
    golden_scores_path: Path,
    strategies_json_path: Path,
    version_schema_path: Optional[Path],
    output_dir: Path,
    top_n: int,
    status_file: Path,
    resume: bool,
    chunk_size: int = DEFAULT_CHUNK_SIZE,
) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)

    # ---- گام ۰: بارگذاری وضعیت قبلی ----
    status = load_status(status_file) if resume else _default_status()
    processed_set: set[tuple] = {
        tuple(sig) if isinstance(sig, list) else sig
        for sig in status.get("processed_signatures", [])
    }

    if resume and processed_set:
        log.info("ادامه از آخرین وقفه — %d امضا قبلاً پردازش شده‌اند.", len(processed_set))

    # ---- گام ۱: بارگذاری داده‌ها ----
    signatures = load_signatures(signatures_dir)
    golden = load_golden_scores(golden_scores_path)
    _strategies_meta = load_strategies_metadata(strategies_json_path)
    version_id = load_version_schema(version_schema_path)

    # ---- گام ۲: پیش‌فیلتر ----
    candidates = prefilter_candidates(signatures, golden)
    if candidates.empty:
        log.warning(
            "هیچ استراتژی واجد شرایط (Golden score >= %s) یافت نشد.",
            GOLDEN_SCORE_THRESHOLD
        )

    # ---- گام ۳: استخراج لیست امضاهای منحصربه‌فرد ----
    all_sig_keys: list[tuple[str, str]] = (
        candidates
        .groupby(["coin_composition", "signature"])
        .size()
        .reset_index()[["coin_composition", "signature"]]
        .apply(tuple, axis=1)
        .tolist()
    )
    log.info("تعداد کل گروه‌های (coin_composition, signature): %d", len(all_sig_keys))

    # حذف امضاهای قبلاً پردازش‌شده در حالت resume
    pending_keys = [k for k in all_sig_keys if k not in processed_set]
    log.info("تعداد گروه‌های باقی‌مانده برای پردازش: %d", len(pending_keys))

    # ---- گام ۴: تقسیم به chunk ----
    chunks = [
        pending_keys[i: i + chunk_size]
        for i in range(0, len(pending_keys), chunk_size)
    ]
    total_chunks = len(chunks)

    # start_chunk_index: اگر resume فعال باشد و قبلاً chunk‌هایی پردازش شده باشند
    start_chunk_index = 0
    if resume and status.get("last_chunk_index", -1) >= 0:
        # چون pending_keys قبلاً پردازش‌شده‌ها را حذف کرده، از ۰ شروع می‌کنیم
        start_chunk_index = 0

    status["total_chunks"] = total_chunks + (status.get("last_chunk_index", -1) + 1 if resume else 0)
    status["chunk_size"] = chunk_size
    status["status"] = "running"
    save_status(status_file, status)

    # بارگذاری نتایج قبلی از فایل موقت اگر resume فعال است
    temp_results_path = output_dir / "_portfolios_temp.parquet"
    all_portfolios: list[dict] = []
    if resume and temp_results_path.exists():
        try:
            prev_df = _read_parquet_or_csv(temp_results_path)
            all_portfolios = prev_df.to_dict("records")
            log.info("نتایج قبلی بارگذاری شد: %d سبد", len(all_portfolios))
        except Exception as exc:
            log.warning("خطا در بارگذاری نتایج موقت قبلی: %s — از صفر شروع می‌شود.", exc)

    interrupted = False

    # ---- گام ۵: پردازش chunk به chunk ----
    groups_df = candidates.groupby(["coin_composition", "signature"])

    for chunk_idx, chunk in enumerate(chunks[start_chunk_index:], start=start_chunk_index):

        # بررسی interrupt.flag قبل از هر chunk
        if check_interrupt_flag(output_dir):
            log.warning("interrupt.flag شناسایی شد — ذخیره وضعیت و توقف.")
            status["status"] = "interrupted"
            interrupted = True
            # ذخیره نتایج موقت
            if all_portfolios:
                temp_df = pd.DataFrame(all_portfolios)
                _save_dataframe(temp_df, temp_results_path.with_suffix(""))
            save_status(status_file, status)
            break

        log.info("پردازش chunk %d/%d (%d امضا)", chunk_idx + 1, total_chunks, len(chunk))

        chunk_results: list[dict] = []

        for coin_composition, signature in chunk:
            key = (coin_composition, signature)
            try:
                group = groups_df.get_group(key)
            except KeyError:
                log.warning("گروه %s یافت نشد — رد شدن.", key)
                processed_set.add(key)
                continue

            if group["strategy_id"].nunique() < 2:
                processed_set.add(key)
                continue

            result = evaluate_group(coin_composition, signature, group, top_n)
            chunk_results.extend(result)
            processed_set.add(key)

        all_portfolios.extend(chunk_results)

        # ---- به‌روزرسانی وضعیت پس از هر chunk ----
        status["last_chunk_index"] = chunk_idx
        status["processed_signatures"] = [list(k) for k in processed_set]
        status["status"] = "running"

        # ذخیره نتایج موقت
        if all_portfolios:
            temp_df = pd.DataFrame(all_portfolios)
            _save_dataframe(temp_df, temp_results_path.with_suffix(""))

        save_status(status_file, status)
        log.info(
            "chunk %d/%d کامل شد — %d سبد جدید / مجموع %d سبد / %d امضا پردازش‌شده",
            chunk_idx + 1, total_chunks, len(chunk_results),
            len(all_portfolios), len(processed_set),
        )

    if interrupted:
        log.info("اجرا به‌صورت graceful متوقف شد. برای ادامه از --resume استفاده کنید.")
        return output_dir / "portfolios.parquet"

    # ---- گام ۶: پس از اتمام همه chunk‌ها ----
    columns = [
        "coin_composition", "signature", "members", "survival_rate",
        "compensation_ratio", "avg_return", "avg_correlation", "score",
        "version_id", "created_at",
    ]

    total_before_filter = len(all_portfolios)

    if not all_portfolios:
        log.warning("هیچ سبدی شرایط لازم را احراز نکرد. فایل خروجی خالی ساخته می‌شود.")
        out_df = pd.DataFrame(columns=columns)
    else:
        out_df = pd.DataFrame(all_portfolios)
        out_df["version_id"] = version_id
        out_df["created_at"] = datetime.now(timezone.utc).isoformat()
        out_df = out_df[[c for c in columns if c in out_df.columns]]

    output_path = output_dir / "portfolios"
    final_path = _save_dataframe(out_df, output_path)
    log.info("ذخیره شد: %s (%d سبد)", final_path, len(out_df))

    # پاک‌کردن فایل موقت
    for suffix in (".parquet", ".csv"):
        candidate = temp_results_path.with_suffix(suffix)
        if candidate.exists():
            try:
                candidate.unlink()
            except OSError:
                pass

    # ---- وضعیت نهایی ----
    status["status"] = "completed"
    status["processed_signatures"] = [list(k) for k in processed_set]
    save_status(status_file, status)

    # ---- تولید فایل خلاصه ----
    summary = {
        "status": "completed",
        "total_signatures": len(all_sig_keys),
        "processed_signatures": len(processed_set),
        "total_portfolios_before_filter": total_before_filter,
        "total_portfolios_after_filter": len(out_df),
        "output_files": [str(final_path.name)],
        "version_id": version_id,
        "completed_at": datetime.now(timezone.utc).isoformat(),
    }
    summary_path = output_dir / "portfolios_summary.json"
    try:
        with open(summary_path, "w", encoding="utf-8") as f:
            json.dump(summary, f, ensure_ascii=False, indent=2)
        log.info("فایل خلاصه ذخیره شد: %s", summary_path)
    except OSError as exc:
        log.warning("خطا در ذخیره فایل خلاصه: %s", exc)

    return final_path


# -----------------------------------------------------------------------------
# CLI
# -----------------------------------------------------------------------------

def parse_args(argv=None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="ماژول سبدهای مکمل (Portfolios) — ساخت و امتیازدهی سبدهای ۲ و ۳ استراتژی",
    )
    parser.add_argument(
        "--signatures-dir", required=True, type=Path,
        help="مسیر پوشه‌ی فایل‌های .jsonl signatures",
    )
    parser.add_argument(
        "--golden-scores", required=True, type=Path,
        help="مسیر فایل golden_scores.parquet",
    )
    parser.add_argument(
        "--strategies-json", required=True, type=Path,
        help="مسیر فایل strategies_metadata.json",
    )
    parser.add_argument(
        "--version-schema", required=False, type=Path, default=None,
        help="مسیر فایل version_schema.json (اختیاری)",
    )
    parser.add_argument(
        "--output-dir", required=True, type=Path,
        help="مسیر پوشه‌ی خروجی برای ذخیره‌ی portfolios.parquet",
    )
    parser.add_argument(
        "--top-n", required=False, type=int, default=15,
        help="تعداد سبدهای برتر برای هر امضا (پیش‌فرض ۱۵)",
    )
    parser.add_argument(
        "--status-file", required=False, type=Path, default=None,
        help="مسیر فایل وضعیت برای مدیریت ادامه (پیش‌فرض: portfolios_status.json در output-dir)",
    )
    parser.add_argument(
        "--resume", action="store_true", default=False,
        help="ادامه از آخرین وضعیت ذخیره‌شده",
    )
    parser.add_argument(
        "--chunk-size", required=False, type=int, default=DEFAULT_CHUNK_SIZE,
        help=f"تعداد امضا در هر chunk (پیش‌فرض {DEFAULT_CHUNK_SIZE})",
    )
    return parser.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)

    output_dir: Path = args.output_dir
    output_dir.mkdir(parents=True, exist_ok=True)

    status_file: Path = (
        args.status_file
        if args.status_file is not None
        else output_dir / "portfolios_status.json"
    )

    try:
        run(
            signatures_dir=args.signatures_dir,
            golden_scores_path=args.golden_scores,
            strategies_json_path=args.strategies_json,
            version_schema_path=args.version_schema,
            output_dir=output_dir,
            top_n=args.top_n,
            status_file=status_file,
            resume=args.resume,
            chunk_size=args.chunk_size,
        )
    except Exception:
        log.exception("اجرای ماژول Portfolios با خطا مواجه شد.")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
