#!/usr/bin/env python3
# combo_monthly.py - تحلیل ترکیبی الگوهای خبری در بازه ماهانه (هر ماه ۳ فایل)
import os
import sys
import json
import csv
import argparse
import itertools
import statistics
import pickle
import re
from datetime import datetime
from collections import defaultdict

# ================================ ثابت‌ها ================================
INDICATORS = ['CPI m/m', 'Core CPI m/m', 'PPI m/m', 'Core PPI m/m', 'FOMC', 'CPI y/y']
THRESHOLDS = [0.0, 0.1, 0.2, 0.3]

# ================================ توابع کمکی (همانند combo_10day) ================================
def extract_start_date_from_period(period_str):
    match = re.search(r'(\d{4}-\d{2}-\d{2})_', period_str)
    return match.group(1) if match else None

def extract_end_date_from_period(period_str):
    match = re.search(r'_(\d{4}-\d{2}-\d{2})$', period_str)
    return match.group(1) if match else None

def find_events_in_range(events, start_date_str, end_date_str):
    try:
        start = datetime.strptime(start_date_str, "%Y-%m-%d").date()
        end = datetime.strptime(end_date_str, "%Y-%m-%d").date()
    except:
        return []
    return [ev for ev in events if start <= ev["date"] <= end]

def load_news_data(pickle_path):
    with open(pickle_path, "rb") as f:
        return pickle.load(f)

def load_strategy_results(base_results_dir, group, folder_name):
    folder_path = os.path.join(base_results_dir, group, folder_name)
    if not os.path.isdir(folder_path):
        return []
    results = []
    for fname in os.listdir(folder_path):
        if fname.endswith(".json") and fname != "1.json":
            file_path = os.path.join(folder_path, fname)
            with open(file_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            info = data.get("اطلاعات_فایل", {}) or {}
            stats = data.get("آمار_کلی_معاملات", {}) or {}
            risk = data.get("آنالیز_ریسک", {}) or {}
            period_name = info.get("نام_فایل", "") or ""
            results.append({
                "file_name": fname,
                "period_name": period_name,
                "total_trades": stats.get("تعداد_کل_معاملات", 0) or 0,
                "win_rate": float(stats.get("نرخ_برد", "0%").replace("%", "")),
                "real_return": float(info.get("بازدهی_کل", "0%").replace("%", "")),
                "special_rounded_return": 0.0,
            })
    return results

def get_returns_cache(returns_json_path):
    with open(returns_json_path, "r", encoding="utf-8") as f:
        return json.load(f)

def compute_indicator_status_for_file(file_obj, news_events):
    start = extract_start_date_from_period(file_obj["period_name"])
    end = extract_end_date_from_period(file_obj["period_name"])
    if not start or not end:
        return None
    events_in_range = find_events_in_range(news_events, start, end)
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

# ================================ توابع ویژه ماهانه ================================
def compute_monthly_records_from_files(file_objects, group):
    """
    با توجه به فایل‌های یک استراتژی، رکوردهای ماهانه (سه فایل در ماه) را استخراج می‌کند.
    مشابه compute_monthly_records_for_strategy در کد اصلی.
    """
    monthly_files = {}
    for r in file_objects:
        start = extract_start_date_from_period(r["period_name"])
        if not start:
            continue
        ym = start[:7]  # YYYY-MM
        monthly_files.setdefault(ym, []).append(r)

    monthly_records = []
    for ym, files in monthly_files.items():
        selected = []
        for day in [1, 11, 21]:
            target = f"{ym}-{day:02d}"
            found = next((f for f in files if extract_start_date_from_period(f["period_name"]) == target), None)
            if found:
                selected.append(found)
        if len(selected) != 3:
            continue
        total_return = sum(f["special_rounded_return"] for f in selected) if group == "1" else sum(f["real_return"] for f in selected)
        monthly_records.append({
            "year_month": ym,
            "total_return": total_return,
            "files": [f["file_name"] for f in selected],
            "file_objects": selected,
            "group": group,
            "start_date": min(extract_start_date_from_period(f["period_name"]) for f in selected),
            "end_date": max(extract_end_date_from_period(f["period_name"]) for f in selected)
        })
    monthly_records.sort(key=lambda x: x["year_month"])
    return monthly_records

def compute_indicator_status_for_month(month_record, news_events):
    """محاسبه وضعیت شاخص‌ها برای کل بازه ماهانه (ادغام سه فایل)"""
    start = month_record["start_date"]
    end = month_record["end_date"]
    events_in_range = find_events_in_range(news_events, start, end)
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

def process_strategy_combo_monthly(strategy_files, news_events, group, output_path):
    """
    تحلیل ترکیبی ماهانه: برای هر ماه (سه فایل)، وضعیت شاخص‌ها را گرفته و 
    ترکیب الگوها را با درصد ضرردهی ماهانه محاسبه می‌کند.
    """
    # ابتدا رکوردهای ماهانه را بساز
    monthly_records = compute_monthly_records_from_files(strategy_files, group)
    if not monthly_records:
        print("⚠️ هیچ رکورد ماهانه معتبری یافت نشد.")
        return

    # کش وضعیت ماهانه
    month_status = []
    for rec in monthly_records:
        status = compute_indicator_status_for_month(rec, news_events)
        if status is not None:
            month_status.append({
                "record": rec,
                "status": status
            })
    if not month_status:
        print("⚠️ هیچ وضعیت خبری ماهانه معتبری یافت نشد.")
        return

    # همه ترکیب‌های ممکن (مجموعه غیرتهی شاخص‌ها × آستانه‌ها)
    all_combinations = []
    for r in range(1, len(INDICATORS) + 1):
        for subset_tuple in itertools.combinations(INDICATORS, r):
            for thr in THRESHOLDS:
                all_combinations.append((thr, list(subset_tuple)))

    csv_rows = []
    for thr, subset in all_combinations:
        pattern_counts = defaultdict(lambda: {'total': 0, 'loss': 0, 'profit': 0, 'months': []})
        for item in month_status:
            status_dict = item["status"]
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
            ret = item["record"]["total_return"]
            pattern_counts[pattern]['total'] += 1
            pattern_counts[pattern]['months'].append(item["record"]["year_month"])
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

    if not csv_rows:
        print("⚠️ هیچ ردیفی برای این استراتژی تولید نشد.")
        return

    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    with open(output_path, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=csv_rows[0].keys())
        writer.writeheader()
        writer.writerows(csv_rows)
    print(f"✅ {len(csv_rows)} ردیف برای ماهانه ذخیره شد: {output_path}")

# ================================ ورودی اصلی ================================
def main():
    parser = argparse.ArgumentParser()
    # آرگومان‌های سازگار با workflow (مثل combo_10day)
    parser.add_argument("--strategy-folder", required=True)
    parser.add_argument("--trades-json", default=None, help="استفاده نمی‌شود (برای سازگاری با workflow)")
    parser.add_argument("--news-dir", default=None, help="استفاده نمی‌شود (برای سازگاری با workflow)")
    parser.add_argument("--coin", default=None, help="استفاده نمی‌شود (برای سازگاری با workflow)")
    parser.add_argument("--interval", default=None, help="استفاده نمی‌شود (برای سازگاری با workflow)")
    parser.add_argument("--model", default=None, help="استفاده نمی‌شود (برای سازگاری با workflow)")
    parser.add_argument("--chunk-start", type=int, default=None, help="استفاده نمی‌شود (برای سازگاری با workflow)")
    parser.add_argument("--chunk-end", type=int, default=None, help="استفاده نمی‌شود (برای سازگاری با workflow)")
    # آرگومان‌های اصلی combo_monthly (اختیاری با مقادیر پیش‌فرض)
    parser.add_argument("--group", default="1", choices=["1", "2"])
    parser.add_argument("--results-base", default="/tmp/prep/results_base")
    parser.add_argument("--news-pickle", default="/tmp/news.pkl")
    parser.add_argument("--returns-cache", default="/tmp/returns_cache.json")
    parser.add_argument("--output-dir", default="/tmp/combo_monthly_output")
    args = parser.parse_args()

    # اگر news-pickle وجود ندارد، با پیام واضح و exit 0 خارج می‌شویم
    if not os.path.isfile(args.news_pickle):
        print(f"warning combo_monthly: news-pickle not found at {args.news_pickle}. skipping.")
        sys.exit(0)

    if not os.path.isdir(args.results_base):
        print(f"warning combo_monthly: results-base not found at {args.results_base}. skipping.")
        sys.exit(0)

    print("loading news...")
    news_events = load_news_data(args.news_pickle)
    print("loading strategy results...")
    strategy_files = load_strategy_results(args.results_base, args.group, args.strategy_folder)
    if not strategy_files:
        print(f"warning combo_monthly: no result files for {args.strategy_folder}. skipping.")
        sys.exit(0)

    returns_cache = {}
    if os.path.isfile(args.returns_cache):
        returns_cache = get_returns_cache(args.returns_cache)

    for r in strategy_files:
        key = f"{args.group}_{args.strategy_folder}_{r['file_name']}"
        if key in returns_cache:
            r["special_rounded_return"] = returns_cache[key].get("special", r.get("real_return", 0))
            r["real_return"] = returns_cache[key].get("real", r["real_return"])
        else:
            r["special_rounded_return"] = r.get("real_return", 0)

    os.makedirs(args.output_dir, exist_ok=True)
    coin = args.coin or "ALL"
    interval = args.interval or "monthly"
    model = args.model or "default"
    out_filename = f"{args.strategy_folder}_{coin}_{interval}_{model}.csv"
    out_path = os.path.join(args.output_dir, out_filename)
    process_strategy_combo_monthly(strategy_files, news_events, args.group, out_path)

    import shutil
    local_out = f"{args.strategy_folder}_{coin}_{interval}_{model}.csv"
    if os.path.isfile(out_path) and os.path.abspath(out_path) != os.path.abspath(local_out):
        shutil.copy(out_path, local_out)
        print(f"output copied to {local_out}")

if __name__ == "__main__":
    main()
