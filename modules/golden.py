#!/usr/bin/env python3
# golden.py - ترکیب طلایی: یافتن استراتژی‌های برتر در شرایط خبری مشابه
import os
import sys
import json
import csv
import argparse
import statistics
import pickle
import re
from datetime import datetime, timedelta
from collections import defaultdict

# ================================ ثابت‌ها ================================
INDICATORS = ['CPI m/m', 'Core CPI m/m', 'PPI m/m', 'Core PPI m/m', 'FOMC', 'CPI y/y']
THRESHOLD = 0.1
PROFIT_THRESHOLD = 80.0   # درصد سوددهی لازم برای معرفی استراتژی

# ================================ توابع کمکی ================================
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

def load_all_strategies_results(results_base_dir, returns_cache_json):
    """
    بارگذاری همه فایل‌های نتایج از تمام استراتژی‌ها (گروه‌های 1 و 2)
    و ترکیب با کش بازده.
    خروجی: لیستی از دیکشنری‌ها شامل اطلاعات هر فایل (folder_name, group, period_name, special_return, real_return)
    """
    all_results = []
    returns_cache = {}
    if os.path.exists(returns_cache_json):
        with open(returns_cache_json, "r", encoding="utf-8") as f:
            returns_cache = json.load(f)
    for group in ["1", "2"]:
        group_path = os.path.join(results_base_dir, group)
        if not os.path.isdir(group_path):
            continue
        for folder in os.listdir(group_path):
            folder_path = os.path.join(group_path, folder)
            if not os.path.isdir(folder_path):
                continue
            for fname in os.listdir(folder_path):
                if not fname.endswith(".json") or fname == "1.json":
                    continue
                file_path = os.path.join(folder_path, fname)
                try:
                    with open(file_path, "r", encoding="utf-8") as f:
                        data = json.load(f)
                    info = data.get("اطلاعات_فایل", {}) or {}
                    period_name = info.get("نام_فایل", "")
                    # بازده از کش
                    key = f"{group}_{folder}_{fname}"
                    if key in returns_cache:
                        special_ret = returns_cache[key].get("special", 0)
                        real_ret = returns_cache[key].get("real", 0)
                    else:
                        # fallback: از خود فایل
                        real_ret = float(info.get("بازدهی_کل", "0%").replace("%", ""))
                        special_ret = real_ret
                    all_results.append({
                        "folder_name": folder,
                        "group": group,
                        "file_name": fname,
                        "period_name": period_name,
                        "real_return": real_ret,
                        "special_rounded_return": special_ret,
                    })
                except:
                    continue
    return all_results

def compute_fingerprint(events, indicators, thr):
    """
    محاسبه اثر انگشت خبری بر اساس لیست رویدادها.
    خروجی رشته‌ای مثل "CPI m/m:Bad | Core CPI m/m:Neutral"
    """
    parts = []
    for ind in indicators:
        evs = [e for e in events if e["indicator"] == ind]
        if not evs:
            continue
        diffs = [e["actual"] - e["forecast"] for e in evs if e["actual"] is not None and e["forecast"] is not None]
        if not diffs:
            continue
        avg_diff = statistics.mean(diffs)
        if avg_diff > thr:
            status = 'Bad'
        elif avg_diff < -thr:
            status = 'Good'
        else:
            status = 'Neutral'
        parts.append(f"{ind}:{status}")
    return " | ".join(sorted(parts)) if parts else "بدون خبر معنادار"

def compute_monthly_records_from_all_results(all_results):
    """
    گروه‌بندی فایل‌ها بر اساس استراتژی و سپس ماه (سه فایل در ماه).
    خروجی: دیکشنری month -> list of records (هر record شامل folder_name, group, total_return, files, start_date, end_date)
    """
    # گروه‌بندی بر اساس استراتژی (folder, group)
    strategy_map = defaultdict(list)
    for r in all_results:
        key = (r["folder_name"], r["group"])
        strategy_map[key].append(r)

    monthly_records = []  # هر عنصر: {year_month, strategy_name, group, total_return, files, start_date, end_date}
    for (folder, group), files in strategy_map.items():
        # گروه‌بندی فایل‌های این استراتژی بر اساس ماه شروع
        monthly_files = defaultdict(list)
        for f in files:
            start = extract_start_date_from_period(f["period_name"])
            if not start:
                continue
            ym = start[:7]
            monthly_files[ym].append(f)
        for ym, flist in monthly_files.items():
            # انتخاب سه فایل با شروع‌های 1، 11، 21
            selected = []
            for day in [1, 11, 21]:
                target = f"{ym}-{day:02d}"
                found = next((f for f in flist if extract_start_date_from_period(f["period_name"]) == target), None)
                if found:
                    selected.append(found)
            if len(selected) != 3:
                continue
            total_return = sum(f["special_rounded_return"] for f in selected) if group == "1" else sum(f["real_return"] for f in selected)
            start_dates = [extract_start_date_from_period(f["period_name"]) for f in selected]
            end_dates = [extract_end_date_from_period(f["period_name"]) for f in selected]
            monthly_records.append({
                "year_month": ym,
                "strategy_name": folder,
                "group": group,
                "total_return": total_return,
                "files": [f["file_name"] for f in selected],
                "start_date": min(start_dates),
                "end_date": max(end_dates),
            })
    return monthly_records

def compute_file_based_records(all_results):
    """
    برای تحلیل 10 روزه، مستقیماً فایل‌ها را برمی‌گرداند (هر فایل یک بازه 10 روزه)
    """
    records = []
    for r in all_results:
        start = extract_start_date_from_period(r["period_name"])
        end = extract_end_date_from_period(r["period_name"])
        if not start or not end:
            continue
        ret = r["special_rounded_return"] if r["group"] == "1" else r["real_return"]
        records.append({
            "strategy_name": r["folder_name"],
            "group": r["group"],
            "file_name": r["file_name"],
            "return": ret,
            "start_date": start,
            "end_date": end,
        })
    return records

# ================================ تحلیل طلایی (ماهانه) ================================
def golden_monthly_analysis(monthly_records, news_events, output_path):
    """
    برای هر ماه، اثر انگشت خبری را محاسبه کرده و سپس استراتژی‌هایی که در گذشته
    در شرایط مشابه ≥ PROFIT_THRESHOLD سودده بوده‌اند را معرفی می‌کند.
    """
    # گروه‌بندی ماه‌ها بر اساس year_month
    months_dict = defaultdict(list)
    for rec in monthly_records:
        months_dict[rec["year_month"]].append(rec)

    output_rows = []
    for ym, recs in months_dict.items():
        # از اولین رکورد برای استخراج بازه (همه رکوردهای همان ماه بازه یکسان دارند)
        sample = recs[0]
        month_events = find_events_in_range(news_events, sample["start_date"], sample["end_date"])
        month_fingerprint = compute_fingerprint(month_events, INDICATORS, THRESHOLD)

        # برای هر استراتژی در این ماه، تاریخچه را بررسی می‌کنیم
        best_strategies = []
        for rec in recs:
            strat_name = rec["strategy_name"]
            group = rec["group"]
            # پیدا کردن تمام ماه‌های تاریخی (غیر از ماه جاری) با اثر انگشت مشابه
            matching_records = []
            for other_ym, other_recs in months_dict.items():
                if other_ym == ym:
                    continue
                for ori in other_recs:
                    if ori["strategy_name"] != strat_name:
                        continue
                    other_events = find_events_in_range(news_events, ori["start_date"], ori["end_date"])
                    other_fp = compute_fingerprint(other_events, INDICATORS, THRESHOLD)
                    if other_fp == month_fingerprint:
                        matching_records.append(ori)
            if matching_records:
                profit_count = sum(1 for mr in matching_records if mr["total_return"] > 0)
                profit_pct = (profit_count / len(matching_records)) * 100
                if profit_pct >= PROFIT_THRESHOLD:
                    best_strategies.append({
                        "name": f"{strat_name} (گروه {group})",
                        "profit_pct": profit_pct,
                        "samples": len(matching_records),
                        "return_this_month": rec["total_return"]
                    })
        if best_strategies:
            best_strategies.sort(key=lambda x: (-x["profit_pct"], -x["samples"]))
            strategies_str = " | ".join([f"{s['name']} ({s['profit_pct']:.0f}%, n={s['samples']})" for s in best_strategies])
            output_rows.append({
                "ماه": ym,
                "اثر_انگشت_خبری": month_fingerprint,
                "استراتژی‌های_برتر": strategies_str,
                "تعداد": len(best_strategies)
            })
        else:
            output_rows.append({
                "ماه": ym,
                "اثر_انگشت_خبری": month_fingerprint,
                "استراتژی‌های_برتر": "-",
                "تعداد": 0
            })
    # نوشتن CSV
    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    with open(output_path, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=["ماه", "اثر_انگشت_خبری", "استراتژی‌های_برتر", "تعداد"])
        writer.writeheader()
        writer.writerows(output_rows)
    print(f"✅ تحلیل ماهانه ترکیب طلایی در {output_path} ذخیره شد.")

# ================================ تحلیل طلایی (۱۰ روزه) ================================
def golden_daily_analysis(file_records, news_events, output_path):
    """
    مشابه ماهانه ولی بر اساس فایل‌ها (بازه ۱۰ روزه)
    """
    output_rows = []
    # برای هر فایل، اثر انگشت خبری و بازده را محاسبه می‌کنیم
    file_info = []
    for rec in file_records:
        start = rec["start_date"]
        end = rec["end_date"]
        events = find_events_in_range(news_events, start, end)
        fingerprint = compute_fingerprint(events, INDICATORS, THRESHOLD)
        file_info.append({
            "strategy_name": rec["strategy_name"],
            "group": rec["group"],
            "return": rec["return"],
            "fingerprint": fingerprint,
            "file_name": rec["file_name"],
        })
    # گروه‌بندی بر اساس اثر انگشت
    fp_map = defaultdict(list)
    for fi in file_info:
        fp_map[fi["fingerprint"]].append(fi)
    # برای هر فایل (یا هر اثر انگشت) استراتژی‌های برتر را بیاب
    processed_files = set()
    for fi in file_info:
        key = (fi["strategy_name"], fi["group"], fi["fingerprint"])
        if key in processed_files:
            continue
        processed_files.add(key)
        # پیدا کردن سایر فایل‌ها با همان اثر انگشت (غیر از فایل جاری)
        same_fp = [f for f in fp_map.get(fi["fingerprint"], []) if not (f["strategy_name"] == fi["strategy_name"] and f["group"] == fi["group"])]
        if not same_fp:
            continue
        # محاسبه درصد سوددهی برای هر استراتژی در این شرایط خبری
        strat_perf = defaultdict(lambda: {"profit": 0, "total": 0})
        for f in same_fp:
            key2 = (f["strategy_name"], f["group"])
            strat_perf[key2]["total"] += 1
            if f["return"] > 0:
                strat_perf[key2]["profit"] += 1
        best = []
        for (sname, sgroup), cnt in strat_perf.items():
            pct = (cnt["profit"] / cnt["total"]) * 100
            if pct >= PROFIT_THRESHOLD:
                best.append(f"{sname} (گروه {sgroup}) : {pct:.0f}% (n={cnt['total']})")
        if best:
            output_rows.append({
                "فایل": f"{fi['strategy_name']}/{fi['file_name']}",
                "اثر_انگشت_خبری": fi["fingerprint"],
                "استراتژی‌های_برتر_در_این_شرایط": " | ".join(best)
            })
    # نوشتن CSV
    with open(output_path, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=["فایل", "اثر_انگشت_خبری", "استراتژی‌های_برتر_در_این_شرایط"])
        writer.writeheader()
        writer.writerows(output_rows)
    print(f"✅ تحلیل ۱۰ روزه ترکیب طلایی در {output_path} ذخیره شد.")

# ================================ اصلی ================================
def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--results-base", required=True, help="مسیر پایه نتایج بکتست (شامل 1 و 2)")
    parser.add_argument("--returns-cache", required=True, help="مسیر کش بازده (JSON)")
    parser.add_argument("--news-pickle", required=True, help="مسیر فایل pickle اخبار")
    parser.add_argument("--output-dir", required=True, help="پوشه خروجی نهایی")
    parser.add_argument("--mode", choices=["monthly", "daily", "both"], default="both", help="نوع تحلیل")
    args = parser.parse_args()

    print("🔄 بارگذاری اخبار...")
    news = load_news_data(args.news_pickle)
    print("🔄 بارگذاری همه نتایج استراتژی‌ها...")
    all_results = load_all_strategies_results(args.results_base, args.returns_cache)
    print(f"تعداد کل فایل‌ها: {len(all_results)}")

    if args.mode in ["monthly", "both"]:
        monthly_recs = compute_monthly_records_from_all_results(all_results)
        print(f"تعداد ماه‌های قابل تحلیل: {len(monthly_recs)}")
        out_monthly = os.path.join(args.output_dir, "ترکیب_طلایی_ماهانه.csv")
        golden_monthly_analysis(monthly_recs, news, out_monthly)

    if args.mode in ["daily", "both"]:
        file_recs = compute_file_based_records(all_results)
        print(f"تعداد فایل‌های 10 روزه: {len(file_recs)}")
        out_daily = os.path.join(args.output_dir, "ترکیب_طلایی_10روزه.csv")
        golden_daily_analysis(file_recs, news, out_daily)

    print("✅ ماژول golden پایان یافت.")

if __name__ == "__main__":
    main()
