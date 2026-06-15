#!/usr/bin/env python3
# calculator_returns.py - محاسبه بازده واقعی و ویژه برای فایل‌های نتایج
import os
import sys
import json
import argparse
from decimal import Decimal

def safe_percentage(value):
    if value is None:
        return 0.0
    if isinstance(value, (int, float)):
        return float(value)
    cleaned = str(value).replace('%', '').replace('+', '').strip()
    if cleaned == "":
        return 0.0
    try:
        return float(cleaned)
    except:
        return 0.0

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

def calculate_real_return(distribution_data):
    total = Decimal("0")
    for item in distribution_data or []:
        percent = safe_percentage(item.get("درصد_دقیق"))
        count = item.get("تعداد_معاملات", 0) or 0
        total += Decimal(str(percent)) * Decimal(str(count))
    return float(total)

def calculate_special_return(distribution_data, move_percents):
    total = Decimal("0")
    for item in distribution_data or []:
        percent = safe_percentage(item.get("درصد_دقیق"))
        count = item.get("تعداد_معاملات", 0) or 0
        rounded = apply_special_rounding(percent, move_percents)
        total += Decimal(str(rounded)) * Decimal(str(count))
    return float(total)

def process_file(file_path, move_percents_list):
    with open(file_path, "r", encoding="utf-8") as f:
        data = json.load(f)
    distribution = data.get("توزیع_دقیق_سود_ضرر", [])
    real = calculate_real_return(distribution)
    special = calculate_special_return(distribution, move_percents_list or [])
    return {"real": real, "special": special}

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--strategies-json", required=True, help="خروجی loader (strategies_metadata.json)")
    parser.add_argument("--results-base", required=True, help="مسیر پایه نتایج (شامل 1 و 2)")
    parser.add_argument("--output", required=True, help="فایل خروجی کش returns_cache.json")
    args = parser.parse_args()

    with open(args.strategies_json, "r", encoding="utf-8") as f:
        data = json.load(f)
    strategies = data["strategies"]
    cache = {}
    for s in strategies:
        folder = s["folder"]
        group = s["group"]
        move = s.get("move_percents")
        for fname in s["result_files"]:
            file_path = os.path.join(args.results_base, group, folder, fname)
            if not os.path.exists(file_path):
                continue
            key = f"{group}_{folder}_{fname}"
            try:
                res = process_file(file_path, move)
                cache[key] = res
            except Exception as e:
                print(f"⚠️ خطا در {file_path}: {e}")
    os.makedirs(os.path.dirname(args.output), exist_ok=True)
    with open(args.output, "w", encoding="utf-8") as f:
        json.dump(cache, f, indent=2)
    print(f"✅ کش بازده برای {len(cache)} فایل در {args.output} ذخیره شد.")

if __name__ == "__main__":
    main()
