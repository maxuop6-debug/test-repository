#!/usr/bin/env python3
# combo_10day.py - تحلیل ترکیبی الگوهای خبری در بازه‌های زمانی دلخواه
# نسخه نهایی با پشتیبانی از متادیتا، بازده ویژه، بازه‌های وابسته به خبر، و فیلتر بر اساس کوین

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

# نقشه برای تبدیل نام شاخص در interval به نام استاندارد در INDICATORS
INTERVAL_TO_INDICATOR = {
    'CPI': 'CPI m/m',
    'CoreCPI': 'Core CPI m/m',
    'PPI': 'PPI m/m',
    'CorePPI': 'Core PPI m/m',
    'FOMC': 'FOMC',
    'CPI_y_y': 'CPI y/y'
}

# ================================ توابع کمکی برای بازده ویژه ================================
def round_to_nearest(value, options):
    if not options:
        return value
    return min(options, key=lambda x: abs(x - value))

def apply_special_rounding(percent, move_percents):
    """ تبدیل سود خام به بازده ویژه (special rounded return) """
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

# ================================ توابع کمکی خبری (خواندن CSV) ================================
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
    """بارگذاری رویدادهای خبری از همه فایل‌های CSV داخل پوشه news_dir"""
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
                        actual = None
                        forecast = None
                        previous = None
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
            print(f"⚠️ خطا در خواندن فایل خبر {filename}: {e}")
    print(f"📰 مجموع رویدادهای خبری بارگذاری شده: {len(events)}")
    return events

# ================================ توابع کمکی برای بازه‌های زمانی ================================
def find_nearest_event_date(events, target_date, indicator, direction='post'):
    """
    پیدا کردن نزدیک‌ترین رویداد خبری به target_date برای شاخص مشخص.
    direction='post': رویداد بعد از target_date (برای بازه‌های post)
    direction='pre': رویداد قبل از target_date (برای بازه‌های pre)
    بازگرداندن تاریخ رویداد یا None.
    """
    filtered = [ev for ev in events if ev["indicator"] == indicator and ev["date"] is not None]
    if not filtered:
        return None
    if direction == 'post':
        candidates = [ev["date"] for ev in filtered if ev["date"] >= target_date]
        if candidates:
            return min(candidates)
        else:
            return None
    else:  # pre
        candidates = [ev["date"] for ev in filtered if ev["date"] <= target_date]
        if candidates:
            return max(candidates)
        else:
            return None

def get_period_key_from_date(date, interval, news_events):
    """
    با توجه به نوع interval، یک کلید بازه (رشته) برای تاریخ ورودی برمی‌گرداند.
    پشتیبانی از fixed_Xd، CPI_post_Xd، CPI_pre_Xd، و همینطور برای سایر شاخص‌ها.
    """
    if interval.startswith('fixed_'):
        days = int(interval.split('_')[1].replace('d', ''))
        epoch = datetime(2000, 1, 1).date()
        delta = (date - epoch).days
        period_num = delta // days
        start = epoch + timedelta(days=period_num * days)
        end = start + timedelta(days=days - 1)
        return f"{start.isoformat()}_{end.isoformat()}"
    
    # بازه‌های وابسته به خبر: format: <Indicator>_<post/pre>_<days>d
    parts = interval.split('_')
    if len(parts) >= 3:
        indicator_key = parts[0]          # مثلاً CPI, CoreCPI, PPI, ...
        direction = parts[1]              # post یا pre
        days_str = parts[2].replace('d', '')
        try:
            days = int(days_str)
        except:
            days = 0
        if days == 0:
            return date.isoformat()
        # نقشه به نام استاندارد شاخص
        indicator_name = INTERVAL_TO_INDICATOR.get(indicator_key)
        if not indicator_name:
            print(f"⚠️ شاخص ناشناخته در interval: {indicator_key}")
            return None
        # پیدا کردن تاریخ رویداد
        event_date = find_nearest_event_date(news_events, date, indicator_name, direction)
        if event_date is None:
            print(f"⚠️ برای تاریخ {date} و شاخص {indicator_name} رویداد {direction} یافت نشد.")
            return None
        # محاسبه شروع و پایان بازه
        if direction == 'post':
            start = event_date + timedelta(days=1)
            end = start + timedelta(days=days - 1)
        else:  # pre
            end = event_date - timedelta(days=1)
            start = end - timedelta(days=days - 1)
        return f"{start.isoformat()}_{end.isoformat()}"
    
    # fallback
    return date.isoformat()

def compute_indicator_status_for_period(start_date, end_date, news_events):
    """محاسبه وضعیت شاخص‌ها برای یک بازه زمانی مشخص (شامل start_date تا end_date)"""
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
            status[ind] = None
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
def process_analysis(trades_json_path, news_dir, interval, target_coin, chunk_start, chunk_end, output_path):
    """
    trades_json_path: فایل JSON شامل ساختار {"trades": [...], "metadata": {...}}
    news_dir: پوشه CSV اخبار
    interval: نوع بازه (مثلاً fixed_5d, CPI_post_7d)
    target_coin: کوین مورد نظر (مثل BTCUSDT)
    chunk_start, chunk_end: محدوده ترکیب‌های شاخص/آستانه
    """
    # ---------- مرحله 1: بارگذاری معاملات و متادیتا ----------
    print(f"🔍 بارگذاری معاملات از {trades_json_path}")
    with open(trades_json_path, "r", encoding="utf-8") as f:
        data = json.load(f)
    
    if isinstance(data, list):
        trades = data
        metadata = None
        print("⚠️ فرمت قدیمی فایل یکپارچه (بدون متادیتا) - بازده ویژه محاسبه نخواهد شد.")
    elif isinstance(data, dict) and "trades" in data:
        trades = data["trades"]
        metadata = data.get("metadata", {})
        print("✅ فرمت جدید فایل یکپارچه با متادیتا.")
    else:
        raise ValueError("فایل معاملات باید یک آرایه JSON یا دیکشنری با کلید 'trades' باشد.")
    
    if not isinstance(trades, list):
        raise ValueError("فیلد 'trades' باید یک آرایه JSON باشد.")
    print(f"📊 تعداد کل معاملات: {len(trades)}")
    
    # استخراج move_percents از متادیتا
    move_percents = metadata.get("move_percents", []) if metadata else []
    if move_percents:
        print(f"📈 move_percents استخراج شد: {move_percents}")
    else:
        print("⚠️ move_percents موجود نیست - بازده ویژه محاسبه نمی‌شود.")
    
    # ---------- مرحله 2: فیلتر بر اساس کوین و استخراج تاریخ/سود ----------
    trade_list = []  # هر عنصر: (date, profit)
    for t in trades:
        # بررسی فیلد symbol (ممکن است با نام‌های مختلف باشد)
        symbol = t.get("symbol") or t.get("pair") or t.get("coin")
        if symbol != target_coin:
            continue
        time_str = t.get("entryTime") or t.get("entry_time") or t.get("open_time") or t.get("time") or t.get("timestamp")
        if not time_str:
            continue
        try:
            if 'T' in time_str:
                date_part = time_str.split('T')[0]
            elif ' ' in time_str:
                date_part = time_str.split(' ')[0]
            else:
                date_part = time_str
            trade_date = datetime.strptime(date_part, "%Y-%m-%d").date()
        except:
            continue
        raw_profit = t.get("profitPercent", 0.0)
        try:
            raw_profit = float(raw_profit)
        except:
            raw_profit = 0.0
        # اعمال بازده ویژه در صورت وجود move_percents
        if move_percents:
            profit = apply_special_rounding(raw_profit, move_percents)
        else:
            profit = raw_profit
        trade_list.append((trade_date, profit))
    
    if not trade_list:
        print(f"⚠️ هیچ معامله‌ای برای کوین {target_coin} یافت نشد.")
        return
    print(f"✅ تعداد معاملات کوین {target_coin} با تاریخ معتبر: {len(trade_list)}")
    
    # ---------- مرحله 3: بارگذاری اخبار ----------
    news_events = load_news_from_directory(news_dir)
    if not news_events:
        print("⚠️ هیچ رویداد خبری بارگذاری نشد.")
        return
    
    # ---------- مرحله 4: گروه‌بندی بازه‌ها با استفاده از news_events و interval ----------
    period_groups = defaultdict(list)
    for date, profit in trade_list:
        period_key = get_period_key_from_date(date, interval, news_events)
        if period_key is None:
            continue
        period_groups[period_key].append(profit)
        if len(period_groups) <= 10:
            print(f"   تاریخ {date} -> بازه {period_key} (سود {profit})")
    
    if not period_groups:
        print("⚠️ هیچ بازه معتبری تشکیل نشد.")
        return
    print(f"📊 تعداد دوره‌های تشکیل شده: {len(period_groups)}")
    
    # محاسبه بازده کل هر دوره
    period_returns = {period: sum(profits) for period, profits in period_groups.items()}
    for p, ret in list(period_returns.items())[:5]:
        print(f"   نمونه دوره {p} : بازده کل {ret:.4f}%")
    
    # ---------- مرحله 5: محاسبه وضعیت خبری هر دوره ----------
    period_status = {}
    for period_key, ret in period_returns.items():
        parts = period_key.split('_')
        if len(parts) != 2:
            continue
        start_str, end_str = parts
        try:
            start_date = datetime.strptime(start_str, "%Y-%m-%d").date()
            end_date = datetime.strptime(end_str, "%Y-%m-%d").date()
        except:
            continue
        status = compute_indicator_status_for_period(start_date, end_date, news_events)
        if status is not None:
            period_status[period_key] = (status, ret)
            if len(period_status) <= 10:
                print(f"   دوره {period_key} -> بازده {ret:.2f}% , وضعیت خبری: { {k:v for k,v in status.items() if v is not None} }")
    
    if not period_status:
        print("⚠️ هیچ دوره‌ای وضعیت خبری معتبر نداشت.")
        return
    print(f"✅ تعداد دوره‌های دارای وضعیت خبری: {len(period_status)}")
    
    # ---------- مرحله 6: تولید همه ترکیب‌های شاخص و آستانه ----------
    all_combinations = []
    for r in range(1, len(INDICATORS) + 1):
        for subset_tuple in itertools.combinations(INDICATORS, r):
            for thr in THRESHOLDS:
                all_combinations.append((thr, list(subset_tuple)))
    print(f"🔢 تعداد کل ترکیب‌های ممکن: {len(all_combinations)}")
    
    total = len(all_combinations)
    start_idx = max(0, chunk_start)
    end_idx = min(total, chunk_end) if chunk_end is not None else total
    selected = all_combinations[start_idx:end_idx]
    print(f"🎯 پردازش چانک: {start_idx} تا {end_idx} (تعداد {len(selected)})")
    
    csv_rows = []
    for thr, subset in selected:
        pattern_counts = defaultdict(lambda: {'total': 0, 'loss': 0, 'profit': 0, 'periods': []})
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
                'دوره‌ها': '|'.join(counts['periods'])
            })
    
    # ---------- مرحله 7: ذخیره CSV ----------
    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    if not csv_rows:
        with open(output_path, "w", encoding="utf-8-sig", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=[
                'آستانه', 'تعداد_شاخص‌ها', 'لیست_شاخص‌ها', 'الگوی_وضعیت',
                'تعداد_کل_وقوع', 'تعداد_ضررده', 'تعداد_سودده',
                'درصد_ضررده', 'درصد_سودده', 'نسبت_شانس', 'دوره‌ها'
            ])
            writer.writeheader()
        print(f"⚠️ هیچ ردیفی یافت نشد. فایل خالی ایجاد شد: {output_path}")
    else:
        with open(output_path, "w", encoding="utf-8-sig", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=csv_rows[0].keys())
            writer.writeheader()
            writer.writerows(csv_rows)
        print(f"✅ {len(csv_rows)} ردیف برای {start_idx} تا {end_idx} ذخیره شد: {output_path}")

# ================================ main ================================
def main():
    parser = argparse.ArgumentParser(description="تحلیل ترکیبی الگوهای خبری (combo_10day)")
    parser.add_argument("--trades-json", required=True, help="مسیر فایل یکپارچه معاملات (all_trades.json)")
    parser.add_argument("--news-dir", required=True, help="پوشه حاوی فایل‌های CSV اخبار (data/news)")
    parser.add_argument("--interval", required=True, help="نوع بازه (مثلاً fixed_5d, CPI_post_7d, PPI_pre_3d)")
    parser.add_argument("--chunk-start", type=int, default=0, help="شروع چانک ترکیب‌ها")
    parser.add_argument("--chunk-end", type=int, default=None, help="پایان چانک ترکیب‌ها")
    parser.add_argument("--strategy-folder", required=True, help="نام پوشه استراتژی (برای نام خروجی)")
    parser.add_argument("--coin", required=True, help="جفت ارز (مثلاً BTCUSDT)")
    parser.add_argument("--model", required=True, help="مدل محاسبه (برای نام خروجی)")
    
    args = parser.parse_args()
    
    output_file = f"{args.strategy_folder}_{args.coin}_{args.interval}_{args.model}.csv"
    output_path = os.path.join(os.getcwd(), output_file)
    process_analysis(
        args.trades_json, args.news_dir, args.interval, args.coin,
        args.chunk_start, args.chunk_end, output_path
    )

if __name__ == "__main__":
    main()
