#!/usr/bin/env python3
# combo_monthly.py - تحلیل ترکیبی الگوهای خبری در بازه ماهانه
# سازگار با workflow: همان آرگومان‌های combo_10day را می‌گیرد
# تفاوت: معاملات را ماهانه گروه‌بندی می‌کند (نه per-interval)

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

# ================================ توابع کمکی بازده ویژه ================================
def round_to_nearest(value, options):
    if not options:
        return value
    return min(options, key=lambda x: abs(x - value))

def apply_special_rounding(percent, move_percents):
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
    except:
        return percent

# ================================ بارگذاری اخبار از CSV ================================
def find_column_index(header, keyword):
    for i, h in enumerate(header):
        if keyword in h.lower():
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

def load_news_from_directory(news_dir):
    events = []
    indicator_map = {
        'US Core CPI m_m': 'Core CPI m/m',
        'US Core PPI m_m': 'Core PPI m/m',
        'US CPI m_m': 'CPI m/m',
        'US CPI y_y': 'CPI y/y',
        'US PPI m_m': 'PPI m/m',
        'United States Fomc Minutes': 'FOMC',
        'FOMC': 'FOMC',
    }
    if not os.path.isdir(news_dir):
        print(f"⚠️ پوشه اخبار یافت نشد: {news_dir}")
        return events
    print(f"📂 بارگذاری اخبار از {news_dir}")
    for filename in os.listdir(news_dir):
        if not filename.endswith('.csv'):
            continue
        file_path = os.path.join(news_dir, filename)
        try:
            with open(file_path, 'r', encoding='utf-8') as f:
                reader = csv.reader(f)
                header = next(reader)
                base_name = filename.replace('.csv', '').strip()
                base_name = re.sub(r'\s*_\s*Forex\s*Factory\s*$', '', base_name, flags=re.IGNORECASE)
                indicator = indicator_map.get(base_name, base_name)
                is_fomc = (indicator == 'FOMC')
                date_idx = find_column_index(header, 'date')
                if date_idx == -1:
                    continue
                actual_idx = find_column_index(header, 'actual')
                forecast_idx = find_column_index(header, 'forecast')
                if forecast_idx == -1:
                    forecast_idx = find_column_index(header, 'consensus')
                previous_idx = find_column_index(header, 'previous')
                count = 0
                for row in reader:
                    if not row:
                        continue
                    date_str = row[date_idx].strip()
                    try:
                        event_date = datetime.strptime(date_str, "%b %d, %Y").date()
                    except:
                        try:
                            event_date = datetime.strptime(date_str, "%Y-%m-%d").date()
                        except:
                            continue
                    if is_fomc:
                        actual = forecast = previous = None
                    else:
                        actual = parse_percent(row[actual_idx]) if actual_idx != -1 and actual_idx < len(row) else None
                        forecast = parse_percent(row[forecast_idx]) if forecast_idx != -1 and forecast_idx < len(row) else None
                        previous = parse_percent(row[previous_idx]) if previous_idx != -1 and previous_idx < len(row) else None
                    events.append({
                        "date": event_date,
                        "indicator": indicator,
                        "actual": actual,
                        "forecast": forecast,
                        "previous": previous
                    })
                    count += 1
                print(f"   ✅ {filename}: {count} رویداد")
        except Exception as e:
            print(f"⚠️ خطا در خواندن {filename}: {e}")
    print(f"📰 مجموع رویدادهای خبری: {len(events)}")
    return events

# ================================ وضعیت خبری برای یک بازه تاریخی ================================
def compute_indicator_status_for_period(start_date, end_date, news_events):
    events_in_range = [ev for ev in news_events if start_date <= ev["date"] <= end_date]
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
            # FOMC یا بدون داده: وجود رویداد را ثبت می‌کنیم
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
def process_analysis(trades_json_path, news_dir, target_coin, chunk_start, chunk_end, output_path):
    # ---------- 1. بارگذاری معاملات ----------
    print(f"🔍 بارگذاری معاملات از {trades_json_path}")
    with open(trades_json_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    if isinstance(data, list):
        trades = data
        metadata = None
        print("⚠️ فرمت قدیمی (بدون متادیتا)")
    elif isinstance(data, dict) and "trades" in data:
        trades = data["trades"]
        metadata = data.get("metadata", {})
        print("✅ فرمت جدید با متادیتا")
    else:
        raise ValueError("فایل معاملات باید آرایه JSON یا دیکشنری با کلید 'trades' باشد.")

    move_percents = metadata.get("move_percents", []) if metadata else []
    if move_percents:
        print(f"📈 move_percents: {move_percents}")

    # ---------- 2. فیلتر بر اساس کوین + گروه‌بندی ماهانه ----------
    # برای coin های ترکیبی (مثل BTCUSDT+ETHUSDT) همه کوین‌ها را در نظر می‌گیریم
    target_coins = [c.strip() for c in target_coin.split('+')]

    monthly_profits = defaultdict(list)  # key: "YYYY-MM"
    for t in trades:
        symbol = t.get("symbol") or t.get("pair") or t.get("coin")
        if symbol not in target_coins:
            continue
        time_str = t.get("entryTime") or t.get("entry_time") or t.get("open_time") or t.get("time") or t.get("timestamp")
        if not time_str:
            continue
        try:
            date_part = time_str.split('T')[0] if 'T' in time_str else time_str.split(' ')[0] if ' ' in time_str else time_str
            trade_date = datetime.strptime(date_part, "%Y-%m-%d").date()
        except:
            continue
        raw_profit = t.get("profitPercent", 0.0)
        try:
            raw_profit = float(raw_profit)
        except:
            raw_profit = 0.0
        profit = apply_special_rounding(raw_profit, move_percents) if move_percents else raw_profit
        ym = trade_date.strftime("%Y-%m")
        monthly_profits[ym].append(profit)

    if not monthly_profits:
        print(f"⚠️ هیچ معامله‌ای برای {target_coin} یافت نشد.")
        return
    print(f"✅ {len(monthly_profits)} ماه با داده برای {target_coin}")

    # ---------- 3. بارگذاری اخبار ----------
    news_events = load_news_from_directory(news_dir)
    if not news_events:
        print("⚠️ هیچ رویداد خبری بارگذاری نشد.")
        return

    # ---------- 4. محاسبه بازده ماهانه + وضعیت خبری ----------
    # بازه هر ماه: اول ماه تا آخر ماه
    month_status = {}
    for ym, profits in sorted(monthly_profits.items()):
        year, month = int(ym[:4]), int(ym[5:7])
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
        return
    print(f"✅ {len(month_status)} ماه دارای وضعیت خبری")

    # ---------- 5. همه ترکیب‌های شاخص/آستانه ----------
    all_combinations = []
    for r in range(1, len(INDICATORS) + 1):
        for subset_tuple in itertools.combinations(INDICATORS, r):
            for thr in THRESHOLDS:
                all_combinations.append((thr, list(subset_tuple)))
    print(f"🔢 تعداد کل ترکیب‌ها: {len(all_combinations)}")

    total = len(all_combinations)
    start_idx = max(0, chunk_start)
    end_idx = min(total, chunk_end) if chunk_end is not None else total
    selected = all_combinations[start_idx:end_idx]
    print(f"🎯 چانک: {start_idx} تا {end_idx} ({len(selected)} ترکیب)")

    # ---------- 6. محاسبه الگوها ----------
    csv_rows = []
    for thr, subset in selected:
        pattern_counts = defaultdict(lambda: {'total': 0, 'loss': 0, 'profit': 0, 'months': []})
        for ym, (status_dict, ret, _, _) in month_status.items():
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
            total_cnt = counts['total']
            loss_cnt = counts['loss']
            profit_cnt = counts['profit']
            loss_pct = (loss_cnt / total_cnt) * 100 if total_cnt > 0 else 0
            profit_pct = (profit_cnt / total_cnt) * 100 if total_cnt > 0 else 0
            odds = (loss_pct / profit_pct) if profit_pct > 0 else float('inf')
            csv_rows.append({
                'آستانه': thr,
                'تعداد_شاخص‌ها': len(subset),
                'لیست_شاخص‌ها': '|'.join(subset),
                'الگوی_وضعیت': '|'.join(pattern_tuple),
                'تعداد_کل_وقوع': total_cnt,
                'تعداد_ضررده': loss_cnt,
                'تعداد_سودده': profit_cnt,
                'درصد_ضررده': round(loss_pct, 1),
                'درصد_سودده': round(profit_pct, 1),
                'نسبت_شانس': odds if odds == float('inf') else round(odds, 2),
                'ماه‌ها': '|'.join(counts['months'])
            })

    # ---------- 7. ذخیره CSV ----------
    fieldnames = [
        'آستانه', 'تعداد_شاخص‌ها', 'لیست_شاخص‌ها', 'الگوی_وضعیت',
        'تعداد_کل_وقوع', 'تعداد_ضررده', 'تعداد_سودده',
        'درصد_ضررده', 'درصد_سودده', 'نسبت_شانس', 'ماه‌ها'
    ]
    os.makedirs(os.path.dirname(output_path) if os.path.dirname(output_path) else '.', exist_ok=True)
    with open(output_path, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        if csv_rows:
            writer.writerows(csv_rows)
    if csv_rows:
        print(f"✅ {len(csv_rows)} ردیف ذخیره شد: {output_path}")
    else:
        print(f"⚠️ هیچ ردیفی تولید نشد. فایل خالی: {output_path}")

# ================================ main ================================
def main():
    parser = argparse.ArgumentParser(description="تحلیل ترکیبی ماهانه (combo_monthly)")
    parser.add_argument("--trades-json", required=True, help="مسیر فایل یکپارچه معاملات")
    parser.add_argument("--news-dir", required=True, help="پوشه CSV اخبار")
    parser.add_argument("--strategy-folder", required=True, help="نام پوشه استراتژی (برای نام خروجی)")
    parser.add_argument("--coin", required=True, help="جفت ارز (مثلاً BTCUSDT یا BTCUSDT+ETHUSDT)")
    parser.add_argument("--interval", required=True, help="نوع بازه (برای نام خروجی - در این ماژول فقط ماهانه)")
    parser.add_argument("--model", required=True, help="مدل محاسبه (برای نام خروجی)")
    parser.add_argument("--chunk-start", type=int, default=0)
    parser.add_argument("--chunk-end", type=int, default=None)
    args = parser.parse_args()

    output_file = f"{args.strategy_folder}_{args.coin}_{args.interval}_{args.model}.csv"
    output_path = os.path.join(os.getcwd(), output_file)
    process_analysis(
        args.trades_json, args.news_dir, args.coin,
        args.chunk_start, args.chunk_end, output_path
    )

if __name__ == "__main__":
    main()
