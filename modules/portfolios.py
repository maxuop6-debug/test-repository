#!/usr/bin/env python3
# portfolios.py - یافتن سبدهای مکمل بهینه با حداکثر نرخ بقا
import os
import sys
import json
import csv
import argparse
import itertools
from collections import defaultdict

# ================================ توابع کمکی ================================
def extract_start_date_from_period(period_str):
    import re
    match = re.search(r'(\d{4}-\d{2}-\d{2})_', period_str)
    return match.group(1) if match else None

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
                    key = f"{group}_{folder}_{fname}"
                    if key in returns_cache:
                        special_ret = returns_cache[key].get("special", 0)
                        real_ret = returns_cache[key].get("real", 0)
                    else:
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

def compute_strategy_monthly_matrix(all_results):
    """
    برای هر استراتژی (folder + group) یک دیکشنری از بازده ماهانه بر اساس year_month می‌سازد.
    خروجی: strategy_monthly = { strategy_key: { year_month: return } }
    همچنین لیست همه ماه‌های موجود را برمی‌گرداند.
    """
    strategy_monthly = defaultdict(dict)
    all_months = set()
    for r in all_results:
        start = extract_start_date_from_period(r["period_name"])
        if not start:
            continue
        ym = start[:7]   # YYYY-MM
        all_months.add(ym)
        key = f"{r['folder_name']} (گروه {r['group']})"
        ret = r["special_rounded_return"] if r["group"] == "1" else r["real_return"]
        strategy_monthly[key][ym] = ret
    all_months = sorted(all_months)
    return strategy_monthly, all_months

def build_binary_matrix(strategy_monthly, all_months):
    """
    ساخت ماتریس باینری (سودده = 1، ضررده = 0) برای هر استراتژی.
    خروجی: dict strategy -> list of 0/1 به ترتیب months.
    """
    matrix = {}
    for strat, monthly_data in strategy_monthly.items():
        vec = []
        for ym in all_months:
            ret = monthly_data.get(ym)
            if ret is None:
                vec.append(None)   # داده ناموجود
            else:
                vec.append(1 if ret > 0 else 0)
        matrix[strat] = vec
    return matrix, all_months

def calculate_survival_rate(combo_strategies, matrix, all_months):
    """
    محاسبه نرخ بقا برای ترکیبی از استراتژی‌ها (حداقل یکی سودده باشد).
    همچنین میانگین بازده ماهانه را محاسبه می‌کند.
    بازگشت: (survival_rate, avg_return, valid_months)
    """
    survival_count = 0
    total_return_sum = 0.0
    valid_months = 0
    for month_idx in range(len(all_months)):
        # بررسی آیا همه استراتژی‌های ترکیب داده دارند؟
        values = []
        returns = []
        for strat in combo_strategies:
            v = matrix[strat][month_idx]
            if v is None:
                break
            values.append(v)
            # بازده واقعی (برای میانگین) - باید از دیکشنری اصلی استخراج کنیم
            # برای سادگی فرض می‌کنیم بازده را از همان جا داریم ولی فعلاً صرفاً باینری
            # برای محاسبه دقیق میانگین بازده، نیاز به نگهداری بازده واقعی داریم.
        if len(values) != len(combo_strategies):
            continue
        valid_months += 1
        if any(v == 1 for v in values):
            survival_count += 1
        # میانگین بازده: اگر بخواهیم واقعی را محاسبه کنیم باید بازده عددی را هم ذخیره کنیم.
        # فعلاً ساده می‌گیریم.
    survival_rate = (survival_count / valid_months) * 100 if valid_months > 0 else 0
    return survival_rate, 0.0, valid_months

def calculate_avg_return(combo_strategies, strategy_monthly, all_months):
    """محاسبه میانگین بازده ماهانه ترکیب (میانگین ساده بازده‌ها)"""
    total_return = 0.0
    valid_months = 0
    for ym in all_months:
        returns = []
        for strat in combo_strategies:
            ret = strategy_monthly[strat].get(ym)
            if ret is None:
                break
            returns.append(ret)
        if len(returns) != len(combo_strategies):
            continue
        valid_months += 1
        total_return += sum(returns) / len(returns)
    avg_return = total_return / valid_months if valid_months > 0 else 0
    return avg_return

def find_optimal_portfolios(strategy_monthly, matrix, all_months, top_n=15):
    """
    بررسی تمام ترکیب‌های ۲، ۳ و ۴ استراتژی و یافتن بهترین‌ها بر اساس نرخ بقا و سپس میانگین بازده.
    """
    strategies = list(strategy_monthly.keys())
    if len(strategies) < 2:
        return []
    results = []
    total_combos = 0
    for size in [2, 3, 4]:
        for combo in itertools.combinations(strategies, size):
            total_combos += 1
            # نرخ بقا
            survival = calculate_survival_rate(combo, matrix, all_months)[0]
            # میانگین بازده
            avg_ret = calculate_avg_return(combo, strategy_monthly, all_months)
            results.append({
                "combo": combo,
                "size": size,
                "survival_rate": survival,
                "avg_return": avg_ret,
            })
    # مرتب‌سازی بر اساس نرخ بقا (نزولی) سپس میانگین بازده (نزولی)
    results.sort(key=lambda x: (-x["survival_rate"], -x["avg_return"]))
    return results[:top_n]

def save_portfolios_csv(portfolios, output_path):
    """ذخیره سبدهای برتر در فایل CSV"""
    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    with open(output_path, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["رتبه", "تعداد_استراتژی", "اعضای_سبد", "نرخ_بقا_%", "میانگین_بازدهی_ماهانه_%"])
        for i, p in enumerate(portfolios, 1):
            writer.writerow([
                i,
                p["size"],
                " | ".join(p["combo"]),
                f"{p['survival_rate']:.1f}",
                f"{p['avg_return']:.3f}"
            ])
    print(f"✅ {len(portfolios)} سبد مکمل برتر در {output_path} ذخیره شد.")

# ================================ اصلی ================================
def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--results-base", required=True, help="مسیر پایه نتایج بکتست (شامل 1 و 2)")
    parser.add_argument("--returns-cache", required=True, help="مسیر کش بازده (JSON)")
    parser.add_argument("--output-dir", required=True, help="پوشه خروجی نهایی")
    parser.add_argument("--top", type=int, default=15, help="تعداد سبدهای برتر")
    args = parser.parse_args()

    print("🔄 بارگذاری نتایج استراتژی‌ها...")
    all_results = load_all_strategies_results(args.results_base, args.returns_cache)
    print(f"تعداد کل فایل‌ها: {len(all_results)}")

    print("🔄 ساخت ماتریس ماهانه...")
    strategy_monthly, all_months = compute_strategy_monthly_matrix(all_results)
    print(f"تعداد استراتژی‌های فعال: {len(strategy_monthly)}")
    print(f"تعداد ماه‌های مشترک: {len(all_months)}")

    if len(strategy_monthly) < 2:
        print("❌ حداقل دو استراتژی برای تحلیل مکمل نیاز است.")
        sys.exit(1)

    matrix, _ = build_binary_matrix(strategy_monthly, all_months)

    print("🔄 جستجوی سبدهای بهینه...")
    portfolios = find_optimal_portfolios(strategy_monthly, matrix, all_months, top_n=args.top)
    if not portfolios:
        print("⚠️ هیچ سبدی یافت نشد.")
        sys.exit(0)

    out_path = os.path.join(args.output_dir, "سبدهای_مکمل_برتر.csv")
    save_portfolios_csv(portfolios, out_path)

    print("✅ ماژول portfolios پایان یافت.")

if __name__ == "__main__":
    main()
