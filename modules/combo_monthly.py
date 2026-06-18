#!/usr/bin/env python3
# combo_monthly.py - تحلیل ترکیبی الگوهای خبری در بازه ماهانه
# نسخه اصلاح‌شده

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

VALID_MODELS = ['simple_hybrid', 'fibonacci_full', 'fibonacci_hybrid']


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


# ================================ محاسبه وضعیت خبری ================================

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

def compute_trade_profit(raw_profit, move_percents, model, trade):
    """
    محاسبه بازده معامله بر اساس مدل:

    - simple_hybrid:    بازده خام — بدون special rounding
    - fibonacci_full:   همه معاملات با apply_special_rounding
                        اگر move_percents خالی باشد → بازده خام (fallback با warning)
    - fibonacci_hybrid: بر اساس فیلد is_fibonacci در داده معامله
                        اگر move_percents خالی باشد → همیشه بازده خام (fallback)
    """
    if model == 'simple_hybrid':
        return raw_profit

    elif model == 'fibonacci_full':
        if not move_percents:
            return raw_profit
        return apply_special_rounding(raw_profit, move_percents)

    elif model == 'fibonacci_hybrid':
        if not move_percents:
            return raw_profit
        is_fib = trade.get("is_fibonacci", False)
        if is_fib:
            return apply_special_rounding(raw_profit, move_percents)
        else:
            return raw_profit

    else:
        return raw_profit


# ================================ تابع اصلی تحلیل ================================

def process_analysis(trades_json_path, news_dir, target_coin,
                     chunk_start, chunk_end, output_path, model):
    """
    تحلیل ماهانه.
    اصلاح کلیدی: پارامتر model دریافت می‌شود و در compute_trade_profit استفاده می‌شود.
    اصلاح کلیدی: move_percents خالی دیگر باعث crash نمی‌شود — fallback به raw_profit.
    """

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

    if model == 'simple_hybrid':
        print("ℹ️ مدل simple_hybrid: بازده خام (raw_profit) استفاده می‌شود.")
    elif model in ('fibonacci_full', 'fibonacci_hybrid'):
        if move_percents:
            print(f"📈 مدل {model}: move_percents = {move_percents}")
        else:
            print(f"⚠️ مدل {model}: move_percents خالی است — این استراتژی فیبوناچی نیست.")
            print("   fallback: بازده خام (raw_profit) استفاده می‌شود.")

    # ---------- مرحله ۲: فیلتر بر اساس کوین + گروه‌بندی ماهانه ----------
    target_coins = [c.strip() for c in target_coin.split('+')]

    monthly_profits = defaultdict(list)
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

        profit = compute_trade_profit(raw_profit, move_percents, model, t)

        ym = trade_date.strftime("%Y-%m")
        monthly_profits[ym].append(profit)

    if not monthly_profits:
        print(f"⚠️ هیچ معامله‌ای برای {target_coin} یافت نشد.")
        _write_empty_csv(output_path)
        return

    print(f"✅ {len(monthly_profits)} ماه با داده برای {target_coin}")

    # ---------- مرحله ۳: بارگذاری اخبار ----------
    news_events = load_news_from_directory(news_dir)
    if not news_events:
        print("⚠️ هیچ رویداد خبری بارگذاری نشد.")
        _write_empty_csv(output_path)
        return

    # ---------- مرحله ۴: محاسبه بازده ماهانه + وضعیت خبری ----------
    month_status = {}
    for ym in sorted(monthly_profits.keys()):
        profits = monthly_profits[ym]
        year    = int(ym[:4])
        month   = int(ym[5:7])

        start_date = datetime(year, month, 1).date()
        if month == 12:
            end_date = datetime(year + 1, 1, 1).date() - timedelta(days=1)
        else:
            end_date = datetime(year, month + 1, 1).date() - timedelta(days=1)

        total_return = sum(profits)
        status = compute_indicator_status_for_period(start_date, end_date, news_events)
        month_status[ym] = (status, total_return, start_date, end_date)

    if not month_status:
        print("⚠️ هیچ ماهی وضعیت خبری معتبر نداشت.")
        _write_empty_csv(output_path)
        return

    print(f"✅ {len(month_status)} ماه دارای وضعیت خبری")

    # ---------- مرحله ۵: تولید همه ترکیب‌های شاخص/آستانه ----------
    all_combinations = []
    for r in range(1, len(INDICATORS) + 1):
        for subset_tuple in itertools.combinations(INDICATORS, r):
            for thr in THRESHOLDS:
                all_combinations.append((thr, list(subset_tuple)))

    print(f"🔢 تعداد کل ترکیب‌ها: {len(all_combinations)}")

    total     = len(all_combinations)
    start_idx = max(0, chunk_start)
    end_idx   = min(total, chunk_end) if chunk_end is not None else total
    selected  = all_combinations[start_idx:end_idx]
    print(f"🎯 چانک: {start_idx} تا {end_idx} ({len(selected)} ترکیب)")

    # ---------- مرحله ۶: محاسبه الگوها ----------
    csv_rows = []
    for thr, subset in selected:
        pattern_counts = defaultdict(lambda: {
            'total': 0, 'loss': 0, 'profit': 0, 'months': []
        })

        for ym, (status_dict, ret, _start, _end) in month_status.items():
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
            pattern_counts[pattern]['months'].append(ym)
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
                'ماه‌ها':          '|'.join(counts['months']),
            })

    # ---------- مرحله ۷: ذخیره CSV ----------
    _write_csv(output_path, csv_rows)


def _write_empty_csv(output_path):
    _write_csv(output_path, [])


def _write_csv(output_path, csv_rows):
    fieldnames = [
        'آستانه', 'تعداد_شاخص‌ها', 'لیست_شاخص‌ها', 'الگوی_وضعیت',
        'تعداد_کل_وقوع', 'تعداد_ضررده', 'تعداد_سودده',
        'درصد_ضررده', 'درصد_سودده', 'نسبت_شانس', 'ماه‌ها',
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


# ================================ main ================================

def main():
    parser = argparse.ArgumentParser(
        description="تحلیل ترکیبی ماهانه (combo_monthly)"
    )
    parser.add_argument("--trades-json",     required=True)
    parser.add_argument("--news-dir",        required=True)
    parser.add_argument("--strategy-folder", required=True)
    parser.add_argument("--coin",            required=True)
    parser.add_argument("--interval",        required=True)
    parser.add_argument("--model",           required=True, choices=VALID_MODELS)
    parser.add_argument("--chunk-start",     type=int, default=0)
    parser.add_argument("--chunk-end",       type=int, default=None)
    args = parser.parse_args()

    output_file = f"{args.strategy_folder}_{args.coin}_{args.interval}_{args.model}.csv"
    output_path = os.path.join(os.getcwd(), output_file)

    process_analysis(
        trades_json_path=args.trades_json,
        news_dir=args.news_dir,
        target_coin=args.coin,
        chunk_start=args.chunk_start,
        chunk_end=args.chunk_end,
        output_path=output_path,
        model=args.model,
    )


if __name__ == "__main__":
    main()
