#!/usr/bin/env python3
# combo_10day.py - تحلیل ترکیبی الگوهای خبری در بازه‌های زمانی دلخواه
# نسخه ادغام‌شده: هم CSV و هم JSONL (امضاهای per-period) تولید می‌کند.
# اولویت ۱: --ohlc-dir اجباری است. اگر داده نشود یا پوشه خالی باشد، exit 1.
# اولویت ۲: --jsonl-out اختیاری است؛ در صورت داده‌شدن، JSONL نیز تولید می‌شود.

import os
import sys
import json
import csv
import glob
import argparse
import itertools
import statistics
import re
from datetime import datetime, timedelta
from collections import defaultdict

import pandas as pd

# ================================ ثابت‌ها ================================
INDICATORS = ['CPI m/m', 'Core CPI m/m', 'PPI m/m', 'Core PPI m/m', 'FOMC', 'CPI y/y']
THRESHOLDS = [0.0, 0.1, 0.2, 0.3]

INTERVAL_TO_INDICATOR = {
    'CPI':      'CPI m/m',
    'CoreCPI':  'Core CPI m/m',
    'PPI':      'PPI m/m',
    'CorePPI':  'Core PPI m/m',
    'FOMC':     'FOMC',
    'CPI_y_y':  'CPI y/y',
}

VALID_MODELS = ['simple_hybrid', 'fibonacci_full', 'fibonacci_hybrid']

# نسبت‌های فیبوناچی برای تقسیم بازه‌های زمانی (تجمعی، از ابتدای بازه)
FIBONACCI_RATIOS = [0.236, 0.382, 0.5, 0.618, 0.786, 1.0]

# ================================ توابع کمکی بازده ویژه ================================

def round_to_nearest(value, options):
    if not options:
        return value
    return min(options, key=lambda x: abs(x - value))


def apply_special_rounding(percent, move_percents):
    """تبدیل سود خام به بازده ویژه (special rounded return)"""
    if not move_percents or percent == 0:
        return percent
    try:
        if percent > 0:
            temp = percent + 0.05
            nearest = round_to_nearest(temp, move_percents)
            result = nearest - 0.05
        else:
            temp = abs(percent) + 0.05
            nearest = round_to_nearest(temp, move_percents)
            result = -(nearest - 0.05)
        return result - 0.1
    except Exception:
        return percent


# ================================ توابع کمکی خواندن CSV ================================

def find_column_index(header, keyword):
    kw = keyword.lower()
    for i, h in enumerate(header):
        if kw in h.lower():
            return i
    return -1


def parse_percent(value_str):
    if value_str is None or str(value_str).strip() in ('', '-', '—', '--'):
        return None
    cleaned = re.sub(r'[^\d.-]', '', str(value_str))
    if cleaned in ('', '-', '.', '-.'):
        return None
    try:
        return float(cleaned)
    except ValueError:
        return None


def detect_indicator_from_filename(filename_no_ext):
    name = filename_no_ext.lower()
    if 'core_cpi' in name or 'core cpi' in name:
        return 'Core CPI m/m'
    if 'core_ppi' in name or 'core ppi' in name:
        return 'Core PPI m/m'
    if 'cpi_y_y' in name or 'cpi y/y' in name:
        return 'CPI y/y'
    if 'cpi' in name:
        return 'CPI m/m'
    if 'ppi' in name:
        return 'PPI m/m'
    if 'fomc' in name or 'federal' in name or 'interest rate' in name:
        return 'FOMC'
    if 'moneycontrol' in name or 'united_states' in name:
        return 'FOMC'
    return None


def load_news_from_directory(news_dir):
    events = []
    if not os.path.isdir(news_dir):
        print(f"⚠️ پوشه اخبار یافت نشد: {news_dir}")
        return events

    print(f"📂 بارگذاری اخبار از {news_dir}")
    for filename in sorted(os.listdir(news_dir)):
        if not filename.endswith('.csv'):
            continue

        file_path = os.path.join(news_dir, filename)
        base_name = filename.replace('.csv', '').strip()
        indicator = detect_indicator_from_filename(base_name)
        if indicator is None:
            print(f"   ⚠️ {filename}: indicator ناشناخته → نادیده گرفته شد")
            continue

        is_fomc = (indicator == 'FOMC')
        try:
            with open(file_path, 'r', encoding='utf-8') as f:
                reader = csv.reader(f)
                header = next(reader)
                date_idx     = find_column_index(header, 'date')
                actual_idx   = find_column_index(header, 'actual')
                forecast_idx = find_column_index(header, 'forecast')
                if forecast_idx == -1:
                    forecast_idx = find_column_index(header, 'consensus')
                previous_idx  = find_column_index(header, 'previous')
                reference_idx = find_column_index(header, 'reference') if is_fomc else -1

                if date_idx == -1:
                    print(f"   ⚠️ {filename}: ستون تاریخ یافت نشد → نادیده گرفته شد")
                    continue

                count = 0
                for row in reader:
                    if not row:
                        continue
                    if is_fomc and reference_idx != -1 and reference_idx < len(row):
                        ref = row[reference_idx].strip()
                        if 'Interest Rate' not in ref and 'FOMC' not in ref:
                            continue
                    if date_idx >= len(row):
                        continue
                    date_str = row[date_idx].strip()
                    try:
                        event_date = datetime.strptime(date_str, "%b %d, %Y").date()
                    except ValueError:
                        try:
                            event_date = datetime.strptime(date_str, "%Y-%m-%d").date()
                        except ValueError:
                            continue

                    actual   = parse_percent(row[actual_idx])   if actual_idx != -1   and actual_idx < len(row)   else None
                    forecast = parse_percent(row[forecast_idx]) if forecast_idx != -1 and forecast_idx < len(row) else None
                    previous = parse_percent(row[previous_idx]) if previous_idx != -1 and previous_idx < len(row) else None

                    events.append({
                        "date":      event_date,
                        "indicator": indicator,
                        "actual":    actual,
                        "forecast":  forecast,
                        "previous":  previous,
                    })
                    count += 1

            print(f"   ✅ {filename} → [{indicator}]: {count} رویداد")
        except Exception as e:
            print(f"   ⚠️ خطا در خواندن {filename}: {e}")

    print(f"📰 مجموع رویدادهای خبری بارگذاری‌شده: {len(events)}")
    return events


# ================================ OHLC بارگذاری ================================

def load_ohlc_data(ohlc_dir):
    """
    [اولویت ۱] بارگذاری داده‌های OHLC از پوشه.
    - ستون‌های date یا timestamp را می‌پذیرد.
    - تایم‌فریم را بر اساس میانگین فاصله‌ی زمانی بین رکوردها تشخیص می‌دهد.
    - اگر تایم‌فریم زیر-روزانه بود، با pd.Grouper(freq='D') به روزانه resample می‌کند.
    - اگر بعد از resample تعداد روزها < 200 بود، هشدار چاپ می‌کند.
    """
    columns = ["date", "coin", "open", "high", "low", "close"]
    if not ohlc_dir or not os.path.isdir(ohlc_dir):
        return pd.DataFrame(columns=columns)

    csv_paths = sorted(glob.glob(os.path.join(ohlc_dir, "*.csv")))
    if not csv_paths:
        return pd.DataFrame(columns=columns)

    frames = []
    for path in csv_paths:
        coin = os.path.splitext(os.path.basename(path))[0]
        try:
            df = pd.read_csv(path)
        except Exception:
            continue

        df.columns = [str(col).strip().lower() for col in df.columns]

        # ۱. پشتیبانی از ستون timestamp به‌عنوان date
        if "date" not in df.columns and "timestamp" in df.columns:
            df = df.rename(columns={"timestamp": "date"})
            print(f"   ℹ️ [{coin}] ستون 'timestamp' به 'date' تغییر نام داد.")

        required = {"date", "open", "high", "low", "close"}
        if not required.issubset(set(df.columns)):
            print(f"   ⚠️ [{coin}] ستون‌های لازم یافت نشد → نادیده گرفته شد.")
            continue

        df = df[["date", "open", "high", "low", "close"]].copy()
        df["date"] = pd.to_datetime(df["date"], errors="coerce")
        df = df.dropna(subset=["date"])
        if df.empty:
            continue

        df = df.sort_values("date").reset_index(drop=True)

        # ۲. تشخیص تایم‌فریم (میانگین فاصله‌ی زمانی بین رکوردها)
        if len(df) >= 2:
            time_diffs = df["date"].diff().dropna()
            avg_diff_minutes = time_diffs.dt.total_seconds().mean() / 60.0
        else:
            avg_diff_minutes = 1440.0  # فرض روزانه

        is_intraday = avg_diff_minutes < 60 * 23  # کمتر از ۲۳ ساعت → زیر-روزانه

        if is_intraday:
            timeframe_str = (
                f"{int(avg_diff_minutes)}دقیقه‌ای" if avg_diff_minutes < 60
                else f"{avg_diff_minutes/60:.1f}ساعته"
            )
            print(f"   ⏱️ [{coin}] تایم‌فریم تشخیص داده شد: ~{timeframe_str} "
                  f"(میانگین فاصله {avg_diff_minutes:.1f} دقیقه) → resample به روزانه")

            # ۳. resample به روزانه
            df = df.set_index("date")
            df_daily = df.resample("D").agg(
                open=("open", "first"),
                high=("high", "max"),
                low=("low", "min"),
                close=("close", "last"),
            ).dropna(subset=["close"])
            df_daily = df_daily.reset_index()
            df_daily.columns = ["date", "open", "high", "low", "close"]
            df = df_daily
            print(f"   ✅ [{coin}] بعد از resample: {len(df)} روز کاری")
        else:
            print(f"   ✅ [{coin}] تایم‌فریم روزانه تشخیص داده شد "
                  f"(میانگین فاصله {avg_diff_minutes:.1f} دقیقه)")

        # ۴. هشدار اگر تعداد روزها < 200
        if len(df) < 200:
            print(f"   ⚠️ [{coin}] تعداد روزهای OHLC ({len(df)}) کمتر از ۲۰۰ است → "
                  f"market_regime این کوین 'unknown' خواهد بود (MA200 قابل‌محاسبه نیست).")

        df["coin"] = coin
        frames.append(df[columns])

    if not frames:
        return pd.DataFrame(columns=columns)

    combined = pd.concat(frames, ignore_index=True)
    combined = combined.sort_values(["coin", "date"]).reset_index(drop=True)
    print(f"✅ OHLC: {len(combined)} ردیف از {len(frames)} کوین بارگذاری شد.")
    return combined


def compute_market_regime(ohlc_df, coin, start_date):
    """
    رژیم بازار را برای یک کوین مشخص، صرفاً بر اساس داده‌های قیمت *قبل* از
    start_date محاسبه می‌کند (بدون آینده‌نگری).
    """
    if ohlc_df is None or len(ohlc_df) == 0 or coin is None:
        return "unknown"

    coin_df = ohlc_df[ohlc_df["coin"] == coin]
    if coin_df.empty:
        return "unknown"

    cutoff = pd.Timestamp(start_date) - timedelta(days=1)
    hist = coin_df[coin_df["date"] <= cutoff].sort_values("date")
    if hist.empty or len(hist) < 200:
        return "unknown"

    last_200 = hist.tail(200)
    last_50  = last_200.tail(50)
    last_14  = last_200.tail(14)

    ma50  = last_50["close"].mean()
    ma200 = last_200["close"].mean()
    atr   = (last_14["high"] - last_14["low"]).mean()
    price = hist["close"].iloc[-1]

    if price is None or pd.isna(price) or price == 0:
        return "unknown"
    if pd.isna(ma50) or pd.isna(ma200) or pd.isna(atr):
        return "unknown"

    if (atr / price) > 0.02:
        return "volatile"
    if ma50 > ma200:
        return "trending_up"
    if ma50 < ma200:
        return "trending_down"
    if abs(ma50 - ma200) / price < 0.05:
        return "ranging"
    return "unknown"


# ================================ توابع کمکی بازه‌های زمانی ================================

def parse_interval(interval):
    m_fixed = re.match(r'^fixed_(\d+)d$', interval)
    if m_fixed:
        return ('fixed', int(m_fixed.group(1)), None)
    m_news = re.match(r'^(.+)_(post|pre)_(\d+)d$', interval)
    if m_news:
        return (m_news.group(1), m_news.group(2), int(m_news.group(3)))
    return None


def find_nearest_event_date(events, target_date, indicator, direction='post'):
    filtered = [ev["date"] for ev in events
                if ev["indicator"] == indicator and ev["date"] is not None]
    if not filtered:
        return None
    if direction == 'post':
        candidates = [d for d in filtered if d <= target_date]
        return max(candidates) if candidates else None
    else:
        candidates = [d for d in filtered if d >= target_date]
        return min(candidates) if candidates else None


def find_adjacent_event_date(events, anchor_date, indicator, direction):
    filtered = sorted({ev["date"] for ev in events
                        if ev["indicator"] == indicator and ev["date"] is not None})
    if not filtered:
        return None
    if direction == 'post':
        candidates = [d for d in filtered if d > anchor_date]
        return min(candidates) if candidates else None
    else:
        candidates = [d for d in filtered if d < anchor_date]
        return max(candidates) if candidates else None


def build_fibonacci_sub_periods(gap_start, gap_end):
    total_days = (gap_end - gap_start).days + 1
    if total_days <= 0:
        return []

    boundaries = [0]
    for ratio in FIBONACCI_RATIOS:
        offset = round(ratio * total_days)
        offset = max(boundaries[-1], min(offset, total_days))
        boundaries.append(offset)
    if boundaries[-1] != total_days:
        boundaries[-1] = total_days

    sub_periods = []
    for i in range(len(boundaries) - 1):
        start_offset = boundaries[i]
        end_offset   = boundaries[i + 1]
        if end_offset <= start_offset:
            continue
        sub_start = gap_start + timedelta(days=start_offset)
        sub_end   = gap_start + timedelta(days=end_offset - 1)
        sub_periods.append((sub_start, sub_end))

    return sub_periods


def find_sub_period_for_date(date, sub_periods):
    for start, end in sub_periods:
        if start <= date <= end:
            return (start, end)
    return None


def get_period_key_from_date(date, interval, news_events, model='simple_hybrid'):
    parsed = parse_interval(interval)
    if parsed is None:
        print(f"⚠️ interval ناشناخته: {interval}")
        return None

    mode = parsed[0]
    if mode == 'fixed':
        days = parsed[1]
        epoch = datetime(2000, 1, 1).date()
        delta = (date - epoch).days
        period_num = delta // days
        start = epoch + timedelta(days=period_num * days)
        end   = start + timedelta(days=days - 1)
        return f"{start.isoformat()}_{end.isoformat()}"

    indicator_key  = mode
    direction      = parsed[1]
    days           = parsed[2]
    indicator_name = INTERVAL_TO_INDICATOR.get(indicator_key)
    if not indicator_name:
        print(f"⚠️ indicator_key ناشناخته در interval: {indicator_key}")
        return None

    if days == 0:
        return date.isoformat()

    event_date = find_nearest_event_date(news_events, date, indicator_name, direction)
    if event_date is None:
        return None

    if model == 'simple_hybrid':
        if direction == 'post':
            start = event_date + timedelta(days=1)
            end   = start + timedelta(days=days - 1)
        else:
            end   = event_date - timedelta(days=1)
            start = end - timedelta(days=days - 1)
        return f"{start.isoformat()}_{end.isoformat()}"

    adjacent_event_date = find_adjacent_event_date(news_events, event_date, indicator_name, direction)
    if adjacent_event_date is None:
        if direction == 'post':
            start = event_date + timedelta(days=1)
            end   = start + timedelta(days=days - 1)
        else:
            end   = event_date - timedelta(days=1)
            start = end - timedelta(days=days - 1)
        return f"{start.isoformat()}_{end.isoformat()}"

    if direction == 'post':
        gap_start = event_date + timedelta(days=1)
        gap_end   = adjacent_event_date - timedelta(days=1)
    else:
        gap_start = adjacent_event_date + timedelta(days=1)
        gap_end   = event_date - timedelta(days=1)

    gap_total_days = (gap_end - gap_start).days + 1
    if gap_total_days <= 0:
        return None

    if model == 'fibonacci_full':
        sub_periods = build_fibonacci_sub_periods(gap_start, gap_end)
        match = find_sub_period_for_date(date, sub_periods)
        if match is None:
            return None
        start, end = match
        return f"{start.isoformat()}_{end.isoformat()}"

    if model != 'fibonacci_hybrid':
        print(f"⚠️ مدل ناشناخته در get_period_key_from_date: {model}")
        return None

    # fibonacci_hybrid
    if gap_total_days <= days:
        return f"{gap_start.isoformat()}_{gap_end.isoformat()}"

    if direction == 'post':
        fixed_start = gap_start
        fixed_end   = fixed_start + timedelta(days=days - 1)
        remainder_start = fixed_end + timedelta(days=1)
        remainder_end   = gap_end
    else:
        fixed_end   = gap_end
        fixed_start = fixed_end - timedelta(days=days - 1)
        remainder_start = gap_start
        remainder_end   = fixed_start - timedelta(days=1)

    if fixed_start <= date <= fixed_end:
        return f"{fixed_start.isoformat()}_{fixed_end.isoformat()}"

    sub_periods = build_fibonacci_sub_periods(remainder_start, remainder_end)
    match = find_sub_period_for_date(date, sub_periods)
    if match is None:
        return None
    start, end = match
    return f"{start.isoformat()}_{end.isoformat()}"


def compute_indicator_status_for_period(start_date, end_date, news_events):
    events_in_range = [ev for ev in news_events
                       if start_date <= ev["date"] <= end_date]
    events_by_indicator = defaultdict(list)
    for ev in events_in_range:
        events_by_indicator[ev["indicator"]].append(ev)

    status = {}
    for ind in INDICATORS:
        evs = events_by_indicator.get(ind, [])
        if not evs:
            status[ind] = None
            continue
        diffs = []
        for ev in evs:
            if ev["actual"] is not None and ev["forecast"] is not None:
                diffs.append(ev["actual"] - ev["forecast"])
        if not diffs:
            status[ind] = {thr: 'Neutral' for thr in THRESHOLDS}
            continue
        avg_diff = statistics.mean(diffs)
        status[ind] = {}
        for thr in THRESHOLDS:
            if avg_diff > thr:
                status[ind][thr] = 'Bad'
            elif avg_diff < -thr:
                status[ind][thr] = 'Good'
            else:
                status[ind][thr] = 'Neutral'
    return status


# ================================ محاسبه بازده بر اساس مدل ================================

def compute_trade_profit(raw_profit, move_percents, model, trade, use_tp_sl=False, take_profit=None, stop_loss=None):
    """
    محاسبه بازده معامله:
    - حالت skeep (use_tp_sl=True): بازده خام با TP/SL محدود می‌شود.
    - حالت عادی: apply_special_rounding اجباری است.
    """
    if use_tp_sl:
        result = raw_profit
        if take_profit is not None and result >= take_profit:
            result = take_profit
        if stop_loss is not None and result <= -stop_loss:
            result = -stop_loss
        return result

    if not move_percents:
        return raw_profit
    return apply_special_rounding(raw_profit, move_percents)


# ================================ تابع اصلی تحلیل ================================

def load_skeep_tp_sl(trades_json_path):
    base_dir = os.path.dirname(os.path.abspath(trades_json_path))
    enc_path = trades_json_path

    if not os.path.exists(enc_path):
        return False, None, None

    skeep_path = os.path.join(base_dir, "skeepmove_percents.txt")
    if not os.path.exists(skeep_path):
        return False, None, None

    take_profit = None
    stop_loss   = None
    try:
        with open(skeep_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line.startswith("take_profit"):
                    val = re.sub(r'[^\d.-]', '', line.split("=", 1)[-1])
                    take_profit = float(val) if val else None
                elif line.startswith("stop_loss"):
                    val = re.sub(r'[^\d.-]', '', line.split("=", 1)[-1])
                    stop_loss = float(val) if val else None
        print(f"📄 skeepmove_percents.txt یافت شد → take_profit={take_profit}, stop_loss={stop_loss}")
        return True, take_profit, stop_loss
    except Exception as e:
        print(f"⚠️ خطا در خواندن skeepmove_percents.txt: {e}")
        return False, None, None


def _importance(actual, forecast, distance_days):
    if actual is None or forecast is None:
        return None
    d = distance_days if distance_days is not None else 0
    return abs(actual - forecast) * (1.0 / (d + 1))


def process_analysis(trades_json_path, news_dir, interval, target_coin,
                     chunk_start, chunk_end, output_path, model,
                     ohlc_dir=None, jsonl_out=None, min_sample_count=1):
    """
    تحلیل اصلی.
    [اولویت ۱] ohlc_dir اجباری است — بدون آن، پردازش ادامه می‌یابد اما
    market_regime همه "unknown" خواهد بود (چون enforce در main() است).
    [اولویت ۲] اگر jsonl_out داده شده باشد، فایل JSONL نیز تولید می‌شود.
    """

    # ---------- بارگذاری OHLC ----------
    ohlc_df = None
    if ohlc_dir:
        print(f"📊 بارگذاری OHLC از {ohlc_dir} ...")
        ohlc_df = load_ohlc_data(ohlc_dir)
        if len(ohlc_df) == 0:
            print("⚠️ هیچ داده OHLC یافت نشد — market_regime همه 'unknown' خواهند بود.")
    else:
        print("ℹ️ --ohlc-dir داده نشده — market_regime همه 'unknown' خواهند بود.")

    # ---------- مرحله ۰: بررسی حالت skeep ----------
    use_tp_sl, take_profit, stop_loss = load_skeep_tp_sl(trades_json_path)
    if use_tp_sl:
        print(f"🔀 حالت skeep فعال: take_profit={take_profit}, stop_loss={stop_loss}")
    else:
        print("✅ حالت عادی: apply_special_rounding اجباری برای همه معاملات")

    # ---------- مرحله ۱: بارگذاری معاملات ----------
    print(f"🔍 بارگذاری معاملات از {trades_json_path}")
    with open(trades_json_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    if isinstance(data, list):
        trades   = data
        metadata = None
        print("⚠️ فرمت قدیمی (بدون متادیتا).")
    elif isinstance(data, dict) and "trades" in data:
        trades   = data["trades"]
        metadata = data.get("metadata", {})
        print("✅ فرمت جدید با متادیتا.")
    else:
        raise ValueError("فایل معاملات باید آرایه JSON یا دیکشنری با کلید 'trades' باشد.")

    if not isinstance(trades, list):
        raise ValueError("فیلد 'trades' باید آرایه JSON باشد.")

    print(f"📊 تعداد کل معاملات: {len(trades)}")

    move_percents = metadata.get("move_percents", []) if metadata else []

    if not use_tp_sl:
        if move_percents:
            print(f"📈 move_percents (اجباری): {move_percents}")
        else:
            print("⚠️ move_percents خالی است — fallback به raw_profit")

    # ---------- مرحله ۲: فیلتر بر اساس کوین ----------
    target_coins = [c.strip() for c in target_coin.split('+')]

    trade_list = []
    for t in trades:
        symbol = t.get("symbol") or t.get("pair") or t.get("coin")
        if symbol not in target_coins:
            continue

        time_str = (t.get("entryTime") or t.get("entry_time") or
                    t.get("open_time") or t.get("time") or t.get("timestamp"))
        if not time_str:
            continue

        try:
            if 'T' in str(time_str):
                date_part = str(time_str).split('T')[0]
            elif ' ' in str(time_str):
                date_part = str(time_str).split(' ')[0]
            else:
                date_part = str(time_str)
            trade_date = datetime.strptime(date_part, "%Y-%m-%d").date()
        except (ValueError, AttributeError):
            continue

        raw_profit = t.get("profitPercent", 0.0)
        try:
            raw_profit = float(raw_profit)
        except (TypeError, ValueError):
            raw_profit = 0.0

        profit = compute_trade_profit(
            raw_profit, move_percents, model, t,
            use_tp_sl=use_tp_sl,
            take_profit=take_profit,
            stop_loss=stop_loss,
        )
        trade_list.append((trade_date, profit))

    if not trade_list:
        print(f"⚠️ هیچ معامله‌ای برای کوین {target_coin} یافت نشد.")
        _write_empty_csv(output_path)
        if jsonl_out:
            _write_jsonl(jsonl_out, [])
        return

    print(f"✅ معاملات {target_coin} با تاریخ معتبر: {len(trade_list)}")

    # ---------- مرحله ۳: بارگذاری اخبار ----------
    news_events = load_news_from_directory(news_dir)
    if not news_events:
        print("⚠️ هیچ رویداد خبری بارگذاری نشد.")
        _write_empty_csv(output_path)
        if jsonl_out:
            _write_jsonl(jsonl_out, [])
        return

    # ---------- مرحله ۴: گروه‌بندی معاملات بر اساس بازه زمانی ----------
    period_groups = defaultdict(list)
    skipped = 0
    for date, profit in trade_list:
        period_key = get_period_key_from_date(date, interval, news_events, model=model)
        if period_key is None:
            skipped += 1
            continue
        period_groups[period_key].append(profit)

    if skipped > 0:
        print(f"   ℹ️ {skipped} معامله بدون بازه معتبر نادیده گرفته شد.")

    if not period_groups:
        print("⚠️ هیچ بازه معتبری تشکیل نشد.")
        _write_empty_csv(output_path)
        if jsonl_out:
            _write_jsonl(jsonl_out, [])
        return

    print(f"📊 تعداد دوره‌های تشکیل‌شده: {len(period_groups)}")

    period_returns = {period: sum(profits) for period, profits in period_groups.items()}

    # ---------- مرحله ۵: محاسبه وضعیت خبری هر دوره ----------
    period_status = {}
    for period_key, ret in period_returns.items():
        if len(period_key) < 21:
            continue
        start_str = period_key[:10]
        end_str   = period_key[11:]
        try:
            start_date = datetime.strptime(start_str, "%Y-%m-%d").date()
            end_date   = datetime.strptime(end_str,   "%Y-%m-%d").date()
        except ValueError:
            continue
        status = compute_indicator_status_for_period(start_date, end_date, news_events)
        period_status[period_key] = (status, ret, start_date, end_date)

    if not period_status:
        print("⚠️ هیچ دوره‌ای وضعیت خبری معتبر نداشت.")
        _write_empty_csv(output_path)
        if jsonl_out:
            _write_jsonl(jsonl_out, [])
        return

    print(f"✅ دوره‌های دارای وضعیت خبری: {len(period_status)}")

    # ---------- مرحله ۶: تولید ترکیب‌های شاخص/آستانه و CSV ----------
    all_combinations = []
    for r in range(1, len(INDICATORS) + 1):
        for subset_tuple in itertools.combinations(INDICATORS, r):
            for thr in THRESHOLDS:
                all_combinations.append((thr, list(subset_tuple)))

    total     = len(all_combinations)
    start_idx = max(0, chunk_start)
    end_idx   = min(total, chunk_end) if chunk_end is not None else total
    selected  = all_combinations[start_idx:end_idx]
    print(f"🎯 پردازش چانک: {start_idx} تا {end_idx} (تعداد {len(selected)})")

    csv_rows = []
    for thr, subset in selected:
        pattern_counts = defaultdict(lambda: {
            'total': 0, 'loss': 0, 'profit': 0, 'periods': []
        })

        for period, (status_dict, ret, _sd, _ed) in period_status.items():
            valid = True
            pattern_parts = []
            for ind in subset:
                st = status_dict.get(ind)
                if st is None or thr not in st:
                    valid = False
                    break
                pattern_parts.append(st[thr])
            if not valid:
                continue

            pattern = tuple(pattern_parts)
            pattern_counts[pattern]['total'] += 1
            pattern_counts[pattern]['periods'].append(period)
            if ret < 0:
                pattern_counts[pattern]['loss'] += 1
            else:
                pattern_counts[pattern]['profit'] += 1

        for pattern_tuple, counts in pattern_counts.items():
            total_cnt  = counts['total']
            loss_cnt   = counts['loss']
            profit_cnt = counts['profit']
            loss_pct   = (loss_cnt / total_cnt) * 100 if total_cnt > 0 else 0
            profit_pct = (profit_cnt / total_cnt) * 100 if total_cnt > 0 else 0
            odds       = (loss_pct / profit_pct) if profit_pct > 0 else float('inf')

            csv_rows.append({
                'آستانه':          thr,
                'تعداد_شاخص‌ها':   len(subset),
                'لیست_شاخص‌ها':    '|'.join(subset),
                'الگوی_وضعیت':     '|'.join(pattern_tuple),
                'تعداد_کل_وقوع':   total_cnt,
                'تعداد_ضررده':     loss_cnt,
                'تعداد_سودده':     profit_cnt,
                'درصد_ضررده':      round(loss_pct, 1),
                'درصد_سودده':      round(profit_pct, 1),
                'نسبت_شانس':       odds if odds == float('inf') else round(odds, 2),
                'دوره‌ها':         '|'.join(counts['periods']),
            })

    _write_csv(output_path, csv_rows)

    # ---------- [اولویت ۲] تولید JSONL در صورت درخواست ----------
    if jsonl_out:
        parsed_iv = parse_interval(interval)
        indicator_key = parsed_iv[0] if parsed_iv else None
        direction     = parsed_iv[1] if parsed_iv else None
        distance_days = parsed_iv[2] if parsed_iv else None
        first_coin    = target_coins[0] if target_coins else None

        records = []
        for period_key, (status_dict, ret, start_date, end_date) in period_status.items():
            profits = period_groups.get(period_key, [])
            if len(profits) < min_sample_count:
                continue

            events_in_range = [ev for ev in news_events if start_date <= ev["date"] <= end_date]

            dominant_indicator = None
            dominant_score = -1.0
            diffs_all = []
            indicators_present = set()
            for ev in events_in_range:
                indicators_present.add(ev["indicator"])
                if ev["actual"] is None or ev["forecast"] is None:
                    continue
                diff = ev["actual"] - ev["forecast"]
                diffs_all.append(diff)
                d_days = abs((ev["date"] - start_date).days)
                score = _importance(ev["actual"], ev["forecast"], d_days)
                if score is not None and score > dominant_score:
                    dominant_score = score
                    dominant_indicator = ev["indicator"]

            total_return   = sum(profits)
            trade_count    = len(profits)
            avg_trade_ret  = (total_return / trade_count) if trade_count else 0.0
            period_len     = (end_date - start_date).days + 1
            avg_daily_ret  = (avg_trade_ret / period_len) if period_len else 0.0
            secondary      = sorted(indicators_present - ({dominant_indicator} if dominant_indicator else set()))

            market_regime  = compute_market_regime(ohlc_df, first_coin, start_date)

            records.append({
                "coin_composition":                target_coin,
                "model":                           model,
                "interval":                        interval,
                "indicator_key":                   indicator_key,
                "position":                        direction,
                "distance_days":                   distance_days,
                "period_start":                    start_date.isoformat(),
                "period_end":                      end_date.isoformat(),
                "period_length_days":              period_len,
                "total_return":                    total_return,
                "trade_count":                     trade_count,
                "avg_trade_return":                avg_trade_ret,
                "avg_daily_return":                avg_daily_ret,
                "dominant_indicator":              dominant_indicator,
                "dominant_indicator_importance":   (dominant_score if dominant_score >= 0 else None),
                "secondary_indicators":            secondary,
                "diff_avg":    (statistics.mean(diffs_all) if diffs_all else None),
                "diff_std":    (statistics.pstdev(diffs_all) if len(diffs_all) > 1 else (0.0 if diffs_all else None)),
                "event_count":         len(events_in_range),
                "indicator_diversity": len(indicators_present),
                "use_tp_sl":    use_tp_sl,
                "take_profit":  take_profit,
                "stop_loss":    stop_loss,
                "strategy_folder": "",  # در main تنظیم می‌شود
                "market_regime": market_regime,
            })

        _write_jsonl(jsonl_out, records)


def _write_empty_csv(output_path):
    _write_csv(output_path, [])


def _write_csv(output_path, csv_rows):
    fieldnames = [
        'آستانه', 'تعداد_شاخص‌ها', 'لیست_شاخص‌ها', 'الگوی_وضعیت',
        'تعداد_کل_وقوع', 'تعداد_ضررده', 'تعداد_سودده',
        'درصد_ضررده', 'درصد_سودده', 'نسبت_شانس', 'دوره‌ها',
    ]
    parent_dir = os.path.dirname(output_path)
    if parent_dir:
        os.makedirs(parent_dir, exist_ok=True)

    with open(output_path, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        if csv_rows:
            writer.writerows(csv_rows)

    if csv_rows:
        print(f"✅ {len(csv_rows)} ردیف ذخیره شد: {output_path}")
    else:
        print(f"⚠️ فایل خالی ایجاد شد (داده‌ای یافت نشد): {output_path}")


def _write_jsonl(out_path, records):
    out_dir = os.path.dirname(out_path)
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    print(f"✅ {len(records)} رکورد JSONL ذخیره شد: {out_path}")


# ================================ main ================================

def main():
    parser = argparse.ArgumentParser(
        description="تحلیل ترکیبی الگوهای خبری (combo_10day) — هم CSV هم JSONL"
    )
    parser.add_argument("--trades-json",      required=True)
    parser.add_argument("--news-dir",         required=True)
    parser.add_argument("--interval",         required=True)
    parser.add_argument("--chunk-start",      type=int, default=0)
    parser.add_argument("--chunk-end",        type=int, default=None)
    parser.add_argument("--strategy-folder",  required=True)
    parser.add_argument("--coin",             required=True)
    parser.add_argument("--model",            required=True, choices=VALID_MODELS)

    # [اولویت ۱] --ohlc-dir اجباری است
    parser.add_argument("--ohlc-dir", required=True,
                        help="مسیر پوشه CSVهای OHLC روزانه (هر فایل = یک کوین). اجباری است.")

    # [اولویت ۲] تولید JSONL
    parser.add_argument("--jsonl-out", default=None,
                        help="مسیر خروجی JSONL (امضاهای per-period). اختیاری.")
    parser.add_argument("--min-sample-count", type=int, default=1,
                        help="حداقل تعداد معامله در هر دوره برای ثبت در JSONL.")

    args = parser.parse_args()

    # [اولویت ۱] اجبار OHLC: اگر پوشه وجود نداشت یا خالی بود، exit 1
    if not os.path.isdir(args.ohlc_dir):
        print(f"❌ [OHLC اجباری] پوشه --ohlc-dir یافت نشد: {args.ohlc_dir}")
        print("❌ pipeline باید fail شود: داده OHLC وجود ندارد.")
        sys.exit(1)

    csv_count = len(glob.glob(os.path.join(args.ohlc_dir, "*.csv")))
    if csv_count == 0:
        print(f"❌ [OHLC اجباری] پوشه --ohlc-dir خالی است (هیچ CSV یافت نشد): {args.ohlc_dir}")
        print("❌ pipeline باید fail شود: داده OHLC وجود ندارد.")
        sys.exit(1)

    print(f"✅ [OHLC] پوشه معتبر یافت شد با {csv_count} فایل CSV: {args.ohlc_dir}")

    output_file = f"{args.strategy_folder}_{args.coin}_{args.interval}_{args.model}.csv"
    output_path = os.path.join(os.getcwd(), output_file)

    process_analysis(
        trades_json_path=args.trades_json,
        news_dir=args.news_dir,
        interval=args.interval,
        target_coin=args.coin,
        chunk_start=args.chunk_start,
        chunk_end=args.chunk_end,
        output_path=output_path,
        model=args.model,
        ohlc_dir=args.ohlc_dir,
        jsonl_out=args.jsonl_out,
        min_sample_count=args.min_sample_count,
    )


if __name__ == "__main__":
    main()
