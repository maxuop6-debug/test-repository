#!/usr/bin/env python3
# calculators.py
# ترکیب یکپارچه calculator_returns، calculator_risk، calculator_inverse، complete_reporter
# اجرا: python calculators.py <subcommand> [args]
# subcommands: returns | risk | inverse | report

import os
import sys
import json
import csv
import re
import pickle
import hashlib
import argparse
import logging
import statistics
from datetime import datetime
from decimal import Decimal
from collections import defaultdict
from itertools import combinations

# ══════════════════════════════════════════════════════════════
# لاگ
# ══════════════════════════════════════════════════════════════

def setup_logging(log_file="logs/run.log"):
    os.makedirs(os.path.dirname(log_file) if os.path.dirname(log_file) else ".", exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[
            logging.StreamHandler(),
            logging.FileHandler(log_file, encoding="utf-8"),
        ],
    )

log = logging.getLogger(__name__)

# ══════════════════════════════════════════════════════════════
# توابع پایه
# ══════════════════════════════════════════════════════════════

def safe_percentage(value):
    if value is None:
        return 0.0
    if isinstance(value, (int, float)):
        return float(value)
    cleaned = str(value).replace('%', '').replace('+', '').strip()
    if not cleaned:
        return 0.0
    try:
        return float(cleaned)
    except Exception:
        return 0.0

def safe_float(value, default=0.0):
    if value is None:
        return default
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(str(value).strip())
    except Exception:
        return default

def round_to_nearest(value, options):
    if not options:
        return value
    return min(options, key=lambda x: abs(x - value))

def apply_special_rounding(percent, move_percents):
    if not move_percents or percent == 0:
        return percent
    try:
        if percent > 0:
            result = round_to_nearest(percent + 0.05, move_percents) - 0.05
            return result - 0.1
        else:
            result = round_to_nearest(abs(percent) + 0.05, move_percents) - 0.05
            return -(result - 0.1)  # باگ ۱: قبلاً +0.1 بود → کارمزد کمتر روی ضررها
    except Exception:
        return percent

# ══════════════════════════════════════════════════════════════
# رمزگشایی و بارگذاری فایل یکپارچه
# ══════════════════════════════════════════════════════════════

def decrypt_enc_file(enc_path, password):
    import subprocess
    script = (
        f"const crypto=require('crypto'),fs=require('fs');"
        f"const pass={json.dumps(password)};"
        f"const data=fs.readFileSync({json.dumps(enc_path)});"
        f"const key=crypto.scryptSync(pass,'salt',32);"
        f"const iv=data.slice(0,16),enc=data.slice(16);"
        f"const decipher=crypto.createDecipheriv('aes-256-cbc',key,iv);"
        f"const dec=Buffer.concat([decipher.update(enc),decipher.final()]);"
        f"process.stdout.write(dec);"
    )
    result = subprocess.run(["node", "-e", script], capture_output=True)
    if result.returncode != 0:
        raise RuntimeError(f"رمزگشایی ناموفق: {result.stderr.decode()}")
    return json.loads(result.stdout.decode("utf-8"))

def load_aggregated_data(enc_path, password=None):
    if not password:
        password = os.environ.get("RESULTS_PASSWORD", os.environ.get("DATA_PASSWORD", ""))
    if enc_path.endswith(".enc"):
        raw = decrypt_enc_file(enc_path, password)
    else:
        with open(enc_path, "r", encoding="utf-8") as f:
            raw = json.load(f)
    trades = raw.get("trades") or []
    metadata = raw.get("metadata") or {}
    if not trades:
        log.warning("%s: trades خالی است – مقادیر پیش‌فرض اعمال می‌شود.", enc_path)
    if not metadata:
        log.warning("%s: metadata موجود نیست – مقادیر پیش‌فرض اعمال می‌شود.", enc_path)
        metadata = {"move_percents": [], "stop_loss_initial": -2.0, "max_move_percent": None}
    return {"trades": trades, "metadata": metadata}

# ══════════════════════════════════════════════════════════════
# گروه‌بندی trades بر اساس symbol
# ══════════════════════════════════════════════════════════════

def get_symbol_combinations(symbols):
    """همه ترکیب‌های ممکن از ۱ تا n کوین را برمی‌گرداند."""
    syms = sorted(symbols)
    combos = []
    for r in range(1, len(syms) + 1):
        for combo in combinations(syms, r):
            combos.append(combo)
    return combos

def combo_key(folder, combo):
    """کلید ترکیب: folder_SYM1_SYM2_..."""
    return f"{folder}_{'_'.join(combo)}"

def combo_label(combo):
    """برچسب نمایشی ترکیب."""
    return "_".join(combo)

def get_symbol(trade):
    return trade.get("symbol") or trade.get("pair") or trade.get("coin") or "unknown"

def group_by_symbol(trades):
    groups = defaultdict(list)
    for t in trades:
        groups[get_symbol(t)].append(t)
    return dict(groups)

# ══════════════════════════════════════════════════════════════
# محاسبات بازده
# ══════════════════════════════════════════════════════════════

def real_return(trades):
    return float(sum(Decimal(str(safe_percentage(t.get("profitPercent", 0)))) for t in trades))

def special_return(trades, move_percents):
    return float(sum(
        Decimal(str(apply_special_rounding(safe_percentage(t.get("profitPercent", 0)), move_percents)))
        for t in trades
    ))

# ══════════════════════════════════════════════════════════════
# محاسبات ریسک
# ══════════════════════════════════════════════════════════════

def max_consecutive_count(trades):
    best = cur = 0
    for t in trades:
        if safe_percentage(t.get("profitPercent", 0)) < 0:
            cur += 1
            best = max(best, cur)
        else:
            cur = 0
    return best

def consecutive_loss_value(count, stop_loss):
    """مجموع ضررهای متوالی بر اساس درصد stop_loss."""
    count = int(count or 0)
    sl = float(stop_loss)
    # باگ ۳: اگر stop_loss مثبت ذخیره شده باشد، نتیجه باید منفی باشد
    return count * (-abs(sl))

def max_drawdown(trades):
    peak = cum = 0.0
    dd = 0.0
    for t in trades:
        cum += safe_percentage(t.get("profitPercent", 0))
        if cum > peak:
            peak = cum
        drawdown = peak - cum
        if drawdown > dd:
            dd = drawdown
    return -dd  # منفی — افت سرمایه

def sharpe_ratio(trades):
    profits = [safe_percentage(t.get("profitPercent", 0)) for t in trades]
    if len(profits) < 2:
        return 0.0
    try:
        std = statistics.stdev(profits)
        return (statistics.mean(profits) / std) if std else 0.0
    except Exception:
        return 0.0

def profit_loss_ratio(trades):
    profits = [safe_percentage(t.get("profitPercent", 0)) for t in trades]
    pos = [p for p in profits if p > 0]
    neg = [p for p in profits if p < 0]
    if not pos or not neg:
        return 0.0
    return statistics.mean(pos) / abs(statistics.mean(neg))

# ══════════════════════════════════════════════════════════════
# توزیع معکوس
# ══════════════════════════════════════════════════════════════

def build_inverse_dist(trades, stop_loss, max_gain):
    inv_sl = abs(stop_loss)
    agg = defaultdict(int)
    for t in trades:
        p = safe_percentage(t.get("profitPercent", 0))
        if p > 0:
            inv = -inv_sl
        elif p < 0:
            g = abs(p)
            inv = min(g, max_gain) if max_gain is not None else g
        else:
            inv = 0.0
        key = f"{inv:+.4f}%".replace("+-", "-")
        agg[key] += 1
    return [{"درصد_دقیق": k, "تعداد_معاملات": v} for k, v in sorted(agg.items())]

# ══════════════════════════════════════════════════════════════
# آمار توزیع سود / اوت‌لایر / ماهانه (سنگین) — با کش در cache/
# ══════════════════════════════════════════════════════════════

STATS_CACHE_DIR = "cache"

# باگ ۱۵ (اصلی): compute_monthly_stats همیشه صفر برمی‌گشت چون _trade_raw_time
# فقط ۳ نام کلید دقیق (closeTime/openTime/time) را چک می‌کرد. اگر داده واقعی
# trades از یکی از این نام‌ها استفاده نکند (مثلاً close_time، exitTime، date،
# timestamp و ...)، مقدار همیشه 0 برمی‌گردد → dt=None برای همه معاملات →
# months خالی می‌ماند → کل ستون‌های ماهانه/بازه بکتست صفر می‌شوند، دقیقاً
# همان الگویی که در خروجی جدول مقایسه دیده می‌شود (تمام ردیف‌ها، صرف‌نظر از
# تعداد معاملات). این تابع را طوری اصلاح می‌کنیم که هم نام‌های بیشتری از
# کلید زمان را بشناسد و هم اگر باز هم چیزی پیدا نشد، این را با صدای بلند در
# لاگ گزارش کند (نه سکوت) تا مشکل قابل ردیابی باشد.

_TIME_FIELD_CANDIDATES = [
    "closeTime", "close_time", "closedAt", "closed_at", "closeDate", "close_date",
    "exitTime", "exit_time", "exitDate", "exit_date",
    "openTime", "open_time", "openedAt", "opened_at", "openDate", "open_date",
    "entryTime", "entry_time", "entryDate", "entry_date",
    "time", "timestamp", "date", "datetime",
]


def _trade_raw_time(t):
    for key in _TIME_FIELD_CANDIDATES:
        v = t.get(key)
        if v:
            return v
    # جستجوی عمومی: هر کلیدی که شامل "time" یا "date" باشد (case-insensitive)
    for k, v in t.items():
        if v and isinstance(k, str) and ("time" in k.lower() or "date" in k.lower()):
            return v
    return 0


def _trade_datetime(t):
    """تبدیل زمان معامله (epoch ms/s یا رشته تاریخ) به datetime."""
    v = _trade_raw_time(t)
    if not v:
        return None
    try:
        if isinstance(v, (int, float)):
            ts = float(v)
            if ts > 1e12:  # میلی‌ثانیه
                ts /= 1000.0
            return datetime.fromtimestamp(ts)
        s = str(v).strip()
        # رشته‌ای که فقط عدد است (اپاک به صورت string)
        if re.fullmatch(r"\d+(\.\d+)?", s):
            ts = float(s)
            if ts > 1e12:
                ts /= 1000.0
            return datetime.fromtimestamp(ts)
        for fmt in ("%Y-%m-%dT%H:%M:%S.%fZ", "%Y-%m-%dT%H:%M:%SZ",
                    "%Y-%m-%d %H:%M:%S", "%Y-%m-%d",
                    "%Y/%m/%d %H:%M:%S", "%Y/%m/%d", "%d-%m-%Y", "%d/%m/%Y"):
            try:
                return datetime.strptime(s, fmt)
            except Exception:
                continue
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except Exception:
        return None


def _diagnose_missing_dates(row_key, trades):
    """
    اجباراً بعد از هر بار محاسبه آمار سنگین صدا زده می‌شود (از داخل
    compute_extended_stats که خودش تابعی است که برای هر symbol/combo در
    process_file همیشه صدا زده می‌شود). اگر trades غیرخالی باشد ولی هیچ
    تاریخی از هیچ معامله‌ای قابل استخراج نباشد، این را با جزئیات کامل در
    لاگ ثبت می‌کند تا مشکل دیگر «بی‌صدا» نماند.
    """
    if not trades:
        return
    sample = trades[0]
    found_any = any(_trade_datetime(t) is not None for t in trades)
    if not found_any:
        keys = list(sample.keys()) if isinstance(sample, dict) else []
        raw = _trade_raw_time(sample)
        log.warning(
            "⚠️ تاریخ معامله برای هیچ trade ای در «%s» قابل پارس نشد (تعداد=%d) → "
            "همه ستون‌های ماهانه/بازه‌بکتست صفر می‌مانند. کلیدهای نمونه معامله اول: %s | "
            "مقدار خام زمان استخراج‌شده: %r",
            row_key, len(trades), keys, raw,
        )



def _stats_cache_path(row_key):
    safe = re.sub(r'[^a-zA-Z0-9_.-]', '_', row_key)[:150]
    return os.path.join(STATS_CACHE_DIR, f"{safe}.json")


def _stats_cache_key(trades):
    n = len(trades)
    total = sum(safe_percentage(t.get("profitPercent", 0)) for t in trades)
    first_t = _trade_raw_time(trades[0]) if trades else 0
    last_t = _trade_raw_time(trades[-1]) if trades else 0
    raw = f"{n}|{total:.6f}|{first_t}|{last_t}"
    return hashlib.md5(raw.encode("utf-8")).hexdigest()


def _load_stats_cache(row_key, cache_key):
    path = _stats_cache_path(row_key)
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if data.get("_cache_key") == cache_key:
            return data.get("stats")
    except Exception:
        pass
    return None


def _save_stats_cache(row_key, cache_key, stats):
    os.makedirs(STATS_CACHE_DIR, exist_ok=True)
    path = _stats_cache_path(row_key)
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"_cache_key": cache_key, "stats": stats}, f, ensure_ascii=False)
    except Exception as e:
        log.warning("خطا در ذخیره کش آمار برای %s: %s", row_key, e)


def compute_distribution_stats(profits):
    """دسته اول: توزیع سود معاملات."""
    n = len(profits)
    if not n:
        return {"pct_gt0": 0.0, "pct_gt1": 0.0, "pct_gt5": 0.0, "pct_loss_gt5": 0.0}
    gt0 = sum(1 for p in profits if p > 0)
    gt1 = sum(1 for p in profits if p > 1)
    gt5 = sum(1 for p in profits if p > 5)
    loss_gt5 = sum(1 for p in profits if p < -5)
    return {
        "pct_gt0": gt0 / n * 100,
        "pct_gt1": gt1 / n * 100,
        "pct_gt5": gt5 / n * 100,
        "pct_loss_gt5": loss_gt5 / n * 100,
    }


def compute_outlier_stats(profits):
    """دسته دوم: معاملات خاص (اوت‌لایر بر اساس ۳ برابر IQR)."""
    n = len(profits)
    if not n:
        return {"max_win": 0.0, "max_loss": 0.0, "stdev_profit": 0.0,
                "outlier_count": 0, "outlier_sum": 0.0, "outlier_pct": 0.0}
    max_win = max(profits)
    max_loss = min(profits)
    stdev = statistics.stdev(profits) if n >= 2 else 0.0
    sp = sorted(profits)
    try:
        q1 = statistics.quantiles(sp, n=4)[0]
        q3 = statistics.quantiles(sp, n=4)[2]
    except Exception:
        q1 = q3 = sp[0]
    iqr = q3 - q1
    lower = q1 - 3 * iqr
    upper = q3 + 3 * iqr
    outliers = [p for p in profits if p < lower or p > upper]
    total_profit = sum(profits)
    outlier_sum = sum(outliers)
    outlier_pct = (outlier_sum / total_profit * 100) if total_profit else 0.0
    return {
        "max_win": max_win, "max_loss": max_loss, "stdev_profit": stdev,
        "outlier_count": len(outliers), "outlier_sum": outlier_sum,
        "outlier_pct": outlier_pct,
    }


def compute_concentration_stats(profits):
    """
    میزان وابستگی بازده کل به یک (یا چند) معامله‌ی شانسیِ خیلی بزرگ.
    برخلاف outlier_pct (که بر اساس ۳×IQR فیلتر می‌شود و روی داده‌های
    محدودشده با stop-loss/take-profit عملاً هیچ‌وقت چیزی پیدا نمی‌کند)،
    این معیار مستقیماً می‌گوید: «اگر بهترین معامله را حذف کنیم، چند درصد
    از بازده کل از بین می‌رود؟» — دقیقاً همان چیزی که برای فیلتر کردن
    استراتژی‌هایی که فقط با یک معامله‌ی ۱۰۰۰٪ سود «الکی» بالا آمده‌اند لازم است.
    """
    n = len(profits)
    if not n:
        return {"best_trade_pct_of_total": 0.0, "top3_trades_pct_of_total": 0.0}
    total = sum(profits)
    if total <= 0:
        # وقتی جمع کل منفی/صفر است، نسبت مشارکت معنای گمراه‌کننده پیدا می‌کند؛
        # صفر برمی‌گردانیم چون در این حالت اصلاً «سود الکی» موضوعیت ندارد.
        return {"best_trade_pct_of_total": 0.0, "top3_trades_pct_of_total": 0.0}
    best = max(profits)
    top3 = sum(sorted(profits, reverse=True)[:3])
    return {
        "best_trade_pct_of_total": (best / total) * 100,
        "top3_trades_pct_of_total": (top3 / total) * 100,
    }


def compute_monthly_stats(trades):
    """دسته سوم و چهارم: شاخص‌های زمانی (ماهانه) و ریسک/پایداری."""
    months = defaultdict(list)
    for t in trades:
        dt = _trade_datetime(t)
        if dt is None:
            continue
        key = f"{dt.year:04d}-{dt.month:02d}"
        months[key].append(safe_percentage(t.get("profitPercent", 0)))

    empty = {
        "months_no_trade": 0, "total_span_months": 0,
        "avg_monthly_return": 0.0, "stdev_monthly_return": 0.0,
        "best_month": 0.0, "worst_month": 0.0, "pct_profitable_months": 0.0,
        "avg_profit_in_profitable_months": 0.0, "avg_loss_in_loss_months": 0.0,
        "max_consecutive_monthly_loss": 0, "avg_trades_per_month": 0.0,
        "stdev_trades_per_month": 0.0,
        "sharpe_monthly": 0.0, "sortino": 0.0, "mdd_pct": 0.0,
        "recovery_months": 0, "calmar": 0.0,
    }
    if not months:
        return empty

    ordered_keys = sorted(months.keys())
    monthly_returns = [sum(months[k]) for k in ordered_keys]
    trades_per_month = [len(months[k]) for k in ordered_keys]
    n_months = len(ordered_keys)

    avg_ret = statistics.mean(monthly_returns)
    std_ret = statistics.stdev(monthly_returns) if n_months >= 2 else 0.0
    best = max(monthly_returns)
    worst = min(monthly_returns)
    profitable = [r for r in monthly_returns if r > 0]
    losing = [r for r in monthly_returns if r < 0]
    pct_profitable = (len(profitable) / n_months * 100) if n_months else 0.0
    avg_profit_months = statistics.mean(profitable) if profitable else 0.0
    avg_loss_months = statistics.mean(losing) if losing else 0.0

    best_streak = cur = 0
    for r in monthly_returns:
        if r < 0:
            cur += 1
            best_streak = max(best_streak, cur)
        else:
            cur = 0

    avg_trades = statistics.mean(trades_per_month) if trades_per_month else 0.0
    std_trades = statistics.stdev(trades_per_month) if len(trades_per_month) >= 2 else 0.0

    try:
        first_dt = datetime.strptime(ordered_keys[0] + "-01", "%Y-%m-%d")
        last_dt = datetime.strptime(ordered_keys[-1] + "-01", "%Y-%m-%d")
        total_span_months = (last_dt.year - first_dt.year) * 12 + (last_dt.month - first_dt.month) + 1
        months_no_trade = max(0, total_span_months - n_months)
    except Exception:
        total_span_months = n_months
        months_no_trade = 0

    sharpe_m = (avg_ret / std_ret) if std_ret else 0.0

    downside = [r for r in monthly_returns if r < 0]
    downside_dev = (statistics.stdev(downside) if len(downside) >= 2
                     else (abs(downside[0]) if downside else 0.0))
    sortino = (avg_ret / downside_dev) if downside_dev else 0.0

    cum = 0.0
    peak = 0.0
    mdd = 0.0
    cum_series = []
    for r in monthly_returns:
        cum += r
        cum_series.append(cum)
        if cum > peak:
            peak = cum
        dd = peak - cum
        if dd > mdd:
            mdd = dd

    recovery_months = 0
    if mdd > 0:
        cur_peak = cum_series[0]
        cur_peak_i = 0
        worst_dd = 0.0
        worst_peak_i = 0
        worst_trough_i = 0
        for i, v in enumerate(cum_series):
            if v > cur_peak:
                cur_peak = v
                cur_peak_i = i
            dd = cur_peak - v
            if dd > worst_dd:
                worst_dd = dd
                worst_peak_i = cur_peak_i
                worst_trough_i = i
        target = cum_series[worst_peak_i]
        rec = None
        for i in range(worst_trough_i + 1, len(cum_series)):
            if cum_series[i] >= target:
                rec = i - worst_trough_i
                break
        recovery_months = rec if rec is not None else 0

    calmar = (avg_ret * 12 / mdd) if mdd else 0.0

    return {
        "months_no_trade": months_no_trade,
        "total_span_months": total_span_months,
        "avg_monthly_return": avg_ret,
        "stdev_monthly_return": std_ret,
        "best_month": best,
        "worst_month": worst,
        "pct_profitable_months": pct_profitable,
        "avg_profit_in_profitable_months": avg_profit_months,
        "avg_loss_in_loss_months": avg_loss_months,
        "max_consecutive_monthly_loss": best_streak,
        "avg_trades_per_month": avg_trades,
        "stdev_trades_per_month": std_trades,
        "sharpe_monthly": sharpe_m,
        "sortino": sortino,
        "mdd_pct": -mdd,
        "recovery_months": recovery_months,
        "calmar": calmar,
    }


def _empty_ext_stats():
    d = {}
    d.update(compute_distribution_stats([]))
    d.update(compute_outlier_stats([]))
    d.update(compute_concentration_stats([]))
    d.update(compute_monthly_stats([]))
    return d


# ترتیب کلیدهای آمار جدید (برای نوشتن یکنواخت در CSVها)
EXT_STATS_FIELDS = [
    "pct_gt0", "pct_gt1", "pct_gt5", "pct_loss_gt5",
    "max_win", "max_loss", "stdev_profit", "outlier_count", "outlier_sum", "outlier_pct",
    "best_trade_pct_of_total", "top3_trades_pct_of_total",
    "total_span_months", "months_no_trade", "avg_monthly_return", "stdev_monthly_return", "best_month", "worst_month",
    "pct_profitable_months", "avg_profit_in_profitable_months", "avg_loss_in_loss_months",
    "max_consecutive_monthly_loss", "avg_trades_per_month", "stdev_trades_per_month",
    "sharpe_monthly", "sortino", "mdd_pct", "recovery_months", "calmar",
]


# نگاشت هر فیلد آماری جدید به عنوان ستون CSV و فرمت نمایش آن
EXT_STATS_HEADERS = {
    "pct_gt0": "درصد سود > ۰%",
    "pct_gt1": "درصد سود > ۱%",
    "pct_gt5": "درصد سود > ۵%",
    "pct_loss_gt5": "درصد ضرر > ۵%",
    "max_win": "بیشترین سود یک معامله(%)",
    "max_loss": "بیشترین ضرر یک معامله(%)",
    "stdev_profit": "انحراف معیار سود",
    "outlier_count": "تعداد اوت‌لایرها",
    "outlier_sum": "مجموع سود اوت‌لایرها(%)",
    "outlier_pct": "درصد سود از اوت‌لایرها",
    "best_trade_pct_of_total": "سهم بهترین معامله از بازده کل(%)",
    "top3_trades_pct_of_total": "سهم ۳ معامله برتر از بازده کل(%)",
    "total_span_months": "بازه بکتست (ماه)",
    "months_no_trade": "ماه‌های بدون معامله",
    "avg_monthly_return": "میانگین سود ماهانه",
    "stdev_monthly_return": "انحراف معیار سود ماهانه",
    "best_month": "بهترین ماه(%)",
    "worst_month": "بدترین ماه(%)",
    "pct_profitable_months": "درصد ماه‌های سودده",
    "avg_profit_in_profitable_months": "میانگین سود در ماه‌های سودده",
    "avg_loss_in_loss_months": "میانگین ضرر در ماه‌های ضررده",
    "max_consecutive_monthly_loss": "بیشترین ضرر متوالی ماهانه",
    "avg_trades_per_month": "تعداد معاملات در ماه (میانگین)",
    "stdev_trades_per_month": "انحراف معیار تعداد معاملات",
    "sharpe_monthly": "نسبت شارپ (ماهانه)",
    "sortino": "نسبت سورتینو",
    "mdd_pct": "حداکثر افت سرمایه(%)",
    "recovery_months": "مدت بازگشت از افت (ماه)",
    "calmar": "نسبت کالمار",
}

# فرمت نمایش هر فیلد (None یعنی عدد صحیح، بدون فرمت اعشاری)
EXT_STATS_FORMATS = {
    "pct_gt0": "{:.2f}", "pct_gt1": "{:.2f}", "pct_gt5": "{:.2f}", "pct_loss_gt5": "{:.2f}",
    "max_win": "{:.4f}", "max_loss": "{:.4f}", "stdev_profit": "{:.4f}",
    "outlier_count": None, "outlier_sum": "{:.4f}", "outlier_pct": "{:.2f}",
    "best_trade_pct_of_total": "{:.2f}", "top3_trades_pct_of_total": "{:.2f}",
    "total_span_months": None,
    "months_no_trade": None, "avg_monthly_return": "{:.4f}", "stdev_monthly_return": "{:.4f}",
    "best_month": "{:.4f}", "worst_month": "{:.4f}", "pct_profitable_months": "{:.2f}",
    "avg_profit_in_profitable_months": "{:.4f}", "avg_loss_in_loss_months": "{:.4f}",
    "max_consecutive_monthly_loss": None, "avg_trades_per_month": "{:.2f}",
    "stdev_trades_per_month": "{:.2f}",
    "sharpe_monthly": "{:.3f}", "sortino": "{:.3f}", "mdd_pct": "{:.2f}",
    "recovery_months": None, "calmar": "{:.3f}",
}

# ترتیب ستون‌های جدید مطابق نمونه هدر جدول_مقایسه_همه_*.csv (ماه‌های بدون معامله در انتها)
COMPARISON_EXT_FIELDS = [
    "total_span_months",
    "pct_gt0", "pct_gt1", "pct_gt5", "pct_loss_gt5",
    "max_win", "max_loss", "stdev_profit", "outlier_count", "outlier_sum", "outlier_pct",
    "best_trade_pct_of_total", "top3_trades_pct_of_total",
    "avg_monthly_return", "stdev_monthly_return", "best_month", "worst_month",
    "pct_profitable_months", "avg_profit_in_profitable_months", "avg_loss_in_loss_months",
    "max_consecutive_monthly_loss", "avg_trades_per_month", "stdev_trades_per_month",
    "sharpe_monthly", "sortino", "mdd_pct", "recovery_months", "calmar",
    "months_no_trade",
]


def _ext_headers(fields):
    return [EXT_STATS_HEADERS[f] for f in fields]


def _ext_row(record_or_agg, fields):
    """ساخت لیست مقادیر فرمت‌شده برای ستون‌های آماری جدید از یک دیکشنری (رکورد یا میانگین تجمیع‌شده)."""
    out = []
    for f in fields:
        v = record_or_agg.get(f, 0) or 0
        fmt = EXT_STATS_FORMATS.get(f)
        out.append(fmt.format(v) if fmt else v)
    return out


def compute_extended_stats(row_key, trades):
    """
    محاسبه سنگین آمار توزیع/اوت‌لایر/ماهانه با کش در cache/.
    row_key باید شناسه یکتای ردیف (مثلاً enc_path::symbol یا folder_combo) باشد.
    """
    if not trades:
        return _empty_ext_stats()

    cache_key = _stats_cache_key(trades)
    cached = _load_stats_cache(row_key, cache_key)
    if cached is not None:
        # حتی برای نتیجه کش‌شده هم تشخیص را اجرا می‌کنیم: اگر کش قبلاً با
        # همین باگ ساخته شده و صفر است، دیگر بی‌صدا رد نمی‌شود.
        if not cached.get("total_span_months"):
            _diagnose_missing_dates(row_key, trades)
        return cached

    profits = [safe_percentage(t.get("profitPercent", 0)) for t in trades]
    stats = {}
    stats.update(compute_distribution_stats(profits))
    stats.update(compute_outlier_stats(profits))
    stats.update(compute_concentration_stats(profits))
    stats.update(compute_monthly_stats(trades))

    # اجباری: این تابع همیشه بلافاصله بعد از compute_monthly_stats صدا زده
    # می‌شود (که خودش همیشه از داخل همین تابع فراخوانی‌شده اجرا می‌گردد) تا
    # اگر نتیجه صفر بود، در همین اجرا در لاگ مشخص شود و دیگر نیازی به حدس
    # زدن یا اجرای مجدد نباشد.
    if not stats.get("total_span_months"):
        _diagnose_missing_dates(row_key, trades)

    _save_stats_cache(row_key, cache_key, stats)
    return stats


# ══════════════════════════════════════════════════════════════
# پردازش یک فایل یکپارچه → همه خروجی‌ها به تفکیک symbol
# ══════════════════════════════════════════════════════════════

def process_file(enc_path, move_percents_override=None, stop_loss_override=None, password=None):
    """
    خروجی: {
        "_meta": {"move_percents": [...], "stop_loss": float, "max_move_percent": float|None},
        symbol: {
            "trades": [...],
            "real": float, "special": float,
            "max_cons_count": int, "max_cons_loss": float,
            "max_drawdown": float, "sharpe": float, "pl_ratio": float,
            "win": int, "loss": int, "total": int, "win_rate": float,
            "inv_dist": [...],
        }
    }
    """
    data = load_aggregated_data(enc_path, password)
    trades = data["trades"]
    meta = data["metadata"]

    mp = move_percents_override if move_percents_override is not None else (meta.get("move_percents") or [])
    sl = stop_loss_override if stop_loss_override is not None else meta.get("stop_loss_initial", -2.0)
    # باگ ۸: max_g باید از mp (که ممکن است override شده باشد) محاسبه شود، نه مستقیم از metadata
    max_g = max(mp) if mp else meta.get("max_move_percent")

    if not trades:
        log.warning("%s: هیچ معامله‌ای وجود ندارد.", enc_path)
        return {"_meta": {"move_percents": mp, "stop_loss": sl, "max_move_percent": max_g}}

    groups = group_by_symbol(trades)
    log.info("%s: %d symbol یافت شد: %s", enc_path, len(groups), list(groups.keys()))

    result = {"_meta": {"move_percents": mp, "stop_loss": sl, "max_move_percent": max_g}}
    for sym, sym_trades in groups.items():
        wins = sum(1 for t in sym_trades if safe_percentage(t.get("profitPercent", 0)) > 0)
        losses = sum(1 for t in sym_trades if safe_percentage(t.get("profitPercent", 0)) < 0)
        wr = (wins / len(sym_trades) * 100) if sym_trades else 0.0
        mc = max_consecutive_count(sym_trades)
        r = {
            "trades": sym_trades,
            "real":            real_return(sym_trades),
            "special":         special_return(sym_trades, mp),
            "max_cons_count":  mc,
            "max_cons_loss":   consecutive_loss_value(mc, sl),
            "max_drawdown":    max_drawdown(sym_trades),
            "sharpe":          sharpe_ratio(sym_trades),
            "pl_ratio":        profit_loss_ratio(sym_trades),
            "win":   wins,
            "loss":  losses,
            "total": len(sym_trades),
            "win_rate": wr,
            "ext": compute_extended_stats(f"{enc_path}::{sym}", sym_trades),
        }
        log.info("  symbol=%s | total=%d | win_rate=%.1f%% | real=%.4f | special=%.4f | cons=%d",
                 sym, r["total"], r["win_rate"], r["real"], r["special"], r["max_cons_count"])
        result[sym] = r

    # ترکیب‌های چندتایی
    all_syms = list(groups.keys())
    for combo in get_symbol_combinations(all_syms):
        if len(combo) == 1:
            continue  # تک‌نمادها قبلاً ثبت شدند
        label = combo_label(combo)
        merged = []
        for s in combo:
            merged.extend(groups[s])
        # باگ ۹: مرتب‌سازی بر اساس زمان معامله برای صحت max_consecutive و max_drawdown
        # (قبلاً این خط مستقیماً closeTime/openTime/time را چک می‌کرد که در داده
        # واقعی وجود ندارند [فیلدهای واقعی entryTime/exitTime هستند] و در نتیجه
        # همیشه با مقدار 0 مرتب می‌شد؛ حالا از همان تشخیص چندنامی + پارس واقعی
        # تاریخ استفاده می‌شود تا مقادیر رشته‌ای/عددی قاطی نشوند)
        merged.sort(key=lambda t: _trade_datetime(t) or datetime.min)
        wins = sum(1 for t in merged if safe_percentage(t.get("profitPercent", 0)) > 0)
        losses = sum(1 for t in merged if safe_percentage(t.get("profitPercent", 0)) < 0)
        wr = (wins / len(merged) * 100) if merged else 0.0
        mc = max_consecutive_count(merged)
        r = {
            "trades": merged,
            "real":            real_return(merged),
            "special":         special_return(merged, mp),
            "max_cons_count":  mc,
            "max_cons_loss":   consecutive_loss_value(mc, sl),
            "max_drawdown":    max_drawdown(merged),
            "sharpe":          sharpe_ratio(merged),
            "pl_ratio":        profit_loss_ratio(merged),
            "win":   wins,
            "loss":  losses,
            "total": len(merged),
            "win_rate": wr,
            "ext": compute_extended_stats(f"{enc_path}::{label}", merged),
        }
        log.info("  combo=%s | total=%d | win_rate=%.1f%% | real=%.4f | special=%.4f",
                 label, r["total"], r["win_rate"], r["real"], r["special"])
        result[label] = r
    return result

# ══════════════════════════════════════════════════════════════
# subcommand: returns
# ══════════════════════════════════════════════════════════════

def cmd_returns(args):
    password = args.password or os.environ.get("RESULTS_PASSWORD", os.environ.get("DATA_PASSWORD", ""))
    with open(args.strategies_json, "r", encoding="utf-8") as f:
        strategies = json.load(f)["strategies"]

    cache = {}
    for s in strategies:
        folder = s["folder"]
        mp = s.get("move_percents") or []
        enc = s.get("aggregated_file") or os.path.join(args.results_base, folder, f"{folder}_trades.enc")
        if not os.path.exists(enc):
            log.warning("استراتژی %s: فایل یافت نشد: %s", folder, enc)
            continue
        log.info("returns ← استراتژی: %s", folder)
        try:
            per_sym = process_file(enc, mp, password=password)
            for sym, r in per_sym.items():
                if sym == "_meta":
                    continue
                key = f"{folder}_{sym}"
                cache[key] = {"real": r["real"], "special": r["special"]}
                log.info("  کش: %s → real=%.4f special=%.4f", key, r["real"], r["special"])
        except Exception as e:
            log.error("خطا استراتژی %s: %s", folder, e, exc_info=True)

    _write_json(args.output, cache)
    log.info("✅ returns_cache: %d ترکیب → %s", len(cache), args.output)

# ══════════════════════════════════════════════════════════════
# subcommand: risk
# ══════════════════════════════════════════════════════════════

def cmd_risk(args):
    password = args.password or os.environ.get("RESULTS_PASSWORD", os.environ.get("DATA_PASSWORD", ""))
    with open(args.strategies_json, "r", encoding="utf-8") as f:
        strategies = json.load(f)["strategies"]
    with open(args.stoploss_cache, "r", encoding="utf-8") as f:
        sl_cache = json.load(f)

    cache = {}
    for s in strategies:
        folder = s["folder"]
        sl = sl_cache.get(folder, {}).get("stop_loss", -2.0)
        enc = s.get("aggregated_file") or os.path.join(args.results_base, folder, f"{folder}_trades.enc")
        if not os.path.exists(enc):
            log.warning("استراتژی %s: فایل یافت نشد: %s", folder, enc)
            continue
        log.info("risk ← استراتژی: %s  sl=%.2f", folder, sl)
        try:
            per_sym = process_file(enc, stop_loss_override=sl, password=password)
            for sym, r in per_sym.items():
                if sym == "_meta":
                    continue
                key = f"{folder}_{sym}"
                cache[key] = {"max_consecutive_loss": r["max_cons_loss"], "max_consecutive_count": r["max_cons_count"]}
                log.info("  کش: %s → count=%d loss=%.4f", key, r["max_cons_count"], r["max_cons_loss"])
        except Exception as e:
            log.error("خطا استراتژی %s: %s", folder, e, exc_info=True)

    _write_json(args.output, cache)
    log.info("✅ risk_cache: %d ترکیب → %s", len(cache), args.output)

# ══════════════════════════════════════════════════════════════
# subcommand: inverse
# ══════════════════════════════════════════════════════════════

def cmd_inverse(args):
    # استراتژی معکوس کاملاً از پایپ‌لاین حذف شده است (نه فقط از جدول مقایسه).
    # این subcommand همچنان توسط چند فایل yml صدا زده می‌شود، بنابراین بدون
    # تغییر yml، اینجا فقط یک کش خالی و معتبر می‌نویسیم تا این step بی‌اثر
    # و در کمتر از ۱ ثانیه تمام شود (بدون decrypt/process روی هیچ فایلی).
    _write_json(args.output, {})
    log.info("✅ inverse_cache: غیرفعال (استراتژی معکوس حذف شد) → %s", args.output)

# ══════════════════════════════════════════════════════════════
# subcommand: report
# ══════════════════════════════════════════════════════════════

def cmd_report(args):
    password = args.password or os.environ.get("RESULTS_PASSWORD", os.environ.get("DATA_PASSWORD", ""))

    with open(args.returns_cache, "r", encoding="utf-8") as f:
        returns_cache = json.load(f)
    with open(args.strategies_json, "r", encoding="utf-8") as f:
        strategies = json.load(f)["strategies"]
    with open(args.risk_cache, "r", encoding="utf-8") as f:
        risk_cache = json.load(f)
    inv_cache = {}
    if args.inverse_cache and os.path.exists(args.inverse_cache):
        with open(args.inverse_cache, "r", encoding="utf-8") as f:
            inv_cache = json.load(f)
    news_events = []
    if args.news_pickle and os.path.exists(args.news_pickle):
        with open(args.news_pickle, "rb") as f:
            news_events = pickle.load(f)

    all_results = _build_all_results(strategies, returns_cache, risk_cache, inv_cache, args, password)
    log.info("✅ all_results: %d رکورد (اصلی + معکوس)", len(all_results))

    os.makedirs(args.output_dir, exist_ok=True)

    # 1. خلاصه هر پوشه (به ازای هر استراتژی پایه، همه ترکیب‌ها در یک فایل)
    base_strategy_map = defaultdict(list)
    for r in all_results:
        if not r.get("is_inverse"):
            # استراتژی پایه = period_name که همان folder اصلی است
            base_strategy_map[r["period_name"]].append(r)
    for base_folder, recs in base_strategy_map.items():
        fp = os.path.join(args.output_dir, base_folder)
        os.makedirs(fp, exist_ok=True)
        _folder_summary(base_folder, fp, recs)

    # 2. گزارش کلی
    _global_report(all_results, args.output_dir)

    # 3. جداول مقایسه
    _comparison_tables(all_results, args.output_dir)

    # 4. گزارش ماهانه
    _monthly_report(all_results, args.output_dir)

    # 5. تحلیل تکمیلی
    if news_events:
        _complementary(all_results, news_events, args.output_dir)

    log.info("✅ همه گزارش‌ها تولید شدند.")


def _build_all_results(strategies, returns_cache, risk_cache, inv_cache, args, password):
    all_results = []
    results_base = getattr(args, "results_base", "") or ""

    for s in strategies:
        folder = s["folder"]
        group = s.get("group", "aggregated")
        mp = s.get("move_percents") or []
        sl = s.get("stop_loss_initial", -2.0)
        max_move = s.get("max_move_percent")
        enc = s.get("aggregated_file") or (
            os.path.join(results_base, folder, f"{folder}_trades.enc") if results_base else ""
        )

        log.info("report ← استراتژی: %s", folder)

        # بارگذاری trades
        trades_by_sym = {}
        if enc and os.path.exists(enc):
            try:
                per_sym = process_file(enc, mp, sl, password=password)
                meta_from_file = per_sym.pop("_meta", {})
                trades_by_sym = per_sym
                # به‌روزرسانی mp/sl از metadata فایل اگر خالی بود (بدون decrypt مجدد)
                if not mp:
                    mp = meta_from_file.get("move_percents") or []
                if sl == -2.0:
                    sl = meta_from_file.get("stop_loss", -2.0)
                if max_move is None:
                    max_move = meta_from_file.get("max_move_percent")
            except Exception as e:
                log.error("خطا بارگذاری %s برای report: %s", enc, e, exc_info=True)
        else:
            log.warning("استراتژی %s: فایل %s موجود نیست – فقط کش.", folder, enc)

        # symbols از trades یا کش — پویا برای هر تعداد کوین
        if trades_by_sym:
            # process_file هم تک‌نمادها هم ترکیب‌ها را مستقیم ذخیره می‌کند
            combos_in_file = list(trades_by_sym.keys())
        else:
            # از کش: پیدا کردن همه کلیدهایی که با folder_ شروع می‌شوند
            combos_in_file = [k[len(folder)+1:] for k in returns_cache if k.startswith(f"{folder}_")]
        if not combos_in_file:
            combos_in_file = ["unknown"]

        for combo_lbl in combos_in_file:
            ck = f"{folder}_{combo_lbl}"
            rd = trades_by_sym.get(combo_lbl)

            if rd:
                total, wins, losses = rd["total"], rd["win"], rd["loss"]
                wr = rd["win_rate"]
                real, spec = rd["real"], rd["special"]
                mc_count, mc_loss = rd["max_cons_count"], rd["max_cons_loss"]
                dd, sh, pl = rd["max_drawdown"], rd["sharpe"], rd["pl_ratio"]
                ext = rd.get("ext") or _empty_ext_stats()
            else:
                rc = returns_cache.get(ck, {"real": 0, "special": 0})
                rk = risk_cache.get(ck, {"max_consecutive_loss": 0, "max_consecutive_count": 0})
                total = wins = losses = 0
                wr = dd = sh = pl = 0.0
                real, spec = rc["real"], rc["special"]
                mc_count = rk["max_consecutive_count"]
                mc_loss = rk["max_consecutive_loss"]
                ext = _empty_ext_stats()

            log.info("  ✓ %s | trades=%d | wr=%.1f%% | real=%.4f | special=%.4f",
                     ck, total, wr, real, spec)

            # وقتی rd موجود نیست (فقط کش قدیمی در دسترس است)، مقادیر ext صرفاً
            # پیش‌فرض صفر هستند نه داده‌ی واقعی؛ برای امتیازدهی این‌ها را None
            # می‌فرستیم تا به‌جای جریمه‌ی نادرست، نمره‌ی خنثی بگیرند.
            span_months = ext.get("total_span_months") or 0
            main_score, main_rating = _score(
                1 if spec >= 1 else 0,
                1 if spec > 0 else 0,
                mc_loss if mc_loss is not None else 0,
                1,
                win_rate=wr if rd else None,
                idle_ratio=((ext.get("months_no_trade", 0) or 0) / span_months) if (rd and span_months) else None,
                avg_trades_per_month=ext.get("avg_trades_per_month") if rd else None,
                best_trade_pct=ext.get("best_trade_pct_of_total") if rd else None,
            )

            main_rec = {
                "folder_name": f"{folder}_{combo_lbl}", "group": group,
                "file_name": f"{folder}_{combo_lbl}", "period_name": folder, "symbol": combo_lbl,
                "total_trades": total, "win_trades": wins, "loss_trades": losses,
                "win_rate": wr, "max_consecutive_loss": mc_loss, "max_consecutive_count": mc_count,
                "max_drawdown": dd, "profit_loss_ratio": pl, "sharpe_ratio": sh,
                "real_return": real, "special_rounded_return": spec,
                "is_inverse": False,
                "score": main_score, "rating": main_rating,
                "_move_percents": mp, "_stop_loss": sl, "_max_move": max_move,
            }
            main_rec.update(ext)
            all_results.append(main_rec)

    # فقط ۵۰۰ استراتژی برتر بر اساس امتیاز در خروجی نهایی نگهداری شوند
    all_results.sort(key=lambda r: r["score"], reverse=True)
    if len(all_results) > 500:
        log.info("✂️ اعمال محدودیت ۵۰۰ استراتژی برتر (از %d رکورد) بر اساس امتیاز", len(all_results))
        all_results = all_results[:500]

    return all_results

# ══════════════════════════════════════════════════════════════
# گزارش‌های CSV
# ══════════════════════════════════════════════════════════════

def _score(returns_ge_1, returns_gt_0, avg_max_loss, total_files,
           win_rate=None, idle_ratio=None, avg_trades_per_month=None,
           best_trade_pct=None):
    """
    امتیازدهی (۰ تا ۱۰۰) بر اساس ۴ محور + ۱ جریمه:
      ۱) عملکرد بازده                    حداکثر ۲۰ امتیاز
      ۲) ریسک ضرر متوالی                 حداکثر ۱۵ امتیاز
      ۳) وین‌ریت واقعی                   حداکثر ۳۰ امتیاز
      ۴) فعالیت معاملاتی نسبت به بازه     حداکثر ۳۵ امتیاز
      ۵) جریمه‌ی تمرکز سود روی معاملات شانسی   حداکثر ۱۵- امتیاز

    قبلاً فقط بند (۱) و (۲) وجود داشت، به همین دلیل استراتژی‌ای که در ۵۵ از
    ~۱۰۰ ماه اصلاً معامله نکرده و کل بازه فقط ۵۰ معامله با وین‌ریت ۵۰٪ داشته
    می‌توانست رتبه‌ی اول را بگیرد (چون بازده‌اش هرچند کوچک، مثبت بود و افت
    سرمایه‌ی خاصی هم نداشت). بندهای (۳) و (۴) دقیقاً همین حالت را جریمه
    می‌کنند. بند (۵) هم جلوی بالا آمدن الکیِ امتیاز به خاطر یک معامله‌ی
    خیلی شانسی (مثلاً ۱۰۰۰٪ سود در حالی که بقیه معاملات ضررده بوده‌اند) را
    می‌گیرد.
    """
    if not total_files:
        return 0, "بدون داده"

    # ۱) عملکرد بازده (حداکثر ۲۰) — وزن کم شده چون به‌تنهایی نباید کافی باشد
    perf = (returns_ge_1 / total_files) * 15 + (returns_gt_0 / total_files) * 5

    # ۲) ریسک ضرر متوالی (حداکثر ۱۵)
    la = abs(avg_max_loss)
    if la <= 5:
        risk = 15.0
    elif la <= 10:
        risk = 15 * (10 - la) / 5
    elif la <= 20:
        risk = 15 * (20 - la) / 10
    else:
        risk = 0.0

    # ۳) وین‌ریت واقعی (حداکثر ۳۰) — زیر ۳۵٪ عملاً صفر، از ۷۰٪ به بالا کامل
    if win_rate is None:
        wr_score = 15.0  # داده‌ای موجود نیست؛ نمره‌ی خنثی (نه تشویق نه جریمه)
    else:
        wr_score = max(0.0, min(1.0, (win_rate - 35) / 35)) * 30

    # ۴) فعالیت معاملاتی نسبت به بازه (حداکثر ۳۵)
    #    idle_ratio = ماه‌های بدون معامله / کل بازه بکتست → هرچه بیشتر، جریمه بیشتر
    #    avg_trades_per_month → چگالی واقعی معاملات (۵ معامله در ماه = چگالی سالم فرضی)
    if idle_ratio is None:
        idle_score = 10.0
    else:
        idle_score = (1 - min(max(idle_ratio, 0.0), 1.0)) * 20
    if avg_trades_per_month is None:
        density_score = 7.5
    else:
        density_score = min(max(avg_trades_per_month, 0.0) / 5.0, 1.0) * 15
    activity = idle_score + density_score

    s = perf + risk + wr_score + activity

    # ۵) جریمه‌ی تمرکز سود روی چند معامله‌ی شانسی (حداکثر ۱۵ امتیاز کسر)
    #    زیر ۳۰٪ مشارکت، جریمه‌ای اعمال نمی‌شود؛ در ۱۰۰٪ مشارکت، کل ۱۵ امتیاز کسر می‌شود.
    if best_trade_pct is not None and best_trade_pct > 30:
        s -= min((best_trade_pct - 30) / 70.0, 1.0) * 15

    s = round(max(0.0, min(100.0, s)), 1)
    rating = ("عالی ★★★★★" if s >= 80 else "خوب ★★★★" if s >= 60
              else "متوسط ★★★" if s >= 40 else "ضعیف ★★" if s >= 20 else "بسیار ضعیف ★")
    return s, rating


def _score_kwargs_from_records(records):
    """
    میانگین/تجمیع فیلدهای لازم برای امتیازدهی (وین‌ریت، فعالیت، تمرکز سود)
    از یک لیست از رکوردهای all_results — برای استفاده در گزارش ماهانه که
    چند رکورد (مثلاً ۳ فایل یک ماه) با هم امتیازدهی می‌شوند.
    """
    if not records:
        return {}
    wr_list = [r.get("win_rate", 0) or 0 for r in records]
    win_rate = statistics.mean(wr_list) if wr_list else None
    span_sum = sum(r.get("total_span_months", 0) or 0 for r in records)
    idle_sum = sum(r.get("months_no_trade", 0) or 0 for r in records)
    idle_ratio = (idle_sum / span_sum) if span_sum else None
    atpm_list = [r.get("avg_trades_per_month", 0) or 0 for r in records]
    avg_trades_per_month = statistics.mean(atpm_list) if atpm_list else None
    btp_list = [r.get("best_trade_pct_of_total", 0) or 0 for r in records]
    best_trade_pct = statistics.mean(btp_list) if btp_list else None
    return {
        "win_rate": win_rate, "idle_ratio": idle_ratio,
        "avg_trades_per_month": avg_trades_per_month, "best_trade_pct": best_trade_pct,
    }


def _folder_summary(folder_name, folder_path, recs):
    if not recs:
        return
    path = os.path.join(folder_path, "خلاصه_پوشه.csv")
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow([f"📊 خلاصه پوشه: {folder_name}"])
        w.writerow(["تاریخ تولید", datetime.now().strftime("%Y-%m-%d %H:%M:%S")])
        w.writerow([])
        w.writerow(["ردیف", "ترکیب کوین‌ها", "معاملات", "سودده", "زیانده", "وین‌ریت(%)",
                    "بازده واقعی(%)", "بازده ویژه(%)", "حداکثر ضرر متوالی(%)",
                    "تعداد ضررهای متوالی", "افت سرمایه(%)", "P/L ratio", "شارپ", "وضعیت"]
                   + _ext_headers(EXT_STATS_FIELDS))
        for i, r in enumerate(recs, 1):
            sp = r["special_rounded_return"]
            status = "✅ عالی (≥۱%)" if sp >= 1 else ("👍 مثبت" if sp > 0 else "⚠️ منفی")
            ml = f"{r['max_consecutive_loss']:.2f}" if r.get("max_consecutive_loss") is not None else "-"
            sh = f"{r['sharpe_ratio']:.3f}" if r.get("sharpe_ratio") is not None else "-"
            combo_display = r.get("symbol", r["folder_name"])
            w.writerow([i, combo_display, r["total_trades"], r["win_trades"], r["loss_trades"],
                        f"{r.get('win_rate', 0):.2f}", f"{r['real_return']:.4f}", f"{sp:.4f}",
                        ml, r.get("max_consecutive_count", 0),
                        f"{r.get('max_drawdown', 0):.2f}", f"{r.get('profit_loss_ratio', 0):.3f}",
                        sh, status]
                       + _ext_row(r, EXT_STATS_FIELDS))
        w.writerow([])
        pos = sum(1 for r in recs if r["special_rounded_return"] > 0)
        ge1 = sum(1 for r in recs if r["special_rounded_return"] >= 1)
        n = len(recs)
        avg_sp = statistics.mean([r["special_rounded_return"] for r in recs])
        w.writerow(["ترکیب‌های با بازدهی مثبت", f"{pos}/{n}"])
        w.writerow(["ترکیب‌های با بازدهی ≥۱٪", f"{ge1}/{n}"])
        w.writerow(["میانگین بازدهی ویژه", f"{avg_sp:.4f}%"])
    log.info("خلاصه پوشه %s → %s", folder_name, path)


def _global_report(all_results, out_dir):
    path = os.path.join(out_dir, "گزارش_کلی_نتایج.csv")
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["گروه", "پوشه", "symbol", "معاملات", "سودده", "زیانده", "وین‌ریت(%)",
                    "حداکثر ضرر متوالی(%)", "تعداد ضررهای متوالی", "افت سرمایه(%)",
                    "P/L ratio", "شارپ", "بازده واقعی(%)", "بازده ویژه(%)", "نوع"]
                   + _ext_headers(EXT_STATS_FIELDS))
        for r in all_results:
            ml = f"{r['max_consecutive_loss']:.2f}" if r.get("max_consecutive_loss") is not None else "-"
            sh = f"{r['sharpe_ratio']:.3f}" if r.get("sharpe_ratio") is not None else "-"
            w.writerow([r["group"], r["folder_name"], r.get("symbol", ""),
                        r["total_trades"], r["win_trades"], r["loss_trades"],
                        f"{r['win_rate']:.2f}", ml, r.get("max_consecutive_count", 0),
                        f"{r.get('max_drawdown', 0):.2f}", f"{r.get('profit_loss_ratio', 0):.3f}",
                        sh, f"{r['real_return']:.4f}", f"{r['special_rounded_return']:.4f}",
                        "معکوس" if r["is_inverse"] else "اصلی"]
                       + _ext_row(r, EXT_STATS_FIELDS))
    log.info("گزارش کلی → %s", path)


# قوانین تجمیع فیلدهای آماری جدید هنگام گروه‌بندی چند رکورد در یک ردیف جدول مقایسه
_EXT_AGG_MAX = {"max_win", "total_span_months"}
_EXT_AGG_MIN = {"max_loss"}
_EXT_AGG_SUM = {"outlier_count", "months_no_trade"}


def _aggregate_ext_field(field, values):
    if not values:
        return 0
    if field in _EXT_AGG_MAX:
        return max(values)
    if field in _EXT_AGG_MIN:
        return min(values)
    if field in _EXT_AGG_SUM:
        return sum(values)
    return statistics.mean(values)


def _comparison_tables(all_results, out_dir):
    agg = {}
    for r in all_results:
        # درخواست کاربر: استراتژی‌های معکوس در جدول مقایسه نمایش داده نشوند
        if r.get("is_inverse"):
            continue
        k = (r["folder_name"], r["group"])
        if k not in agg:
            agg[k] = {"folder": r["folder_name"], "group": r["group"],
                      "win_rates": [], "sharpes": [], "max_losses": [],
                      "real": [], "special": [], "total_trades": 0,
                      "ge1": 0, "gt0": 0, "n": 0,
                      "ext_lists": {f: [] for f in EXT_STATS_FIELDS}}
        a = agg[k]
        a["win_rates"].append(r["win_rate"])
        a["sharpes"].append(r.get("sharpe_ratio") or 0)
        a["max_losses"].append(r["max_consecutive_loss"] if r.get("max_consecutive_loss") is not None else 0)
        a["real"].append(r["real_return"])
        a["special"].append(r["special_rounded_return"])
        a["total_trades"] += r["total_trades"]
        a["ge1"] += 1 if r["special_rounded_return"] >= 1 else 0
        a["gt0"] += 1 if r["special_rounded_return"] > 0 else 0
        a["n"] += 1
        for f in EXT_STATS_FIELDS:
            a["ext_lists"][f].append(r.get(f, 0) or 0)

    rows = []
    for (folder, group), a in agg.items():
        n = a["n"]
        avg_special_chk = statistics.mean(a["special"]) if a["special"] else 0
        avg_win_chk = statistics.mean(a["win_rates"]) if a["win_rates"] else 0
        total_trades_chk = a["total_trades"]

        # قانون ۱: بازده منفی یا صفر داخل جدول نیاید
        if avg_special_chk <= 0:
            continue
        # قانون ۲: کمتر از ۵۰ معامله (در کل بازه) فقط با عملکرد استثنایی بیاید
        if total_trades_chk < 50 and not (avg_win_chk > 70 and avg_special_chk > 10):
            continue

        avg_loss = statistics.mean(a["max_losses"]) if a["max_losses"] else 0
        ext_agg = {f: _aggregate_ext_field(f, a["ext_lists"][f]) for f in EXT_STATS_FIELDS}
        span_sum = sum(a["ext_lists"]["total_span_months"])
        idle_sum = sum(a["ext_lists"]["months_no_trade"])
        idle_ratio = (idle_sum / span_sum) if span_sum else None
        avg_atpm = (statistics.mean(a["ext_lists"]["avg_trades_per_month"])
                    if a["ext_lists"]["avg_trades_per_month"] else None)
        avg_btp = (statistics.mean(a["ext_lists"]["best_trade_pct_of_total"])
                   if a["ext_lists"]["best_trade_pct_of_total"] else None)
        sc, rt = _score(a["ge1"], a["gt0"], avg_loss, n,
                         win_rate=avg_win_chk, idle_ratio=idle_ratio,
                         avg_trades_per_month=avg_atpm, best_trade_pct=avg_btp)
        row = {
            "folder": folder, "group": group,
            "n": n, "is_inv": folder.endswith("_INV"),
            "avg_win": statistics.mean(a["win_rates"]) if a["win_rates"] else 0,
            "avg_sh": statistics.mean(a["sharpes"]) if a["sharpes"] else 0,
            "avg_loss": avg_loss,
            "avg_real": statistics.mean(a["real"]) if a["real"] else 0,
            "avg_special": statistics.mean(a["special"]) if a["special"] else 0,
            "total_trades": a["total_trades"],
            "score": sc, "rating": rt,
        }
        row.update(ext_agg)
        rows.append(row)
    rows.sort(key=lambda x: x["score"], reverse=True)

    for suffix, col, col_key in [
        ("بازده_واقعی", "بازده واقعی(%)", "avg_real"),
        ("بازده_ویژه",  "بازده ویژه(%)",  "avg_special"),
    ]:
        # فقط ۵۰۰ استراتژی برتر بر اساس امتیاز در خروجی نهایی نگهداری شوند
        table_rows = sorted(rows, key=lambda x: x["score"], reverse=True)[:500]

        path = os.path.join(out_dir, f"جدول_مقایسه_همه_{suffix}.csv")
        with open(path, "w", encoding="utf-8-sig", newline="") as f:
            w = csv.writer(f)
            w.writerow(["رتبه", "پوشه", "گروه", "نوع", "تعداد نمادها", col,
                        "وین‌ریت(%)"]
                       + _ext_headers(COMPARISON_EXT_FIELDS)
                       + ["حداکثر ضرر متوالی(%)", "شارپ", "کل معاملات", "امتیاز", "وضعیت"])
            for i, row in enumerate(table_rows, 1):
                w.writerow([i, row["folder"], row["group"],
                            "معکوس" if row["is_inv"] else "اصلی",
                            row["n"], f"{row[col_key]:.4f}%", f"{row['avg_win']:.2f}"]
                           + _ext_row(row, COMPARISON_EXT_FIELDS)
                           + [f"{row['avg_loss']:.2f}", f"{row['avg_sh']:.3f}",
                              row["total_trades"], f"{row['score']:.1f}/100", row["rating"]])
        log.info("جدول %s → %s", suffix, path)


def _extract_start(period):
    m = re.search(r'(\d{4}-\d{2}-\d{2})_', period or "")
    return m.group(1) if m else None

def _extract_end(period):
    m = re.search(r'_(\d{4}-\d{2}-\d{2})$', period or "")
    return m.group(1) if m else None

def _events_in_range(events, s, e):
    try:
        sd = datetime.strptime(s, "%Y-%m-%d").date()
        ed = datetime.strptime(e, "%Y-%m-%d").date()
    except Exception:
        return []
    return [ev for ev in events if sd <= ev["date"] <= ed]


def _monthly_records(recs, group):
    mf = {}
    for r in recs:
        start = _extract_start(r.get("period_name", ""))
        if not start:
            continue
        mf.setdefault(start[:7], []).append(r)
    out = []
    for ym, files in mf.items():
        sel = []
        for day in [1, 11, 21]:
            found = next((f for f in files if _extract_start(f.get("period_name","")) == f"{ym}-{day:02d}"), None)
            if found:
                sel.append(found)
        if len(sel) != 3:
            continue
        total_ret = sum(f["special_rounded_return"] for f in sel) if group == "1" else sum(f["real_return"] for f in sel)
        max_l = min((f["max_consecutive_loss"] for f in sel if f.get("max_consecutive_loss") is not None), default=0)
        # باگ ۴ (حل‌شده با باگ ۳): چون max_consecutive_loss همیشه منفی است، min بدترین را انتخاب می‌کند
        out.append({"year_month": ym, "total_return": total_ret, "max_loss": max_l,
                    "files": [f["file_name"] for f in sel], "file_objects": sel})
    out.sort(key=lambda x: x["year_month"])
    return out


def _monthly_report(all_results, out_dir, top_n=3):
    strat_files = defaultdict(list)
    strat_scores = {}
    for r in all_results:
        strat_files[(r["period_name"], r["group"])].append(r)
    for k, files in strat_files.items():
        # باگ ۱۲: فقط رکوردهای غیر معکوس در امتیازدهی شرکت می‌کنند
        non_inv = [f for f in files if not f.get("is_inverse")]
        n = len(non_inv)
        if not n:
            continue
        ge1 = sum(1 for f in non_inv if f["special_rounded_return"] >= 1)
        gt0 = sum(1 for f in non_inv if f["special_rounded_return"] > 0)
        ml = [f["max_consecutive_loss"] for f in non_inv if f.get("max_consecutive_loss") is not None]
        avg_l = statistics.mean(ml) if ml else 0
        sc, _ = _score(ge1, gt0, avg_l, n, **_score_kwargs_from_records(non_inv))
        strat_scores[k] = sc

    top = sorted(strat_scores.items(), key=lambda x: x[1], reverse=True)[:top_n]
    for (period, group), _ in top:
        # باگ ۱۱: فیلتر بر اساس period_name (استراتژی پایه)
        recs = [f for f in strat_files[(period, group)] if not f.get("is_inverse")]
        monthly = _monthly_records(recs, group)
        if not monthly:
            continue
        for rec in monthly:
            n = len(rec["file_objects"])
            ge1 = sum(1 for f in rec["file_objects"] if f["special_rounded_return"] >= 1)
            gt0 = sum(1 for f in rec["file_objects"] if f["special_rounded_return"] > 0)
            sc, rt = _score(ge1, gt0, rec["max_loss"], n, **_score_kwargs_from_records(rec["file_objects"]))
            rec["score"] = sc; rec["rating"] = rt

        sp = os.path.join(out_dir, group, period)
        os.makedirs(sp, exist_ok=True)
        path = os.path.join(sp, "گزارش_ماهانه_برترین.csv")
        with open(path, "w", encoding="utf-8-sig", newline="") as f:
            w = csv.writer(f)
            w.writerow([f"📊 گزارش ماهانه: {period} (گروه {group})"])
            w.writerow(["تاریخ تولید", datetime.now().strftime("%Y-%m-%d %H:%M:%S")])
            w.writerow([])
            w.writerow(["رتبه", "ماه", "بازده(%)", "وین‌ریت(%)", "حداکثر ضرر(%)", "شارپ", "امتیاز", "وضعیت", "فایل‌ها"])
            for i, rec in enumerate(sorted(monthly, key=lambda x: x["score"], reverse=True), 1):
                fobjs = rec["file_objects"]
                tt = sum(f["total_trades"] for f in fobjs)
                wt = sum(f["win_trades"] for f in fobjs)
                wr = (wt / tt * 100) if tt else 0
                shs = [f["sharpe_ratio"] for f in fobjs if f.get("sharpe_ratio") is not None]
                avg_sh = statistics.mean(shs) if shs else 0
                w.writerow([i, rec["year_month"], f"{rec['total_return']:.4f}", f"{wr:.2f}",
                            f"{rec['max_loss']:.2f}", f"{avg_sh:.3f}",
                            f"{rec['score']:.1f}/100", rec["rating"], "; ".join(rec["files"])])
        log.info("گزارش ماهانه %s → %s", period, path)


def _complementary(all_results, news_events, out_dir):
    strat_map = defaultdict(list)
    for r in all_results:
        if not r.get("is_inverse"):
            strat_map[(r["period_name"], r["group"])].append(r)
    for (period, group), recs in strat_map.items():
        monthly = _monthly_records(recs, group)
        if not monthly:
            continue
        events_data = []
        for rec in monthly:
            # باگ ۱۰: file_objects از _monthly_records قبلاً مرتب‌شده (ترتیب day=1,11,21)
            start = _extract_start(rec["file_objects"][0].get("period_name", ""))
            end = _extract_end(rec["file_objects"][-1].get("period_name", ""))
            if not start or not end:
                continue
            for ev in _events_in_range(news_events, start, end):
                if ev.get("actual") is not None and ev.get("forecast") is not None:
                    events_data.append({
                        "month": rec["year_month"], "return_month": rec["total_return"],
                        "indicator": ev["indicator"],
                        "diff": ev["actual"] - ev["forecast"]
                    })
        if not events_data:
            continue
        path = os.path.join(out_dir, group, period, "تحلیل_تکمیلی_استراتژی.txt")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write("=" * 70 + "\n")
            f.write(f"📈 تحلیل تکمیلی: {period} (گروه {group})\n")
            f.write(f"تاریخ: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
            f.write("=" * 70 + "\n\n")
            for ind in sorted(set(e["indicator"] for e in events_data)):
                ind_ev = [e for e in events_data if e["indicator"] == ind]
                for label, evs in [("ضررده", [e for e in ind_ev if e["return_month"] < 0]),
                                    ("سودده", [e for e in ind_ev if e["return_month"] >= 0])]:
                    diffs = [e["diff"] for e in evs]
                    avg = f"{statistics.mean(diffs):.4f}" if diffs else "---"
                    f.write(f"\n📌 {ind} | ماه‌های {label}: تعداد={len(evs)}  avg_diff={avg}\n")
        log.info("تحلیل تکمیلی %s → %s", period, path)


# ══════════════════════════════════════════════════════════════
# کمکی
# ══════════════════════════════════════════════════════════════

def _write_json(path, data):
    os.makedirs(os.path.dirname(path) if os.path.dirname(path) else ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)

# ══════════════════════════════════════════════════════════════
# main
# ══════════════════════════════════════════════════════════════

def main():
    setup_logging()
    parser = argparse.ArgumentParser(description="ماژول یکپارچه محاسبات")
    sub = parser.add_subparsers(dest="cmd", required=True)

    # ─── returns ───
    p = sub.add_parser("returns")
    p.add_argument("--strategies-json", required=True)
    p.add_argument("--results-base", required=True)
    p.add_argument("--output", required=True)
    p.add_argument("--password", default="")

    # ─── risk ───
    p = sub.add_parser("risk")
    p.add_argument("--strategies-json", required=True)
    p.add_argument("--results-base", required=True)
    p.add_argument("--stoploss-cache", required=True)
    p.add_argument("--output", required=True)
    p.add_argument("--password", default="")

    # ─── inverse ───
    p = sub.add_parser("inverse")
    p.add_argument("--strategies-json", required=True)
    p.add_argument("--results-base", required=True)
    p.add_argument("--stoploss-cache", required=True)
    p.add_argument("--output", required=True)
    p.add_argument("--password", default="")

    # ─── report ───
    p = sub.add_parser("report")
    p.add_argument("--returns-cache", required=True)
    p.add_argument("--strategies-json", required=True)
    p.add_argument("--risk-cache", required=True)
    p.add_argument("--inverse-cache", default="")
    p.add_argument("--results-base", default="")
    p.add_argument("--news-pickle", default="")
    p.add_argument("--output-dir", required=True)
    p.add_argument("--password", default="")

    args = parser.parse_args()
    {"returns": cmd_returns, "risk": cmd_risk, "inverse": cmd_inverse, "report": cmd_report}[args.cmd](args)


if __name__ == "__main__":
    main()
