#!/usr/bin/env python3
# calculator_stoploss.py - استخراج stopLossInitial و movePercent از فایل 1.json
import os
import sys
import json
import re
import argparse

def extract_stop_loss_from_config(data):
    patterns = [
        r'stopLoss\s*:\s*close\s*\*\s*([0-9.]+)',
        r'stopLoss\s*:\s*([0-9.]+)(?![%*])',
        r'stopLoss\s*:\s*["\']?([0-9.]+)%["\']?',
        r'stopLossInitial\s*:\s*([-\d.]+)',
        r'stopLoss\s*:\s*([-\d.]+)',
    ]
    for pattern in patterns:
        match = re.search(pattern, data, re.IGNORECASE)
        if match:
            val_str = match.group(1)
            try:
                val = float(val_str)
            except:
                continue
            if '%' in pattern or val < 0:
                return -abs(val)
            if 0 < val < 1:
                return -((1 - val) * 100)
            return -abs(val)
    return -2.0

def extract_move_percents_from_config(file_path):
    try:
        with open(file_path, 'r', encoding='utf-8') as f:
            data = f.read()
        matches = re.findall(r'movePercent\s*:\s*([0-9.-]+)', data)
        if matches:
            return sorted([float(m) for m in matches])
        return None
    except:
        return None

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--strategies-json", required=True, help="خروجی loader")
    parser.add_argument("--results-base", required=True, help="مسیر پایه نتایج")
    parser.add_argument("--output", required=True, help="فایل خروجی stoploss_cache.json")
    args = parser.parse_args()

    with open(args.strategies_json, "r", encoding="utf-8") as f:
        data = json.load(f)
    strategies = data["strategies"]
    cache = {}
    for s in strategies:
        folder = s["folder"]
        group = s["group"]
        config_path = os.path.join(args.results_base, group, folder, "1.json")
        if os.path.exists(config_path):
            with open(config_path, "r", encoding="utf-8") as cf:
                config_text = cf.read()
            sl = extract_stop_loss_from_config(config_text)
            mp = extract_move_percents_from_config(config_path)
        else:
            sl = -2.0
            mp = None
        key = f"{group}_{folder}"
        cache[key] = {"stop_loss": sl, "move_percents": mp}
    os.makedirs(os.path.dirname(args.output), exist_ok=True)
    with open(args.output, "w", encoding="utf-8") as f:
        json.dump(cache, f, indent=2)
    print(f"✅ کش حد ضرر برای {len(cache)} استراتژی در {args.output} ذخیره شد.")

if __name__ == "__main__":
    main()
