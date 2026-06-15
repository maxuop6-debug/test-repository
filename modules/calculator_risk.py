#!/usr/bin/env python3
# calculator_risk.py - محاسبه حداکثر ضرر متوالی
import os
import sys
import json
import argparse

def calculate_max_consecutive_loss(count, stop_loss_initial):
    count = int(count or 0)
    penalty = count * -0.1
    main_losses = count * float(stop_loss_initial)
    return penalty + main_losses

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

    risk_cache = {}
    for s in strategies:
        folder = s["folder"]
        group = s["group"]
        key_sl = f"{group}_{folder}"
        stop_loss = sl_cache.get(key_sl, {"stop_loss": -2.0})["stop_loss"]
        for fname in s["result_files"]:
            file_path = os.path.join(args.results_base, group, folder, fname)
            if not os.path.exists(file_path):
                continue
            try:
                with open(file_path, "r", encoding="utf-8") as f:
                    file_data = json.load(f)
                risk_info = file_data.get("آنالیز_ریسک", {})
                max_consecutive_count = risk_info.get("بیشترین_ضرر_متوالی", 0) or 0
                max_loss_value = calculate_max_consecutive_loss(max_consecutive_count, stop_loss)
                key = f"{group}_{folder}_{fname}"
                risk_cache[key] = {
                    "max_consecutive_loss": max_loss_value,
                    "max_consecutive_count": max_consecutive_count
                }
            except Exception as e:
                print(f"⚠️ خطا در {file_path}: {e}")
                continue

    os.makedirs(os.path.dirname(args.output), exist_ok=True)
    with open(args.output, "w", encoding="utf-8") as f:
        json.dump(risk_cache, f, indent=2)
    print(f"✅ کش ریسک برای {len(risk_cache)} فایل در {args.output} ذخیره شد.")

if __name__ == "__main__":
    main()
