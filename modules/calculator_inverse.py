#!/usr/bin/env python3
# calculator_inverse.py - ساخت نسخه معکوس معاملات برای هر فایل
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

def calculate_inverse_distribution(original_dist, stop_loss_initial, max_gain):
    inv_sl = abs(stop_loss_initial)
    inv_dist = []
    for item in original_dist or []:
        pct = safe_percentage(item.get("درصد_دقیق"))
        cnt = item.get("تعداد_معاملات", 0) or 0
        if pct > 0:
            inv_pct = -inv_sl
        elif pct < 0:
            gain = abs(pct)
            if max_gain is not None:
                gain = min(gain, max_gain)
            inv_pct = gain
        else:
            inv_pct = 0.0
        inv_dist.append({
            "درصد_دقیق": f"{inv_pct:+.4f}%".replace("+-", "-"),
            "تعداد_معاملات": cnt
        })
    return inv_dist

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--strategies-json", required=True)
    parser.add_argument("--results-base", required=True)
    parser.add_argument("--stoploss-cache", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()

    with open(args.strategies_json, "r", encoding="utf-8") as f:
        data = json.load(f)
    strategies = data["strategies"]
    with open(args.stoploss_cache, "r", encoding="utf-8") as f:
        sl_cache = json.load(f)

    inverse_cache = {}  # key: group_folder_file, value: inverse_distribution
    for s in strategies:
        folder = s["folder"]
        group = s["group"]
        key_sl = f"{group}_{folder}"
        sl_info = sl_cache.get(key_sl, {"stop_loss": -2.0, "move_percents": None})
        stop_loss = sl_info["stop_loss"]
        max_gain = max(sl_info["move_percents"]) if sl_info["move_percents"] else None
        for fname in s["result_files"]:
            orig_path = os.path.join(args.results_base, group, folder, fname)
            if not os.path.exists(orig_path):
                continue
            with open(orig_path, "r", encoding="utf-8") as f:
                orig_data = json.load(f)
            dist = orig_data.get("توزیع_دقیق_سود_ضرر", [])
            inv_dist = calculate_inverse_distribution(dist, stop_loss, max_gain)
            inv_key = f"{group}_{folder}_{fname}"
            inverse_cache[inv_key] = inv_dist
    os.makedirs(os.path.dirname(args.output), exist_ok=True)
    with open(args.output, "w", encoding="utf-8") as f:
        json.dump(inverse_cache, f, indent=2)
    print(f"✅ کش معکوس برای {len(inverse_cache)} فایل در {args.output} ذخیره شد.")

if __name__ == "__main__":
    main()
