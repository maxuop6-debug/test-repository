#!/usr/bin/env python3
# extract_signatures_monthly.py
# ساخت ورودی خام (per-month) برای سه ماژول Golden / Portfolios / Correlation
# از روی تجمیع ماهانه (combo_monthly).
#
# همانند extract_signatures_10day.py، هیچ منطق بازده‌ای دوباره نوشته نمی‌شود؛
# load_skeep_tp_sl و compute_trade_profit مستقیماً از combo_monthly.py
# import می‌شوند.

import os
import sys
import json
import argparse
import statistics
from datetime import datetime, timedelta
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import combo_monthly as cm  # noqa: E402


def _importance(actual, forecast, distance_days):
    if actual is None or forecast is None:
        return None
    d = distance_days if distance_days is not None else 0
    return abs(actual - forecast) * (1.0 / (d + 1))


def build_month_records(trades_json_path, news_dir, target_coin, model,
                         strategy_folder, min_sample_count=1):
    use_tp_sl, take_profit, stop_loss = cm.load_skeep_tp_sl(trades_json_path)

    with open(trades_json_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    if isinstance(data, list):
        trades, metadata = data, None
    elif isinstance(data, dict) and "trades" in data:
        trades, metadata = data["trades"], data.get("metadata", {})
    else:
        raise ValueError("فایل معاملات باید آرایه JSON یا دیکشنری با کلید 'trades' باشد.")

    move_percents = metadata.get("move_percents", []) if metadata else []
    target_coins = [c.strip() for c in target_coin.split('+')]

    monthly_profits = defaultdict(list)
    for t in trades:
        symbol = t.get("symbol") or t.get("pair") or t.get("coin")
        if symbol not in target_coins:
            continue

        time_str = (t.get("entryTime") or t.get("entry_time") or
                    t.get("open_time") or t.get("time") or t.get("timestamp"))
        if not time_str:
            continue
        try:
            s = str(time_str)
            date_part = s.split('T')[0] if 'T' in s else (s.split(' ')[0] if ' ' in s else s)
            trade_date = datetime.strptime(date_part, "%Y-%m-%d").date()
        except (ValueError, AttributeError):
            continue

        raw_profit = t.get("profitPercent", 0.0)
        try:
            raw_profit = float(raw_profit)
        except (TypeError, ValueError):
            raw_profit = 0.0

        # === محاسبه بازده دقیقاً با تابع تست‌شده‌ی combo_monthly ===
        profit = cm.compute_trade_profit(
            raw_profit, move_percents, model, t,
            use_tp_sl=use_tp_sl, take_profit=take_profit, stop_loss=stop_loss,
        )
        ym = trade_date.strftime("%Y-%m")
        monthly_profits[ym].append(profit)

    if not monthly_profits:
        return []

    news_events = cm.load_news_from_directory(news_dir)
    if not news_events:
        return []

    records = []
    for ym in sorted(monthly_profits.keys()):
        profits = monthly_profits[ym]
        year, month = int(ym[:4]), int(ym[5:7])
        start_date = datetime(year, month, 1).date()
        if month == 12:
            end_date = datetime(year + 1, 1, 1).date() - timedelta(days=1)
        else:
            end_date = datetime(year, month + 1, 1).date() - timedelta(days=1)

        events_in_range = [ev for ev in news_events if start_date <= ev["date"] <= end_date]

        dominant_indicator = None
        dominant_score = -1.0
        diffs_all = []
        indicators_present = set()
        for ev in events_in_range:
            indicators_present.add(ev["indicator"])
            if ev["actual"] is None or ev["forecast"] is None:
                continue
            diff = ev["actual"] - ev["forecast"]
            diffs_all.append(diff)
            d_days = abs((ev["date"] - start_date).days)
            score = _importance(ev["actual"], ev["forecast"], d_days)
            if score is not None and score > dominant_score:
                dominant_score = score
                dominant_indicator = ev["indicator"]

        total_return = sum(profits)
        trade_count = len(profits)
        avg_trade_return = (total_return / trade_count) if trade_count else 0.0
        period_len = (end_date - start_date).days + 1
        avg_daily_return = (avg_trade_return / period_len) if period_len else 0.0

        secondary = sorted(indicators_present - ({dominant_indicator} if dominant_indicator else set()))

        records.append({
            "coin_composition": target_coin,
            "model": model,                 # برای حفظ سازگاری نام‌گذاری؛ در ماهانه اثری بر تقسیم بازه ندارد
            "interval": "monthly",
            "indicator_key": None,           # دوره‌ی ماهانه به یک شاخص خاص لنگر نشده
            "position": None,
            "distance_days": None,
            "period_start": start_date.isoformat(),
            "period_end": end_date.isoformat(),
            "period_length_days": period_len,
            "total_return": total_return,         # مجموع_بازده_دوره
            "trade_count": trade_count,
            "avg_trade_return": avg_trade_return,  # میانگین_بازده_معامله
            "avg_daily_return": avg_daily_return,
            "dominant_indicator": dominant_indicator,
            "dominant_indicator_importance": (dominant_score if dominant_score >= 0 else None),
            "secondary_indicators": secondary,
            "diff_avg": (statistics.mean(diffs_all) if diffs_all else None),
            "diff_std": (statistics.pstdev(diffs_all) if len(diffs_all) > 1 else (0.0 if diffs_all else None)),
            "event_count": len(events_in_range),
            "indicator_diversity": len(indicators_present),
            "use_tp_sl": use_tp_sl,
            "take_profit": take_profit,
            "stop_loss": stop_loss,
            "strategy_folder": strategy_folder,
            "market_regime": None,  # نیازمند داده قیمت روزانه (MA50/MA200/ATR) — اینجا تولید نمی‌شود
        })

    return [r for r in records if r["trade_count"] >= min_sample_count]


def main():
    parser = argparse.ArgumentParser(
        description="ساخت ورودی خام Golden/Portfolios/Correlation از تجمیع ماهانه"
    )
    parser.add_argument("--trades-json", required=True)
    parser.add_argument("--news-dir", required=True)
    parser.add_argument("--strategy-folder", required=True)
    parser.add_argument("--coin", required=True)
    parser.add_argument("--model", required=True, choices=cm.VALID_MODELS)
    parser.add_argument("--min-sample-count", type=int, default=1)
    parser.add_argument("--out", required=True, help="مسیر خروجی JSON Lines (.jsonl)")
    args = parser.parse_args()

    records = build_month_records(
        trades_json_path=args.trades_json,
        news_dir=args.news_dir,
        target_coin=args.coin,
        model=args.model,
        strategy_folder=args.strategy_folder,
        min_sample_count=args.min_sample_count,
    )

    out_dir = os.path.dirname(args.out)
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)

    with open(args.out, "w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")

    print(f"✅ {len(records)} رکورد per-month ذخیره شد: {args.out}")


if __name__ == "__main__":
    main()
