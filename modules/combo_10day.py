#!/usr/bin/env python3
# combo_10day.py - تحلیل ترکیبی الگوهای خبری در بازه‌های زمانی دلخواه
# نسخه اصلاح‌شده: تمام باگ‌های شناسایی‌شده برطرف شده‌اند

import os
import sys
import json
import csv
import argparse
import itertools
import statistics
import re
from datetime import datetime, timedelta
from collections import defaultdict

# ================================ ثابت‌ها ================================
INDICATORS = ['CPI m/m', 'Core CPI m/m', 'PPI m/m', 'Core PPI m/m', 'FOMC', 'CPI y/y']
THRESHOLDS = [0.0, 0.1, 0.2, 0.3]

# نقشه تبدیل نام شاخص در interval به نام استاندارد در INDICATORS
# باگ ۲ اصلاح شد: CPI_y_y به عنوان یک کلید کامل اضافه شد
INTERVAL_TO_INDICATOR = {
    'CPI':      'CPI m/m',
    'CoreCPI':  'Core CPI m/m',
    'PPI':      'PPI m/m',
    'CorePPI':  'Core PPI m/m',
    'FOMC':     'FOMC',
    'CPI_y_y':  'CPI y/y',
}

# ================================ توابع کمکی بازده ویژه ================================

def round_to_nearest(value, options):
    """نزدیک‌ترین مقدار از لیست options را به value برمی‌گرداند"""
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
    """پیدا کردن اندیس ستون بر اساس کلیدواژه (case-insensitive)"""
    kw = keyword.lower()
    for i, h in enumerate(header):
        if kw in h.lower():
            return i
    return -1


def parse_percent(value_str):
    """تبدیل رشته درصد (مثل '0.3%' یا '-0.2') به عدد اعشاری"""
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
    """
    باگ ۱ و باگ ۶ اصلاح شد:
    تشخیص indicator بر اساس بررسی substring در نام فایل (lowercase).
    این روش مستقل از regex پیچیده یا exact-match است و با هر فرمت نام فایل کار می‌کند.
    """
    name = filename_no_ext.lower()

    # ترتیب بررسی مهم است: اول موارد خاص‌تر (Core) بررسی می‌شوند
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
    # باگ ۶: فایل FOMC که پسوند Moneycontrol دارد نه Forex Factory
    if 'fomc' in name or 'federal' in name or 'interest rate' in name:
        return 'FOMC'
    # چک اضافه برای فایل Moneycontrol
    if 'moneycontrol' in name or 'united_states' in name:
        return 'FOMC'

    return None  # ناشناخته → نادیده گرفته می‌شود


def load_news_from_directory(news_dir):
    """
    بارگذاری رویدادهای خبری از همه فایل‌های CSV داخل پوشه news_dir.

    باگ‌های اصلاح‌شده:
      - باگ ۱: جایگزینی regex با detect_indicator_from_filename (substring-based)
      - باگ ۴: FOMC: داده‌های actual و consensus از فایل خوانده می‌شوند
      - باگ ۶: فایل FOMC با نام Moneycontrol هم تشخیص داده می‌شود
    """
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

        # باگ ۱ اصلاح شد: تشخیص indicator با substring
        indicator = detect_indicator_from_filename(base_name)
        if indicator is None:
            print(f"   ⚠️ {filename}: indicator ناشناخته → نادیده گرفته شد")
            continue

        is_fomc = (indicator == 'FOMC')

        try:
            with open(file_path, 'r', encoding='utf-8') as f:
                reader = csv.reader(f)
                header = next(reader)

                date_idx    = find_column_index(header, 'date')
                actual_idx  = find_column_index(header, 'actual')
                forecast_idx = find_column_index(header, 'forecast')
                if forecast_idx == -1:
                    # فایل FOMC از 'consensus' به جای 'forecast' استفاده می‌کند
                    forecast_idx = find_column_index(header, 'consensus')
                previous_idx = find_column_index(header, 'previous')

                # برای فایل FOMC یک reference_idx نیز داریم
                reference_idx = find_column_index(header, 'reference') if is_fomc else -1

                if date_idx == -1:
                    print(f"   ⚠️ {filename}: ستون تاریخ یافت نشد → نادیده گرفته شد")
                    continue

                count = 0
                for row in reader:
                    if not row:
                        continue

                    # باگ ۴ اصلاح شد: برای FOMC فقط ردیف‌های 'Fed Interest Rate Decision'
                    # یا ردیف‌هایی که actual معتبر دارند پردازش می‌شوند
                    if is_fomc and reference_idx != -1 and reference_idx < len(row):
                        ref = row[reference_idx].strip()
                        # فقط تصمیمات نرخ بهره اصلی را در نظر می‌گیریم
                        if 'Interest Rate' not in ref and 'FOMC' not in ref:
                            continue

                    # پارس تاریخ
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

                    # باگ ۴ اصلاح شد: برای FOMC هم actual و forecast خوانده می‌شوند
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


# ================================ توابع کمکی بازه‌های زمانی ================================

def parse_interval(interval):
    """
    باگ ۲ اصلاح شد:
    پارس interval با regex از انتها به جای split('_') ساده.
    این تابع هر interval مثل CPI_y_y_post_10d را درست تجزیه می‌کند.

    خروجی:
      ('fixed', days_int, None)           برای fixed_Xd
      (indicator_key, direction, days_int) برای news-based
      None                                 در صورت خطا
    """
    # بررسی فرمت fixed
    m_fixed = re.match(r'^fixed_(\d+)d$', interval)
    if m_fixed:
        return ('fixed', int(m_fixed.group(1)), None)

    # باگ ۲: parse از انتها با regex تا نام شاخص چند-قسمتی درست تشخیص داده شود
    # فرمت: <indicator_key>_(post|pre)_<days>d
    m_news = re.match(r'^(.+)_(post|pre)_(\d+)d$', interval)
    if m_news:
        indicator_key = m_news.group(1)   # مثلاً CPI_y_y یا CoreCPI
        direction     = m_news.group(2)   # post یا pre
        days          = int(m_news.group(3))
        return (indicator_key, direction, days)

    return None


def find_nearest_event_date(events, target_date, indicator, direction='post'):
    """
    نزدیک‌ترین رویداد خبری برای indicator مشخص نسبت به target_date.
    direction='post': اولین رویداد بعد از target_date (یا همان روز)
    direction='pre':  آخرین رویداد قبل از target_date (یا همان روز)
    """
    filtered = [ev["date"] for ev in events
                if ev["indicator"] == indicator and ev["date"] is not None]
    if not filtered:
        return None

    if direction == 'post':
        candidates = [d for d in filtered if d >= target_date]
        return min(candidates) if candidates else None
    else:
        candidates = [d for d in filtered if d <= target_date]
        return max(candidates) if candidates else None


def get_period_key_from_date(date, interval, news_events):
    """
    محاسبه کلید بازه زمانی برای یک تاریخ معین.
    باگ ۲ اصلاح شد: از parse_interval استفاده می‌شود.
    خروجی: رشته 'YYYY-MM-DD_YYYY-MM-DD' یا None
    """
    parsed = parse_interval(interval)
    if parsed is None:
        print(f"⚠️ interval ناشناخته: {interval}")
        return None

    mode = parsed[0]

    # ---- بازه ثابت (fixed) ----
    if mode == 'fixed':
        days = parsed[1]
        epoch = datetime(2000, 1, 1).date()
        delta = (date - epoch).days
        period_num = delta // days
        start = epoch + timedelta(days=period_num * days)
        end   = start + timedelta(days=days - 1)
        return f"{start.isoformat()}_{end.isoformat()}"

    # ---- بازه وابسته به خبر ----
    indicator_key = mode
    direction     = parsed[1]
    days          = parsed[2]

    # نگاشت indicator_key به نام استاندارد
    indicator_name = INTERVAL_TO_INDICATOR.get(indicator_key)
    if not indicator_name:
        print(f"⚠️ indicator_key ناشناخته در interval: {indicator_key}")
        return None

    if days == 0:
        return date.isoformat()

    # پیدا کردن تاریخ رویداد خبری نزدیک
    event_date = find_nearest_event_date(news_events, date, indicator_name, direction)
    if event_date is None:
        return None

    # محاسبه بازه زمانی
    if direction == 'post':
        start = event_date + timedelta(days=1)
        end   = start + timedelta(days=days - 1)
    else:  # pre
        end   = event_date - timedelta(days=1)
        start = end - timedelta(days=days - 1)

    return f"{start.isoformat()}_{end.isoformat()}"


def compute_indicator_status_for_period(start_date, end_date, news_events):
    """
    محاسبه وضعیت (Good/Bad/Neutral) هر شاخص برای بازه [start_date, end_date].
    باگ ۴ اصلاح شد: FOMC اگر actual و forecast داشت، diff محاسبه می‌شود.
    """
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
            # رویداد وجود دارد ولی داده کافی ندارد (مثل برخی ردیف‌های FOMC)
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


# ================================ تابع اصلی تحلیل ================================

def process_analysis(trades_json_path, news_dir, interval, target_coin,
                     chunk_start, chunk_end, output_path):
    """
    تحلیل اصلی:
      - بارگذاری معاملات و متادیتا
      - فیلتر بر اساس کوین
      - گروه‌بندی معاملات بر اساس interval
      - محاسبه وضعیت خبری هر دوره
      - تولید ترکیب‌های شاخص/آستانه
      - ذخیره نتایج در CSV
    """

    # ---------- مرحله ۱: بارگذاری معاملات ----------
    print(f"🔍 بارگذاری معاملات از {trades_json_path}")
    with open(trades_json_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    if isinstance(data, list):
        trades   = data
        metadata = None
        print("⚠️ فرمت قدیمی (بدون متادیتا) — بازده ویژه محاسبه نمی‌شود.")
    elif isinstance(data, dict) and "trades" in data:
        trades   = data["trades"]
        metadata = data.get("metadata", {})
        print("✅ فرمت جدید با متادیتا.")
    else:
        raise ValueError("فایل معاملات باید آرایه JSON یا دیکشنری با کلید 'trades' باشد.")

    if not isinstance(trades, list):
        raise ValueError("فیلد 'trades' باید آرایه JSON باشد.")

    print(f"📊 تعداد کل معاملات: {len(trades)}")

    # استخراج move_percents از متادیتا
    move_percents = metadata.get("move_percents", []) if metadata else []
    if move_percents:
        print(f"📈 move_percents: {move_percents}")
    else:
        print("⚠️ move_percents موجود نیست — بازده ویژه محاسبه نمی‌شود.")

    # ---------- مرحله ۲: فیلتر بر اساس کوین ----------
    # پشتیبانی از کوین‌های ترکیبی مثل BTCUSDT+ETHUSDT (مانند combo_monthly)
    target_coins = [c.strip() for c in target_coin.split('+')]

    trade_list = []  # لیست (date, profit)
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

        profit = apply_special_rounding(raw_profit, move_percents) if move_percents else raw_profit
        trade_list.append((trade_date, profit))

    if not trade_list:
        print(f"⚠️ هیچ معامله‌ای برای کوین {target_coin} یافت نشد.")
        # باگ ۵ اصلاح شد: فایل خالی ایجاد می‌شود تا workflow نشکند
        _write_empty_csv(output_path)
        return

    print(f"✅ معاملات {target_coin} با تاریخ معتبر: {len(trade_list)}")

    # ---------- مرحله ۳: بارگذاری اخبار ----------
    news_events = load_news_from_directory(news_dir)
    if not news_events:
        print("⚠️ هیچ رویداد خبری بارگذاری نشد.")
        _write_empty_csv(output_path)
        return

    # ---------- مرحله ۴: گروه‌بندی معاملات بر اساس بازه زمانی ----------
    period_groups = defaultdict(list)
    skipped = 0
    for date, profit in trade_list:
        period_key = get_period_key_from_date(date, interval, news_events)
        if period_key is None:
            skipped += 1
            continue
        period_groups[period_key].append(profit)

    if skipped > 0:
        print(f"   ℹ️ {skipped} معامله بدون بازه معتبر نادیده گرفته شد.")

    if not period_groups:
        print("⚠️ هیچ بازه معتبری تشکیل نشد.")
        _write_empty_csv(output_path)
        return

    print(f"📊 تعداد دوره‌های تشکیل‌شده: {len(period_groups)}")

    # بازده کل هر دوره
    period_returns = {period: sum(profits) for period, profits in period_groups.items()}
    for p, ret in list(period_returns.items())[:5]:
        print(f"   نمونه دوره {p}: بازده {ret:.4f}%")

    # ---------- مرحله ۵: محاسبه وضعیت خبری هر دوره ----------
    period_status = {}
    for period_key, ret in period_returns.items():
        # period_key فرمت 'YYYY-MM-DD_YYYY-MM-DD' دارد
        # چون تاریخ‌ها با dash هستند و separator هم underscore است،
        # split('_') ممکن است بیش از ۲ قسمت بدهد.
        # راه‌حل: برش از وسط با ایندکس ثابت (هر تاریخ ۱۰ کاراکتر است)
        if len(period_key) < 21:
            continue
        start_str = period_key[:10]   # 'YYYY-MM-DD'
        end_str   = period_key[11:]   # 'YYYY-MM-DD' (بعد از underscore)

        try:
            start_date = datetime.strptime(start_str, "%Y-%m-%d").date()
            end_date   = datetime.strptime(end_str,   "%Y-%m-%d").date()
        except ValueError:
            continue

        status = compute_indicator_status_for_period(start_date, end_date, news_events)
        period_status[period_key] = (status, ret)

    if not period_status:
        print("⚠️ هیچ دوره‌ای وضعیت خبری معتبر نداشت.")
        _write_empty_csv(output_path)
        return

    print(f"✅ دوره‌های دارای وضعیت خبری: {len(period_status)}")

    # ---------- مرحله ۶: تولید ترکیب‌های شاخص/آستانه ----------
    all_combinations = []
    for r in range(1, len(INDICATORS) + 1):
        for subset_tuple in itertools.combinations(INDICATORS, r):
            for thr in THRESHOLDS:
                all_combinations.append((thr, list(subset_tuple)))

    print(f"🔢 تعداد کل ترکیب‌های ممکن: {len(all_combinations)}")

    total     = len(all_combinations)
    start_idx = max(0, chunk_start)
    end_idx   = min(total, chunk_end) if chunk_end is not None else total
    selected  = all_combinations[start_idx:end_idx]
    print(f"🎯 پردازش چانک: {start_idx} تا {end_idx} (تعداد {len(selected)})")

    # ---------- مرحله ۷: محاسبه الگوها ----------
    csv_rows = []
    for thr, subset in selected:
        pattern_counts = defaultdict(lambda: {
            'total': 0, 'loss': 0, 'profit': 0, 'periods': []
        })

        for period, (status_dict, ret) in period_status.items():
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

    # ---------- مرحله ۸: ذخیره CSV ----------
    _write_csv(output_path, csv_rows)


def _write_empty_csv(output_path):
    """ایجاد فایل CSV خالی (فقط header) برای جلوگیری از شکست workflow"""
    _write_csv(output_path, [])


def _write_csv(output_path, csv_rows):
    """
    ذخیره خروجی در CSV.
    باگ ۵ اصلاح شد: بررسی dirname قبل از makedirs تا از خطا جلوگیری شود.
    """
    fieldnames = [
        'آستانه', 'تعداد_شاخص‌ها', 'لیست_شاخص‌ها', 'الگوی_وضعیت',
        'تعداد_کل_وقوع', 'تعداد_ضررده', 'تعداد_سودده',
        'درصد_ضررده', 'درصد_سودده', 'نسبت_شانس', 'دوره‌ها',
    ]

    # باگ ۵: اگر dirname خالی نبود makedirs می‌زنیم
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


# ================================ main ================================

def main():
    parser = argparse.ArgumentParser(
        description="تحلیل ترکیبی الگوهای خبری (combo_10day) — نسخه اصلاح‌شده"
    )
    parser.add_argument("--trades-json",      required=True,
                        help="مسیر فایل یکپارچه معاملات (all_trades.json)")
    parser.add_argument("--news-dir",         required=True,
                        help="پوشه CSV اخبار (data/news)")
    parser.add_argument("--interval",         required=True,
                        help="نوع بازه (مثلاً fixed_5d, CPI_post_7d, CPI_y_y_pre_3d)")
    parser.add_argument("--chunk-start",      type=int, default=0,
                        help="شروع چانک ترکیب‌ها")
    parser.add_argument("--chunk-end",        type=int, default=None,
                        help="پایان چانک ترکیب‌ها")
    parser.add_argument("--strategy-folder",  required=True,
                        help="نام پوشه استراتژی (برای نام خروجی)")
    parser.add_argument("--coin",             required=True,
                        help="جفت ارز (مثلاً BTCUSDT)")
    parser.add_argument("--model",            required=True,
                        help="مدل محاسبه (برای نام خروجی)")
    args = parser.parse_args()

    output_file = (
        f"{args.strategy_folder}_{args.coin}_{args.interval}_{args.model}.csv"
    )
    output_path = os.path.join(os.getcwd(), output_file)

    process_analysis(
        trades_json_path=args.trades_json,
        news_dir=args.news_dir,
        interval=args.interval,
        target_coin=args.coin,
        chunk_start=args.chunk_start,
        chunk_end=args.chunk_end,
        output_path=output_path,
    )


if __name__ == "__main__":
    main()
