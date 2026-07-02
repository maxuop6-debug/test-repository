"""
golden.py – ماژول ترکیب طلایی (Golden)
شناسایی بهترین استراتژی‌ها برای هر شرایط خبری خاص
"""

import argparse
import json
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

def _save(df: pd.DataFrame, path: Path) -> Path:
    """ذخیره DataFrame به CSV."""
    csv_path = path.with_suffix(".csv")
    df.to_csv(csv_path, index=False)
    return csv_path


def _load(path: Path) -> pd.DataFrame:
    """بارگذاری CSV."""
    csv_path = path.with_suffix(".csv")
    if csv_path.exists():
        return pd.read_csv(csv_path)
    raise FileNotFoundError(csv_path)


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# ثابت‌ها
# ---------------------------------------------------------------------------
DEFAULT_VERSION_ID = "v1.0.0"
DEFAULT_ALPHA = 0.1
BTC_MIN_SAMPLES = 10
ALT_MIN_SAMPLES = 5
BTC_DATA_YEARS = 4  # آستانه برای تشخیص BTC/Altcoin
DEFAULT_CHUNK_SIZE = 50  # تعداد امضا در هر قطعه (chunk)


# ---------------------------------------------------------------------------
# گام ۱: بارگذاری داده‌ها
# ---------------------------------------------------------------------------

def load_signatures(signatures_dir: str) -> pd.DataFrame:
    """بارگذاری تمام فایل‌های .jsonl از دایرکتوری مشخص.

    باگ dedup fix: هر ردیف با مسیر نسبیِ فایل مبدأش (source_file) برچسب می‌خورد.
    این مسیر همان چیزی است که در completed_golden.json / all_combinations_golden.json
    استفاده می‌شود (نه «امضای خبری» سطح ردیف که در build_signature ساخته می‌شود).
    بدون این، dedup در سطح YAML هیچ‌وقت کار نمی‌کرد چون دو نوع «امضا»
    در دو سطح متفاوت (فایل در برابر رکورد) با هم مقایسه می‌شدند.
    """
    path = Path(signatures_dir)
    files = list(path.rglob("*.jsonl"))
    if not files:
        raise FileNotFoundError(f"هیچ فایل .jsonl در {signatures_dir} یافت نشد.")

    frames = []
    for f in files:
        log.info(f"بارگذاری: {f.name}")
        df = pd.read_json(f, lines=True)
        rel_path = f.relative_to(path).as_posix()
        df["source_file"] = rel_path
        frames.append(df)

    data = pd.concat(frames, ignore_index=True)
    log.info(f"تعداد کل رکوردها: {len(data):,}")
    return data


def load_strategies(strategies_json: str | None) -> dict:
    """بارگذاری metadata استراتژی‌ها (اختیاری — این فایل در هیچ‌جا استفاده نمی‌شود)."""
    if not strategies_json or not Path(strategies_json).exists():
        return {}
    with open(strategies_json, encoding="utf-8") as fh:
        return json.load(fh)


def load_archive_index(archive_index_path: str | None) -> dict:
    """بارگذاری نگاشت signature_path -> archive_path از فایل ایندکس (اختیاری).

    فرمت فایل باید همان signature_archive_index.json باشد که در مخزن سوم
    نگهداری می‌شود: یک شیء JSON ساده {signature_path: archive_path, ...}.
    اگر مسیر داده نشود یا فایل موجود نباشد، نگاشت خالی برگردانده می‌شود و
    ستون archive_path در خروجی همه‌جا None خواهد بود (بدون خطا).
    """
    if not archive_index_path:
        log.info("[ARCHIVE-INDEX] --archive-index داده نشده — ستون archive_path خالی (None) خواهد بود.")
        return {}
    idx_path = Path(archive_index_path)
    if not idx_path.exists():
        log.warning(f"[ARCHIVE-INDEX] فایل {idx_path} یافت نشد — ستون archive_path خالی (None) خواهد بود.")
        return {}
    try:
        with open(idx_path, encoding="utf-8") as fh:
            mapping = json.load(fh)
        if not isinstance(mapping, dict):
            log.warning(f"[ARCHIVE-INDEX] محتوای {idx_path} یک dict نیست — نادیده گرفته شد.")
            return {}
        log.info(f"[ARCHIVE-INDEX] {len(mapping):,} نگاشت signature_path -> archive_path از {idx_path} بارگذاری شد.")
        return mapping
    except Exception as e:
        log.warning(f"[ARCHIVE-INDEX] خطا در خواندن {idx_path}: {e} — ستون archive_path خالی (None) خواهد بود.")
        return {}


def load_version_schema(version_schema: str | None) -> str:
    """استخراج version_id از فایل schema یا مقدار پیش‌فرض."""
    if not version_schema or not Path(version_schema).exists():
        return DEFAULT_VERSION_ID
    with open(version_schema, encoding="utf-8") as fh:
        schema = json.load(fh)
    return schema.get("version_id", DEFAULT_VERSION_ID)




# ---------------------------------------------------------------------------
# ساخت امضای خبری
# ---------------------------------------------------------------------------

def build_signature(row: pd.Series) -> str:
    """ساخت امضای خبری از فیلدهای یک رکورد."""
    regime = row.get("market_regime")
    if regime is None or (isinstance(regime, float) and pd.isna(regime)) or regime == "":
        regime = "unknown"
    coin = row.get("coin_composition", "")
    indicator = row.get("dominant_indicator", "")
    position = row.get("position")
    if position is None or (isinstance(position, float) and pd.isna(position)):
        position = "none"
    distance = row.get("distance_days")
    if distance is None or (isinstance(distance, float) and pd.isna(distance)):
        distance = 0
    model = row.get("model", "")
    return f"{coin}_{indicator}_{position}_{distance}_{model}_{regime}"


# ---------------------------------------------------------------------------
# گام ۲: محاسبه معیارهای خام
# ---------------------------------------------------------------------------

def _representative_value(series: pd.Series):
    """انتخاب رایج‌ترین مقدار یک سری (در صورت تساوی، اولین مقدار).

    برای انتخاب signature_path نماینده از بین چند source_file که ممکن است
    به یک signature یکسان ختم شوند، استفاده می‌شود.
    """
    clean = series.dropna()
    if clean.empty:
        return None
    counts = clean.value_counts()
    return counts.index[0]


def consecutive_losses(returns: list[float]) -> int:
    """محاسبه حداکثر تعداد ضررهای متوالی."""
    max_consec = 0
    current = 0
    for r in returns:
        if r <= 0:
            current += 1
            max_consec = max(max_consec, current)
        else:
            current = 0
    return max_consec


def compute_raw_metrics(group: pd.DataFrame) -> dict:
    """محاسبه معیارهای خام برای یک گروه (استراتژی, امضا)."""
    returns = group["total_return"].tolist()
    n = len(returns)

    positive = [r for r in returns if r > 0]
    negative = [r for r in returns if r <= 0]

    win_rate = (len(positive) / n * 100) if n > 0 else 0.0

    sum_neg = abs(sum(negative))
    if sum_neg == 0:
        profit_factor = None  # all-win – بعداً جایگزین می‌شود
    else:
        profit_factor = sum(positive) / sum_neg if positive else 0.0

    max_loss_ratio = consecutive_losses(returns) / n if n > 0 else 0.0

    avg_daily_return = group["avg_daily_return"].mean() if "avg_daily_return" in group.columns else 0.0

    # signature_path: مسیر نسبی فایل JSONL نماینده (رایج‌ترین source_file در گروه).
    # یک گروه (strategy_id, coin, signature) ممکن است از چند فایل JSONL تشکیل شده
    # باشد (چون چند فایل می‌توانند به یک امضای خبری یکسان ختم شوند)، بنابراین
    # رایج‌ترین source_file به‌عنوان نماینده انتخاب می‌شود.
    signature_path = (
        _representative_value(group["source_file"]) if "source_file" in group.columns else None
    )

    return {
        "win_rate": win_rate,
        "profit_factor": profit_factor,
        "max_loss_ratio": max_loss_ratio,
        "avg_daily_return": avg_daily_return,
        "sample_count": n,
        "total_return_sum": sum(returns),
        "signature_path": signature_path,
    }


def resolve_all_win_pf(raw_df: pd.DataFrame) -> pd.DataFrame:
    """جایگزینی profit_factor=None (all-win) بر اساس total_return گروه.

    - اگر total_return > 1: گروه معتبر است → ضریب 1.5 * max_valid_pf
    - اگر total_return <= 1: سود ناچیز است → ضریب پایین (1.0 * max_valid_pf یا کمتر)
    """
    valid_pf = raw_df.loc[raw_df["profit_factor"].notna(), "profit_factor"]
    max_valid_pf = valid_pf.max() if not valid_pf.empty else 1.0

    all_win_mask = raw_df["profit_factor"].isna()

    def replacement_for(total_return: float) -> float:
        if total_return is not None and total_return > 1:
            return max_valid_pf * 1.5
        return max_valid_pf * 1.0

    raw_df.loc[all_win_mask, "profit_factor"] = raw_df.loc[all_win_mask, "total_return_sum"].apply(
        replacement_for
    )
    return raw_df


# ---------------------------------------------------------------------------
# گام ۳: فیلتر کم‌تکرار
# ---------------------------------------------------------------------------

def is_btc_like(coin: str) -> bool:
    """تشخیص اینکه ارز به اندازه کافی تاریخچه دارد یا خیر."""
    return "BTC" in coin.upper()


def filter_low_sample(raw_df: pd.DataFrame) -> tuple[pd.DataFrame, int]:
    """حذف گروه‌های با تکرار کم."""
    initial = len(raw_df)

    def min_samples(coin: str) -> int:
        return BTC_MIN_SAMPLES if is_btc_like(coin) else ALT_MIN_SAMPLES

    mask = raw_df.apply(lambda r: r["sample_count"] >= min_samples(r["coin_composition"]), axis=1)
    filtered = raw_df[mask].copy()
    removed = initial - len(filtered)
    return filtered, removed


# ---------------------------------------------------------------------------
# گام ۴: نرمال‌سازی با Percentile Rank
# ---------------------------------------------------------------------------

def percentile_rank(series: pd.Series) -> pd.Series:
    """محاسبه Percentile Rank برای یک سری."""
    ranks = series.rank(method="average", pct=True) * 100
    return ranks


def normalize_metrics(df: pd.DataFrame) -> pd.DataFrame:
    """نرمال‌سازی معیارها به تفکیک coin_composition."""
    df = df.copy()

    for metric in ["win_rate", "profit_factor", "avg_daily_return"]:
        norm_col = f"{metric}_norm"
        df[norm_col] = df.groupby("coin_composition")[metric].transform(percentile_rank)

    # max_loss_ratio: هر چه کمتر، بهتر – قبل از نرمال‌سازی معکوس می‌شود
    df["max_loss_inv"] = 100 - (df["max_loss_ratio"] * 100)
    df["max_loss_ratio_norm"] = df.groupby("coin_composition")["max_loss_inv"].transform(percentile_rank)
    df.drop(columns=["max_loss_inv"], inplace=True)

    return df


# ---------------------------------------------------------------------------
# گام ۵: محاسبه امتیاز نهایی
# ---------------------------------------------------------------------------

def compute_score(row: pd.Series) -> float:
    """محاسبه امتیاز نهایی با وزن‌های ثابت."""
    score = (
        0.30 * row["win_rate_norm"]
        + 0.30 * row["profit_factor_norm"]
        + 0.20 * (100 - row["max_loss_ratio_norm"])
        + 0.20 * row["avg_daily_return_norm"]
    )
    return round(float(score), 4)


# ---------------------------------------------------------------------------
# مدیریت وضعیت (Status) برای وقفه و ادامه (Resume)
# ---------------------------------------------------------------------------

def load_status(status_path: Path) -> dict | None:
    """بارگذاری فایل وضعیت در صورت وجود."""
    if not status_path.exists():
        return None
    try:
        with open(status_path, encoding="utf-8") as fh:
            return json.load(fh)
    except (json.JSONDecodeError, OSError) as exc:
        log.warning(f"خواندن فایل وضعیت ناموفق بود ({exc}) – نادیده گرفته شد.")
        return None


def save_status(
    status_path: Path,
    processed_signatures,
    last_chunk_index: int,
    total_chunks: int,
    status: str,
    chunk_size: int = DEFAULT_CHUNK_SIZE,
    processed_files=None,
) -> dict:
    """ذخیره فایل وضعیت golden_status.json.

    باگ dedup fix: علاوه بر processed_signatures (امضای سطح ردیف، برای resume داخلی)،
    processed_files (مسیر نسبی فایل‌های .jsonl پردازش‌شده) هم ذخیره می‌شود.
    این لیست دوم همان چیزی است که باید با completed_golden.json در YAML مقایسه شود.
    """
    payload = {
        "processed_signatures": sorted(set(processed_signatures)),
        "processed_files": sorted(set(processed_files)) if processed_files is not None else [],
        "last_chunk_index": last_chunk_index,
        "total_chunks": total_chunks,
        "chunk_size": chunk_size,
        "status": status,
        "last_updated": datetime.now(timezone.utc).isoformat(),
    }
    status_path.parent.mkdir(parents=True, exist_ok=True)
    with open(status_path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2)
    return payload


def check_interrupt(interrupt_flag_path: Path | None) -> bool:
    """بررسی وجود فایل interrupt.flag."""
    if interrupt_flag_path is None:
        return False
    return Path(interrupt_flag_path).exists()


def chunk_list(items: list, chunk_size: int) -> list:
    """تقسیم یک لیست به قطعات با اندازه‌ی ثابت."""
    if chunk_size <= 0:
        chunk_size = DEFAULT_CHUNK_SIZE
    return [items[i:i + chunk_size] for i in range(0, len(items), chunk_size)]


def write_summary(
    output_path: Path,
    status: str,
    total_signatures: int,
    processed_signatures_count: int,
    total_groups: int,
    filtered_groups: int,
    output_files: list,
    version_id: str,
    completed_at: datetime,
) -> Path:
    """نوشتن خروجی خلاصه golden_summary.json."""
    summary = {
        "status": status,
        "total_signatures": total_signatures,
        "processed_signatures": processed_signatures_count,
        "total_groups": total_groups,
        "filtered_groups": filtered_groups,
        "output_files": output_files,
        "version_id": version_id,
        "completed_at": completed_at.isoformat(),
    }
    summary_path = output_path / "golden_summary.json"
    with open(summary_path, "w", encoding="utf-8") as fh:
        json.dump(summary, fh, ensure_ascii=False, indent=2)
    log.info(f"golden_summary ذخیره شد: {summary_path}")
    return summary_path


# ---------------------------------------------------------------------------
# پردازش اصلی
# ---------------------------------------------------------------------------

def run(
    signatures_dir: str,
    strategies_json: str | None,
    version_schema: str | None,
    ohlc_dir: str | None,
    output_dir: str,
    alpha: float,
    status_file: str | None = None,
    resume: bool = False,
    chunk_size: int = DEFAULT_CHUNK_SIZE,
    interrupt_flag: str | None = None,
    archive_index: str | None = None,
) -> None:
    now_utc = datetime.now(timezone.utc)
    output_path = Path(output_dir)
    output_path.mkdir(parents=True, exist_ok=True)

    status_path = Path(status_file) if status_file else output_path / "golden_status.json"
    interrupt_path = Path(interrupt_flag) if interrupt_flag else output_path / "interrupt.flag"

    # ── بارگذاری ──────────────────────────────────────────────────────────
    log.info("بارگذاری signatures...")
    sig_df = load_signatures(signatures_dir)

    # ── بارگذاری ایندکس archive_path (اختیاری) ──────────────────────────
    sig_to_archive = load_archive_index(archive_index)

    version_id = load_version_schema(version_schema)
    log.info(f"version_id: {version_id}")

    # ── ساخت امضا ─────────────────────────────────────────────────────────
    log.info("ساخت امضاهای خبری...")
    sig_df["signature"] = sig_df.apply(build_signature, axis=1)

    unique_signatures = sorted(sig_df["signature"].unique().tolist())
    total_signatures = len(unique_signatures)

    # باگ dedup fix: نگاشت هر فایل مبدأ به مجموعه‌ی امضاهای داخل آن، تا بدانیم
    # یک فایل JSONL کِی "کامل" پردازش شده (یعنی همه‌ی امضاهایش در چانک‌های
    # پردازش‌شده هستند) و آن را به completed_golden.json اضافه کنیم.
    file_to_signatures: dict[str, set] = {}
    for src_file, sub_df in sig_df.groupby("source_file"):
        file_to_signatures[src_file] = set(sub_df["signature"].unique().tolist())
    total_files = len(file_to_signatures)
    log.info(f"تعداد فایل‌های .jsonl منبع: {total_files:,}")

    # ── گام ۳: تقسیم به قطعات ────────────────────────────────────────────
    chunks = chunk_list(unique_signatures, chunk_size)
    total_chunks = len(chunks)
    log.info(
        f"تعداد امضاهای منحصربه‌فرد: {total_signatures:,} | "
        f"اندازه قطعه: {chunk_size} | تعداد قطعات: {total_chunks:,}"
    )

    processed_signatures: set = set()
    processed_files: set = set()
    start_chunk = 0
    raw_records: list = []

    # ── گام ۰: بارگذاری وضعیت قبلی (در صورت --resume) ───────────────────
    if resume:
        prev_status = load_status(status_path)
        if prev_status and prev_status.get("status") in ("interrupted", "running"):
            processed_signatures = set(prev_status.get("processed_signatures", []))
            processed_files = set(prev_status.get("processed_files", []))
            start_chunk = prev_status.get("last_chunk_index", -1) + 1
            log.info(
                f"ادامه از وضعیت قبلی: شروع از قطعه {start_chunk}/{total_chunks} | "
                f"امضاهای پردازش‌شده قبلی: {len(processed_signatures):,} | "
                f"فایل‌های کامل قبلی: {len(processed_files):,}"
            )
            try:
                existing_raw = _load(output_path / "golden_raw.csv")
                raw_records = existing_raw.to_dict("records")
                log.info(f"رکوردهای خام قبلی بارگذاری شد: {len(raw_records):,}")
            except FileNotFoundError:
                log.info("فایل golden_raw قبلی یافت نشد – ادامه با رکوردهای خالی.")
        else:
            log.info("هیچ وضعیت قابل‌ادامه‌ای (running/interrupted) یافت نشد. شروع از ابتدا.")

    if total_chunks == 0:
        log.warning("هیچ امضایی برای پردازش یافت نشد.")
        save_status(status_path, processed_signatures, -1, 0, "completed", chunk_size, processed_files)
        write_summary(output_path, "completed", 0, 0, 0, 0, [], version_id, now_utc)
        return

    # ── گام ۴: پردازش هر قطعه ────────────────────────────────────────────
    interrupted = False
    for chunk_idx in range(start_chunk, total_chunks):
        if check_interrupt(interrupt_path):
            log.warning(f"interrupt.flag یافت شد - توقف پردازش قبل از قطعه {chunk_idx}.")
            raw_df_partial = pd.DataFrame(raw_records)
            if not raw_df_partial.empty:
                _save(raw_df_partial, output_path / "golden_raw.csv")
            save_status(status_path, processed_signatures, chunk_idx - 1, total_chunks, "interrupted", chunk_size, processed_files)
            interrupted = True
            break

        chunk_signatures = set(chunks[chunk_idx])
        chunk_df = sig_df[sig_df["signature"].isin(chunk_signatures)]

        groups = chunk_df.groupby(["strategy_folder", "coin_composition", "signature"])
        for (strategy_id, coin, signature), group_df in groups:
            metrics = compute_raw_metrics(group_df)
            raw_records.append(
                {
                    "strategy_id": strategy_id,
                    "coin_composition": coin,
                    "signature": signature,
                    **metrics,
                    "created_at": now_utc,
                }
            )

        processed_signatures.update(chunk_signatures)

        # باگ dedup fix: یک فایل را "کامل" علامت بزن فقط وقتی همه‌ی
        # امضاهای آن فایل دیگر در processed_signatures باشند.
        for src_file, sigs in file_to_signatures.items():
            if src_file in processed_files:
                continue
            if sigs.issubset(processed_signatures):
                processed_files.add(src_file)

        raw_df_partial = pd.DataFrame(raw_records)
        _save(raw_df_partial, output_path / "golden_raw.csv")
        save_status(status_path, processed_signatures, chunk_idx, total_chunks, "running", chunk_size, processed_files)
        log.info(
            f"قطعه {chunk_idx + 1}/{total_chunks} پردازش شد ({len(chunk_signatures)} امضا) | "
            f"فایل‌های کامل تاکنون: {len(processed_files):,}/{total_files:,}"
        )

    if interrupted:
        log.info("پردازش به دلیل interrupt.flag متوقف شد (خروج graceful با کد ۰).")
        return

    # ── حذف رکوردهای تکراری احتمالی ناشی از resume‌های قبلی ──────────────
    raw_df = pd.DataFrame(raw_records)
    if not raw_df.empty:
        raw_df = raw_df.drop_duplicates(
            subset=["strategy_id", "coin_composition", "signature"], keep="last"
        ).reset_index(drop=True)
    total_groups = len(raw_df)

    if raw_df.empty:
        log.warning("هیچ داده‌ای برای پردازش یافت نشد.")
        save_status(status_path, processed_signatures, total_chunks - 1, total_chunks, "completed", chunk_size, processed_files)
        write_summary(output_path, "completed", total_signatures, len(processed_signatures), 0, 0, [], version_id, now_utc)
        return

    # ── مدیریت all-win ────────────────────────────────────────────────────
    raw_df = resolve_all_win_pf(raw_df)

    # ── ذخیره golden_raw.csv ──────────────────────────────────────────
    raw_out = _save(raw_df, output_path / "golden_raw.csv")
    log.info(f"golden_raw ذخیره شد: {raw_out}")

    # ── حذف فیلتر کم‌تکرار: استفاده مستقیم از داده‌های خام ─────────────────
    log.info("فیلتر کم‌تکرار غیرفعال است – استفاده مستقیم از raw_df.")
    filtered_df = raw_df
    removed_count = 0
    log.info(f"گروه‌های باقیمانده: {len(filtered_df):,}")

    if filtered_df.empty:
        log.warning("هیچ گروه معتبری پس از فیلتر باقی نماند. خروج.")
        save_status(status_path, processed_signatures, total_chunks - 1, total_chunks, "completed", chunk_size, processed_files)
        write_summary(
            output_path, "completed", total_signatures, len(processed_signatures),
            total_groups, 0, [raw_out.name], version_id, now_utc,
        )
        return

    # ── نرمال‌سازی ────────────────────────────────────────────────────────
    log.info("نرمال‌سازی معیارها با Percentile Rank...")
    norm_df = normalize_metrics(filtered_df)

    # ── امتیاز نهایی ──────────────────────────────────────────────────────
    log.info("محاسبه امتیاز نهایی...")
    norm_df["score"] = norm_df.apply(compute_score, axis=1)

    # ── آماده‌سازی golden_scores.csv ──────────────────────────────────
    # هیچ ادغامی صورت نمی‌گیرد: هر (strategy_id, signature, coin_composition)
    # به‌عنوان یک ردیف مجزا در خروجی باقی می‌ماند تا عملکرد هر ترکیب کوین
    # به‌طور مستقل قابل بررسی باشد.
    # signature_path ممکن است در برخی مسیرهای قدیمی وجود نداشته باشد؛
    # برای سازگاری با عقب، در صورت نبود، ستون خالی (None) ساخته می‌شود.
    if "signature_path" not in norm_df.columns:
        norm_df["signature_path"] = None

    scores_out_df = norm_df[
        ["strategy_id", "coin_composition", "signature_path", "signature", "score", "sample_count"]
    ].copy()
    scores_out_df["version_id"] = version_id
    scores_out_df["calculated_at"] = now_utc

    # ── پر کردن archive_path از ایندکس (در صورت وجود) ────────────────────
    # این ستون همان چیزی است که build_all_queues.py قبلاً به‌صورت fallback
    # از signature_archive_index.json می‌خواند؛ حالا مستقیماً همینجا در
    # خودِ golden_scores.csv نوشته می‌شود تا نیازی به fallback runtime نباشد.
    scores_out_df["archive_path"] = scores_out_df["signature_path"].map(sig_to_archive)
    matched_archive_count = int(scores_out_df["archive_path"].notna().sum())
    missing_archive_count = len(scores_out_df) - matched_archive_count
    log.info(
        f"[ARCHIVE-INDEX] archive_path پر شد برای {matched_archive_count:,} ردیف | "
        f"یافت نشد برای {missing_archive_count:,} ردیف از {len(scores_out_df):,}"
    )
    if sig_to_archive and missing_archive_count:
        log.warning(
            f"[ARCHIVE-INDEX] {missing_archive_count:,} ردیف signature_path‌ای دارند که در "
            "ایندکس archive_index یافت نشد — archive_path=None برای این ردیف‌ها ثبت می‌شود."
        )

    scores_out = _save(scores_out_df, output_path / "golden_scores.csv")
    log.info(f"golden_scores ذخیره شد: {scores_out}")

    # ── وضعیت نهایی: completed ───────────────────────────────────────────
    save_status(status_path, processed_signatures, total_chunks - 1, total_chunks, "completed", chunk_size, processed_files)

    # ── خروجی خلاصه golden_summary.json ──────────────────────────────────
    write_summary(
        output_path,
        "completed",
        total_signatures,
        len(processed_signatures),
        total_groups,
        len(filtered_df),
        [raw_out.name, scores_out.name],
        version_id,
        now_utc,
    )

    # ── گزارش نهایی ───────────────────────────────────────────────────────
    log.info("=" * 60)
    log.info(f"تعداد گروه‌های پردازش‌شده:  {len(filtered_df):,}")
    log.info(f"تعداد گروه‌های حذف‌شده:     {removed_count:,}")
    log.info(f"تعداد ردیف‌های نهایی امتیاز: {len(scores_out_df):,}")
    log.info(f"خروجی‌ها در: {output_path.resolve()}")
    log.info("=" * 60)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="ماژول Golden – امتیازدهی استراتژی‌ها بر اساس شرایط خبری"
    )
    parser.add_argument("--signatures-dir", required=True, help="مسیر پوشه فایل‌های .jsonl signatures")
    parser.add_argument("--strategies-json", required=False, default=None, help="(اختیاری، بی‌استفاده) مسیر فایل strategies_metadata.json")
    parser.add_argument("--version-schema", default=None, help="(اختیاری) مسیر فایل version_schema.json")
    parser.add_argument("--ohlc-dir", default=None, help="(اختیاری) مسیر پوشه داده‌های OHLC")
    parser.add_argument("--output-dir", required=True, help="مسیر پوشه خروجی")
    parser.add_argument("--alpha", type=float, default=DEFAULT_ALPHA, help="ضریب بازخورد Correlation (پیش‌فرض: 0.1)")
    parser.add_argument(
        "--status-file",
        default=None,
        help="(اختیاری) مسیر فایل وضعیت برای مدیریت وقفه و ادامه (پیش‌فرض: golden_status.json در output-dir)",
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
        help=f"تعداد امضا در هر قطعه (پیش‌فرض: {DEFAULT_CHUNK_SIZE})",
    )
    parser.add_argument(
        "--interrupt-flag",
        default=None,
        help="(اختیاری) مسیر فایل interrupt.flag برای توقف کنترل‌شده (پیش‌فرض: interrupt.flag در output-dir)",
    )
    parser.add_argument(
        "--archive-index",
        default=None,
        help="(اختیاری) مسیر فایل signature_archive_index.json برای پر کردن ستون archive_path "
             "در golden_scores.csv. اگر داده نشود، archive_path=None ثبت می‌شود.",
    )
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    run(
        signatures_dir=args.signatures_dir,
        strategies_json=args.strategies_json,
        version_schema=args.version_schema,
        ohlc_dir=args.ohlc_dir,
        output_dir=args.output_dir,
        alpha=args.alpha,
        status_file=args.status_file,
        resume=args.resume,
        chunk_size=args.chunk_size,
        interrupt_flag=args.interrupt_flag,
        archive_index=args.archive_index,
    )
    sys.exit(0)
