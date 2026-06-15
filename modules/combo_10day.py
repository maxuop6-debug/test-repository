#!/usr/bin/env python3
# combo_10day.py - تحلیل ترکیبی الگوهای خبری در بازه‌های ۱۰ روزه (چانک‌بندی شده)
import os
import sys
import json
import csv
import argparse
import itertools
import statistics
import pickle
import re
from datetime import datetime, timedelta
from collections import defaultdict

# ================================ ثابت‌ها ================================
INDICATORS = ['CPI m/m', 'Core CPI m/m', 'PPI m/m', 'Core PPI m/m', 'FOMC', 'CPI y/y']
THRESHOLDS = [0.0, 0.1, 0.2, 0.3]

# ================================ توابع کمکی (از کد اصلی) ================================
def extract_start_date_from_period(period_str):
    """استخراج تاریخ شروع (YYYY-MM-DD) از رشته دوره معاملاتی"""
    match = re.search(r'(\d{4}-\d{2}-\d{2})_', period_str)
    return match.group(1) if match else None

def extract_end_date_from_period(period_str):
    """استخراج تاریخ پایان (YYYY-MM-DD) از رشته دوره معاملاتی"""
    match = re.search(r'_(\d{4}-\d{2}-\d{2})$', period_str)
    return match.group(1) if match else None

def find_events_in_range(events, start_date_str, end_date_str):
    """بازگرداندن رویدادهایی که تاریخ آنها بین start_date و end_date (شامل) است"""
    try:
        start = datetime.strptime(start_date_str, "%Y-%m-%d").date()
        end = datetime.strptime(end_date_str, "%Y-%m-%d").date()
    except:
        return []
    result = []
    for ev in events:
        if start <= ev["date"] <= end:
            result.append(ev)
    return result

# ================================ تابع اصلی تحلیل ================================
def load_news_data(pickle_path):
    """بارگذاری رویدادهای خبری از فایل pickle"""
    with open(pickle_path, "rb") as f:
        return pickle.load(f)

def load_strategies_metadata(json_path):
    """بارگذاری متادیتای استراتژی‌ها (خروجی loader)"""
    with open(json_path, "r", encoding="utf-8") as f:
        return json.load(f)["strategies"]

def load_strategy_results(base_results_dir, group, folder_name):
    """بارگذاری همه فایل‌های JSON یک استراتژی خاص"""
    folder_path = os.path.join(base_results_dir, group, folder_name)
    if not os.path.isdir(folder_path):
        return []
    results = []
    for fname in os.listdir(folder_path):
        if fname.endswith(".json") and fname != "1.json":
            file_path = os.path.join(folder_path, fname)
            with open(file_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            # استخراج اطلاعات مورد نیاز
            info = data.get("اطلاعات_فایل", {}) or {}
            stats = data.get("آمار_کلی_معاملات", {}) or {}
            risk = data.get("آنالیز_ریسک", {}) or {}
            distribution = data.get("توزیع_دقیق_سود_ضرر", []) or []
            period_name = info.get("نام_فایل", "") or ""
            results.append({
                "file_name": fname,
                "period_name": period_name,
                "total_trades": stats.get("تعداد_کل_معاملات", 0) or 0,
                "win_rate": float(stats.get("نرخ_برد", "0%").replace("%", "")),
                "max_drawdown": float(risk.get("حداکثر_افت_سرمایه", "0%").replace("%", "")),
                "profit_loss_ratio": float(risk.get("نسبت_سود_به_ضرر", 0) or 0),
                "sharpe_ratio": float(risk.get("نسبت_شارپ", 0) or 0),
                "real_return": float(info.get("بازدهی_کل", "0%").replace("%", "")),
                "special_rounded_return": 0.0,  # در صورت نیاز از کش بازده بگیریم
            })
    return results

def get_returns_cache(returns_json_path):
    """بازخوانی کش بازده (real و special) برای هر فایل"""
    with open(returns_json_path, "r", encoding="utf-8") as f:
        cache = json.load(f)
    return cache

def compute_indicator_status_for_file(file_obj, news_events):
    """محاسبه وضعیت شاخص‌ها برای یک فایل معین (بر اساس اخبار آن بازه)"""
    start = extract_start_date_from_period(file_obj["period_name"])
    end = extract_end_date_from_period(file_obj["period_name"])
    if not start or not end:
        return None
    events_in_range = find_events_in_range(news_events, start, end)
    # گروه‌بندی بر اساس شاخص
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

def process_strategy_combo_10day(strategy_results, news_events, chunk_start, chunk_end, output_path):
    """
    اجرای تحلیل ترکیبی برای یک استراتژی با محدوده مشخص از ترکیب‌ها.
    خروجی: CSV جزئی ذخیره شده در output_path
    """
    # ابتدا وضعیت خبری هر فایل را کش کنیم
    file_status_cache = {}
    file_return_map = {}
    for r in strategy_results:
        file_name = r["file_name"]
        status = compute_indicator_status_for_file(r, news_events)
        if status is not None:
            file_status_cache[file_name] = status
            file_return_map[file_name] = r.get("special_rounded_return", r.get("real_return", 0))

    # لیست تمام ترکیب‌های ممکن (مجموعه غیرتهی شاخص‌ها × آستانه‌ها)
    all_combinations = []
    for r in range(1, len(INDICATORS) + 1):
        for subset_tuple in itertools.combinations(INDICATORS, r):
            for thr in THRESHOLDS:
                all_combinations.append((thr, list(subset_tuple)))
    
    total = len(all_combinations)
    chunk_start = max(0, chunk_start)
    chunk_end = min(total, chunk_end) if chunk_end is not None else total
    selected_combos = all_combinations[chunk_start:chunk_end]

    csv_rows = []
    for (thr, subset) in selected_combos:
        pattern_counts = defaultdict(lambda: {'total': 0, 'loss': 0, 'profit': 0, 'files': []})
        for file_name, status_dict in file_status_cache.items():
            # بررسی معتبر بودن برای تمام شاخص‌های زیرمجموعه
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
            ret = file_return_map.get(file_name, 0)
            pattern_counts[pattern]['total'] += 1
            pattern_counts[pattern]['files'].append(file_name)
            if ret < 0:
                pattern_counts[pattern]['loss'] += 1
            else:
                pattern_counts[pattern]['profit'] += 1
        # نوشتن ردیف‌ها
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
                'فایل‌ها': '|'.join(counts['files'])
            })
    # ذخیره CSV جزئی
    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    with open(output_path, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=csv_rows[0].keys() if csv_rows else [
            'آستانه', 'تعداد_شاخص‌ها', 'لیست_شاخص‌ها', 'الگوی_وضعیت',
            'تعداد_کل_وقوع', 'تعداد_ضررده', 'تعداد_سودده',
            'درصد_ضررده', 'درصد_سودده', 'نسبت_شانس', 'فایل‌ها'
        ])
        writer.writeheader()
        writer.writerows(csv_rows)
    print(f"✅ {len(csv_rows)} ردیف برای {chunk_start} تا {chunk_end} ذخیره شد: {output_path}")

# ================================ ورودی اصلی ================================
def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--strategy-folder", required=True, help="نام پوشه استراتژی (مثلاً Strategy_A)")
    parser.add_argument("--group", required=True, choices=["1", "2"], help="گروه استراتژی")
    parser.add_argument("--results-base", required=True, help="مسیر پایه نتایج بکتست (دایرکتوری شامل 1 و 2)")
    parser.add_argument("--news-pickle", required=True, help="مسیر فایل pickle اخبار (خروجی loader)")
    parser.add_argument("--returns-cache", required=True, help="مسیر کش بازده (خروجی calculator_returns)")
    parser.add_argument("--output-dir", required=True, help="پوشه خروجی موقت (temp)")
    parser.add_argument("--chunk-start", type=int, default=0, help="ایندکس شروع ترکیب")
    parser.add_argument("--chunk-end", type=int, default=None, help="ایندکس پایان ترکیب")
    args = parser.parse_args()

    # بارگذاری داده‌ها
    print("🔄 بارگذاری اخبار...")
    news_events = load_news_data(args.news_pickle)
    print("🔄 بارگذاری نتایج استراتژی...")
    strategy_files = load_strategy_results(args.results_base, args.group, args.strategy_folder)
    if not strategy_files:
        print("❌ هیچ فایل نتیجه‌ای برای این استراتژی یافت نشد.")
        sys.exit(1)
    # تکمیل اطلاعات با بازده از کش
    returns_cache = get_returns_cache(args.returns_cache)
    for r in strategy_files:
        key = f"{args.group}_{args.strategy_folder}_{r['file_name']}"
        if key in returns_cache:
            r["special_rounded_return"] = returns_cache[key].get("special", r.get("real_return", 0))
            r["real_return"] = returns_cache[key].get("real", r["real_return"])
        else:
            r["special_rounded_return"] = r.get("real_return", 0)

    # مسیر خروجی
    out_filename = f"combo10_{args.strategy_folder}_chunk{args.chunk_start}_{args.chunk_end}.csv"
    out_path = os.path.join(args.output_dir, out_filename)

    # اجرای تحلیل
    process_strategy_combo_10day(strategy_files, news_events, args.chunk_start, args.chunk_end, out_path)
    print("✅ ماژول combo_10day پایان یافت.")

if __name__ == "__main__":
    main()
