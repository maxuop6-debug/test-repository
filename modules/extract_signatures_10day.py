#!/usr/bin/env python3
# extract_signatures_10day.py
# ساخت ورودی خام (per-period) برای سه ماژول Golden / Portfolios / Correlation
# از روی بازه‌های خبری (combo_10day).
#
# اصل طراحی: این فایل هیچ منطق بازده یا تقسیم بازه را دوباره پیاده‌سازی نمی‌کند.
# همه‌ی منطق حساس (apply_special_rounding، فایل پرچم skeepmove_percents.txt،
# compute_trade_profit، get_period_key_from_date) مستقیماً از combo_10day.py
# import می‌شود تا با خروجی CSV اصلی ۱۰۰٪ همخوان بماند و هیچ drift ایجاد نشود.

import os
import sys
import json
import argparse
import statistics
from datetime import datetime
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import combo_10day as c10  # noqa: E402


def _importance(actual, forecast, distance_days):
    """فرمول اهمیت: |actual-forecast| × 1/(فاصله_روز+1)"""
    if actual is None or forecast is None:
        return None
    d = distance_days if distance_days is not None else 0
    return abs(actual - forecast) * (1.0 / (d + 1))


def build_period_records(trades_json_path, news_dir, interval, target_coin,
                          model, strategy_folder, min_sample_count=1):
    """
    خروجی: لیستی از رکوردهای per-period (دیکشنری) — هر رکورد دقیقاً معادل یک
    «دوره» در منطق combo_10day.py است (همان period_key که در CSV اصلی هم
    استفاده می‌شود)، اما به‌جای ادغام در «الگوهای ترکیبی» شاخص‌ها، خام نگه
    داشته می‌شود تا Golden/Portfolios/Correlation بتوانند امضای خودشان را
    بسازند.

    نکته حیاتی بازده:
    - اگر فایل skeepmove_percents.txt در کنار trades_json_path وجود داشته باشد
      → از حالت skeep (take_profit/stop_loss، بدون special rounding) استفاده می‌شود.
    - در غیر این صورت → apply_special_rounding با move_percents اجباری است
      (دقیقاً مثل combo_10day.py؛ تابع به‌صورت مستقیم از همان ماژول صدا زده می‌شود).
    """
    use_tp_sl, take_profit, stop_loss = c10.load_skeep_tp_sl(trades_json_path)

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

    trade_list = []
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

        # === محاسبه بازده دقیقاً با تابع تست‌شده‌ی combo_10day ===
        profit = c10.compute_trade_profit(
            raw_profit, move_percents, model, t,
            use_tp_sl=use_tp_sl, take_profit=take_profit, stop_loss=stop_loss,
        )
        trade_list.append((trade_date, profit))

    if not trade_list:
        return []

    news_events = c10.load_news_from_directory(news_dir)
    if not news_events:
        return []

    parsed = c10.parse_interval(interval)
    if parsed is None:
        return []
    indicator_key, direction, distance_days = parsed  # ممکن است indicator_key == 'fixed'

    period_groups = defaultdict(list)
    for date, profit in trade_list:
        period_key = c10.get_period_key_from_date(date, interval, news_events, model=model)
        if period_key is None:
            continue
        period_groups[period_key].append(profit)

    if not period_groups:
        return []

    records = []
    for period_key, profits in period_groups.items():
        if '_' not in period_key or len(period_key) < 21:
            continue
        start_str, end_str = period_key[:10], period_key[11:]
        try:
            start_date = datetime.strptime(start_str, "%Y-%m-%d").date()
            end_date = datetime.strptime(end_str, "%Y-%m-%d").date()
        except ValueError:
            continue

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
            # فاصله‌ی همین رویداد خاص تا شروع دوره (برای فرمول اهمیت)
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
            "model": model,
            "interval": interval,
            "indicator_key": indicator_key,
            "position": direction,          # 'pre' / 'post' / None (برای fixed)
            "distance_days": distance_days,
            "period_start": start_date.isoformat(),
            "period_end": end_date.isoformat(),
            "period_length_days": period_len,
            "total_return": total_return,                # special-rounded (یا skeep) — مجموع_بازده_دوره
            "trade_count": trade_count,
            "avg_trade_return": avg_trade_return,          # میانگین_بازده_معامله
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
            # هشدار صریح: رژیم بازار اینجا محاسبه نمی‌شود — به داده قیمت روزانه
            # (OHLC) نیاز دارد که در trades.enc / news.pickle وجود ندارد.
            "market_regime": None,
        })

    filtered = [r for r in records if r["trade_count"] >= min_sample_count]
    return filtered


def main():
    parser = argparse.ArgumentParser(
        description="ساخت ورودی خام Golden/Portfolios/Correlation از بازه‌های خبری (10day)"
    )
    parser.add_argument("--trades-json", required=True)
    parser.add_argument("--news-dir", required=True)
    parser.add_argument("--interval", required=True)
    parser.add_argument("--strategy-folder", required=True)
    parser.add_argument("--coin", required=True)
    parser.add_argument("--model", required=True, choices=c10.VALID_MODELS)
    parser.add_argument("--min-sample-count", type=int, default=1)
    parser.add_argument("--out", required=True, help="مسیر خروجی JSON Lines (.jsonl)")
    args = parser.parse_args()

    records = build_period_records(
        trades_json_path=args.trades_json,
        news_dir=args.news_dir,
        interval=args.interval,
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

    print(f"✅ {len(records)} رکورد per-period ذخیره شد: {args.out}")


if __name__ == "__main__":
    main()
