#!/usr/bin/env python3
# calculators.py
# ترکیب یکپارچه calculator_returns، calculator_risk، calculator_inverse، complete_reporter
# اجرا: python calculators.py <subcommand> [args]
# subcommands: returns | risk | inverse | report

import os
import sys
import json
import csv
import re
import pickle
import argparse
import logging
import statistics
from datetime import datetime
from decimal import Decimal
from collections import defaultdict
from itertools import combinations

# ══════════════════════════════════════════════════════════════
# لاگ
# ══════════════════════════════════════════════════════════════

def setup_logging(log_file="logs/run.log"):
    os.makedirs(os.path.dirname(log_file) if os.path.dirname(log_file) else ".", exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[
            logging.StreamHandler(),
            logging.FileHandler(log_file, encoding="utf-8"),
        ],
    )

log = logging.getLogger(__name__)

# ══════════════════════════════════════════════════════════════
# توابع پایه
# ══════════════════════════════════════════════════════════════

def safe_percentage(value):
    if value is None:
        return 0.0
    if isinstance(value, (int, float)):
        return float(value)
    cleaned = str(value).replace('%', '').replace('+', '').strip()
    if not cleaned:
        return 0.0
    try:
        return float(cleaned)
    except Exception:
        return 0.0

def safe_float(value, default=0.0):
    if value is None:
        return default
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(str(value).strip())
    except Exception:
        return default

def round_to_nearest(value, options):
    if not options:
        return value
    return min(options, key=lambda x: abs(x - value))

def apply_special_rounding(percent, move_percents):
    if not move_percents or percent == 0:
        return percent
    try:
        if percent > 0:
            result = round_to_nearest(percent + 0.05, move_percents) - 0.05
            return result - 0.1
        else:
            result = round_to_nearest(abs(percent) + 0.05, move_percents) - 0.05
            return -(result - 0.1)  # باگ ۱: قبلاً +0.1 بود → کارمزد کمتر روی ضررها
    except Exception:
        return percent

# ══════════════════════════════════════════════════════════════
# رمزگشایی و بارگذاری فایل یکپارچه
# ══════════════════════════════════════════════════════════════

def decrypt_enc_file(enc_path, password):
    import subprocess
    script = (
        f"const crypto=require('crypto'),fs=require('fs');"
        f"const pass={json.dumps(password)};"
        f"const data=fs.readFileSync({json.dumps(enc_path)});"
        f"const key=crypto.scryptSync(pass,'salt',32);"
        f"const iv=data.slice(0,16),enc=data.slice(16);"
        f"const decipher=crypto.createDecipheriv('aes-256-cbc',key,iv);"
        f"const dec=Buffer.concat([decipher.update(enc),decipher.final()]);"
        f"process.stdout.write(dec);"
    )
    result = subprocess.run(["node", "-e", script], capture_output=True)
    if result.returncode != 0:
        raise RuntimeError(f"رمزگشایی ناموفق: {result.stderr.decode()}")
    return json.loads(result.stdout.decode("utf-8"))

def load_aggregated_data(enc_path, password=None):
    if not password:
        password = os.environ.get("RESULTS_PASSWORD", os.environ.get("DATA_PASSWORD", ""))
    if enc_path.endswith(".enc"):
        raw = decrypt_enc_file(enc_path, password)
    else:
        with open(enc_path, "r", encoding="utf-8") as f:
            raw = json.load(f)
    trades = raw.get("trades") or []
    metadata = raw.get("metadata") or {}
    if not trades:
        log.warning("%s: trades خالی است – مقادیر پیش‌فرض اعمال می‌شود.", enc_path)
    if not metadata:
        log.warning("%s: metadata موجود نیست – مقادیر پیش‌فرض اعمال می‌شود.", enc_path)
        metadata = {"move_percents": [], "stop_loss_initial": -2.0, "max_move_percent": None}
    return {"trades": trades, "metadata": metadata}

# ══════════════════════════════════════════════════════════════
# گروه‌بندی trades بر اساس symbol
# ══════════════════════════════════════════════════════════════

def get_symbol_combinations(symbols):
    """همه ترکیب‌های ممکن از ۱ تا n کوین را برمی‌گرداند."""
    syms = sorted(symbols)
    combos = []
    for r in range(1, len(syms) + 1):
        for combo in combinations(syms, r):
            combos.append(combo)
    return combos

def combo_key(folder, combo):
    """کلید ترکیب: folder_SYM1_SYM2_..."""
    return f"{folder}_{'_'.join(combo)}"

def combo_label(combo):
    """برچسب نمایشی ترکیب."""
    return "_".join(combo)

def get_symbol(trade):
    return trade.get("symbol") or trade.get("pair") or trade.get("coin") or "unknown"

def group_by_symbol(trades):
    groups = defaultdict(list)
    for t in trades:
        groups[get_symbol(t)].append(t)
    return dict(groups)

# ══════════════════════════════════════════════════════════════
# محاسبات بازده
# ══════════════════════════════════════════════════════════════

def real_return(trades):
    return float(sum(Decimal(str(safe_percentage(t.get("profitPercent", 0)))) for t in trades))

def special_return(trades, move_percents):
    return float(sum(
        Decimal(str(apply_special_rounding(safe_percentage(t.get("profitPercent", 0)), move_percents)))
        for t in trades
    ))

# ══════════════════════════════════════════════════════════════
# محاسبات ریسک
# ══════════════════════════════════════════════════════════════

def max_consecutive_count(trades):
    best = cur = 0
    for t in trades:
        if safe_percentage(t.get("profitPercent", 0)) < 0:
            cur += 1
            best = max(best, cur)
        else:
            cur = 0
    return best

def consecutive_loss_value(count, stop_loss):
    """مجموع ضررهای متوالی بر اساس درصد stop_loss."""
    count = int(count or 0)
    sl = float(stop_loss)
    # باگ ۳: اگر stop_loss مثبت ذخیره شده باشد، نتیجه باید منفی باشد
    return count * (-abs(sl))

def max_drawdown(trades):
    peak = cum = 0.0
    dd = 0.0
    for t in trades:
        cum += safe_percentage(t.get("profitPercent", 0))
        if cum > peak:
            peak = cum
        drawdown = peak - cum
        if drawdown > dd:
            dd = drawdown
    return -dd  # منفی — افت سرمایه

def sharpe_ratio(trades):
    profits = [safe_percentage(t.get("profitPercent", 0)) for t in trades]
    if len(profits) < 2:
        return 0.0
    try:
        std = statistics.stdev(profits)
        return (statistics.mean(profits) / std) if std else 0.0
    except Exception:
        return 0.0

def profit_loss_ratio(trades):
    profits = [safe_percentage(t.get("profitPercent", 0)) for t in trades]
    pos = [p for p in profits if p > 0]
    neg = [p for p in profits if p < 0]
    if not pos or not neg:
        return 0.0
    return statistics.mean(pos) / abs(statistics.mean(neg))

# ══════════════════════════════════════════════════════════════
# توزیع معکوس
# ══════════════════════════════════════════════════════════════

def build_inverse_dist(trades, stop_loss, max_gain):
    inv_sl = abs(stop_loss)
    agg = defaultdict(int)
    for t in trades:
        p = safe_percentage(t.get("profitPercent", 0))
        if p > 0:
            inv = -inv_sl
        elif p < 0:
            g = abs(p)
            inv = min(g, max_gain) if max_gain is not None else g
        else:
            inv = 0.0
        key = f"{inv:+.4f}%".replace("+-", "-")
        agg[key] += 1
    return [{"درصد_دقیق": k, "تعداد_معاملات": v} for k, v in sorted(agg.items())]

# ══════════════════════════════════════════════════════════════
# پردازش یک فایل یکپارچه → همه خروجی‌ها به تفکیک symbol
# ══════════════════════════════════════════════════════════════

def process_file(enc_path, move_percents_override=None, stop_loss_override=None, password=None):
    """
    خروجی: {
        "_meta": {"move_percents": [...], "stop_loss": float, "max_move_percent": float|None},
        symbol: {
            "trades": [...],
            "real": float, "special": float,
            "max_cons_count": int, "max_cons_loss": float,
            "max_drawdown": float, "sharpe": float, "pl_ratio": float,
            "win": int, "loss": int, "total": int, "win_rate": float,
            "inv_dist": [...],
        }
    }
    """
    data = load_aggregated_data(enc_path, password)
    trades = data["trades"]
    meta = data["metadata"]

    mp = move_percents_override if move_percents_override is not None else (meta.get("move_percents") or [])
    sl = stop_loss_override if stop_loss_override is not None else meta.get("stop_loss_initial", -2.0)
    # باگ ۸: max_g باید از mp (که ممکن است override شده باشد) محاسبه شود، نه مستقیم از metadata
    max_g = max(mp) if mp else meta.get("max_move_percent")

    if not trades:
        log.warning("%s: هیچ معامله‌ای وجود ندارد.", enc_path)
        return {"_meta": {"move_percents": mp, "stop_loss": sl, "max_move_percent": max_g}}

    groups = group_by_symbol(trades)
    log.info("%s: %d symbol یافت شد: %s", enc_path, len(groups), list(groups.keys()))

    result = {"_meta": {"move_percents": mp, "stop_loss": sl, "max_move_percent": max_g}}
    for sym, sym_trades in groups.items():
        wins = sum(1 for t in sym_trades if safe_percentage(t.get("profitPercent", 0)) > 0)
        losses = sum(1 for t in sym_trades if safe_percentage(t.get("profitPercent", 0)) < 0)
        wr = (wins / len(sym_trades) * 100) if sym_trades else 0.0
        mc = max_consecutive_count(sym_trades)
        r = {
            "trades": sym_trades,
            "real":            real_return(sym_trades),
            "special":         special_return(sym_trades, mp),
            "max_cons_count":  mc,
            "max_cons_loss":   consecutive_loss_value(mc, sl),
            "max_drawdown":    max_drawdown(sym_trades),
            "sharpe":          sharpe_ratio(sym_trades),
            "pl_ratio":        profit_loss_ratio(sym_trades),
            "win":   wins,
            "loss":  losses,
            "total": len(sym_trades),
            "win_rate": wr,
            "inv_dist": build_inverse_dist(sym_trades, sl, max_g),
        }
        log.info("  symbol=%s | total=%d | win_rate=%.1f%% | real=%.4f | special=%.4f | cons=%d",
                 sym, r["total"], r["win_rate"], r["real"], r["special"], r["max_cons_count"])
        result[sym] = r

    # ترکیب‌های چندتایی
    all_syms = list(groups.keys())
    for combo in get_symbol_combinations(all_syms):
        if len(combo) == 1:
            continue  # تک‌نمادها قبلاً ثبت شدند
        label = combo_label(combo)
        merged = []
        for s in combo:
            merged.extend(groups[s])
        # باگ ۹: مرتب‌سازی بر اساس زمان معامله برای صحت max_consecutive و max_drawdown
        merged.sort(key=lambda t: t.get("closeTime") or t.get("openTime") or t.get("time") or 0)
        wins = sum(1 for t in merged if safe_percentage(t.get("profitPercent", 0)) > 0)
        losses = sum(1 for t in merged if safe_percentage(t.get("profitPercent", 0)) < 0)
        wr = (wins / len(merged) * 100) if merged else 0.0
        mc = max_consecutive_count(merged)
        r = {
            "trades": merged,
            "real":            real_return(merged),
            "special":         special_return(merged, mp),
            "max_cons_count":  mc,
            "max_cons_loss":   consecutive_loss_value(mc, sl),
            "max_drawdown":    max_drawdown(merged),
            "sharpe":          sharpe_ratio(merged),
            "pl_ratio":        profit_loss_ratio(merged),
            "win":   wins,
            "loss":  losses,
            "total": len(merged),
            "win_rate": wr,
            "inv_dist": build_inverse_dist(merged, sl, max_g),
        }
        log.info("  combo=%s | total=%d | win_rate=%.1f%% | real=%.4f | special=%.4f",
                 label, r["total"], r["win_rate"], r["real"], r["special"])
        result[label] = r
    return result

# ══════════════════════════════════════════════════════════════
# subcommand: returns
# ══════════════════════════════════════════════════════════════

def cmd_returns(args):
    password = args.password or os.environ.get("RESULTS_PASSWORD", os.environ.get("DATA_PASSWORD", ""))
    with open(args.strategies_json, "r", encoding="utf-8") as f:
        strategies = json.load(f)["strategies"]

    cache = {}
    for s in strategies:
        folder = s["folder"]
        mp = s.get("move_percents") or []
        enc = s.get("aggregated_file") or os.path.join(args.results_base, folder, f"{folder}_trades.enc")
        if not os.path.exists(enc):
            log.warning("استراتژی %s: فایل یافت نشد: %s", folder, enc)
            continue
        log.info("returns ← استراتژی: %s", folder)
        try:
            per_sym = process_file(enc, mp, password=password)
            for sym, r in per_sym.items():
                if sym == "_meta":
                    continue
                key = f"{folder}_{sym}"
                cache[key] = {"real": r["real"], "special": r["special"]}
                log.info("  کش: %s → real=%.4f special=%.4f", key, r["real"], r["special"])
        except Exception as e:
            log.error("خطا استراتژی %s: %s", folder, e, exc_info=True)

    _write_json(args.output, cache)
    log.info("✅ returns_cache: %d ترکیب → %s", len(cache), args.output)

# ══════════════════════════════════════════════════════════════
# subcommand: risk
# ══════════════════════════════════════════════════════════════

def cmd_risk(args):
    password = args.password or os.environ.get("RESULTS_PASSWORD", os.environ.get("DATA_PASSWORD", ""))
    with open(args.strategies_json, "r", encoding="utf-8") as f:
        strategies = json.load(f)["strategies"]
    with open(args.stoploss_cache, "r", encoding="utf-8") as f:
        sl_cache = json.load(f)

    cache = {}
    for s in strategies:
        folder = s["folder"]
        sl = sl_cache.get(folder, {}).get("stop_loss", -2.0)
        enc = s.get("aggregated_file") or os.path.join(args.results_base, folder, f"{folder}_trades.enc")
        if not os.path.exists(enc):
            log.warning("استراتژی %s: فایل یافت نشد: %s", folder, enc)
            continue
        log.info("risk ← استراتژی: %s  sl=%.2f", folder, sl)
        try:
            per_sym = process_file(enc, stop_loss_override=sl, password=password)
            for sym, r in per_sym.items():
                if sym == "_meta":
                    continue
                key = f"{folder}_{sym}"
                cache[key] = {"max_consecutive_loss": r["max_cons_loss"], "max_consecutive_count": r["max_cons_count"]}
                log.info("  کش: %s → count=%d loss=%.4f", key, r["max_cons_count"], r["max_cons_loss"])
        except Exception as e:
            log.error("خطا استراتژی %s: %s", folder, e, exc_info=True)

    _write_json(args.output, cache)
    log.info("✅ risk_cache: %d ترکیب → %s", len(cache), args.output)

# ══════════════════════════════════════════════════════════════
# subcommand: inverse
# ══════════════════════════════════════════════════════════════

def cmd_inverse(args):
    password = args.password or os.environ.get("RESULTS_PASSWORD", os.environ.get("DATA_PASSWORD", ""))
    with open(args.strategies_json, "r", encoding="utf-8") as f:
        strategies = json.load(f)["strategies"]
    with open(args.stoploss_cache, "r", encoding="utf-8") as f:
        sl_cache = json.load(f)

    cache = {}
    for s in strategies:
        folder = s["folder"]
        sl_info = sl_cache.get(folder, {"stop_loss": -2.0, "move_percents": None})
        sl = sl_info["stop_loss"]
        mp = sl_info.get("move_percents") or []
        max_g = max(mp) if mp else None
        enc = s.get("aggregated_file") or os.path.join(args.results_base, folder, f"{folder}_trades.enc")
        if not os.path.exists(enc):
            log.warning("استراتژی %s: فایل یافت نشد: %s", folder, enc)
            continue
        log.info("inverse ← استراتژی: %s  sl=%.2f  max_gain=%s", folder, sl, max_g)
        try:
            per_sym = process_file(enc, mp, sl, password=password)
            for sym, r in per_sym.items():
                if sym == "_meta":
                    continue
                key = f"{folder}_{sym}"
                cache[key] = r["inv_dist"]
                log.info("  کش: %s → %d سطح", key, len(r["inv_dist"]))
        except Exception as e:
            log.error("خطا استراتژی %s: %s", folder, e, exc_info=True)

    _write_json(args.output, cache)
    log.info("✅ inverse_cache: %d ترکیب → %s", len(cache), args.output)

# ══════════════════════════════════════════════════════════════
# subcommand: report
# ══════════════════════════════════════════════════════════════

def cmd_report(args):
    password = args.password or os.environ.get("RESULTS_PASSWORD", os.environ.get("DATA_PASSWORD", ""))

    with open(args.returns_cache, "r", encoding="utf-8") as f:
        returns_cache = json.load(f)
    with open(args.strategies_json, "r", encoding="utf-8") as f:
        strategies = json.load(f)["strategies"]
    with open(args.risk_cache, "r", encoding="utf-8") as f:
        risk_cache = json.load(f)
    inv_cache = {}
    if args.inverse_cache and os.path.exists(args.inverse_cache):
        with open(args.inverse_cache, "r", encoding="utf-8") as f:
            inv_cache = json.load(f)
    news_events = []
    if args.news_pickle and os.path.exists(args.news_pickle):
        with open(args.news_pickle, "rb") as f:
            news_events = pickle.load(f)

    all_results = _build_all_results(strategies, returns_cache, risk_cache, inv_cache, args, password)
    log.info("✅ all_results: %d رکورد (اصلی + معکوس)", len(all_results))

    os.makedirs(args.output_dir, exist_ok=True)

    # 1. خلاصه هر پوشه (به ازای هر استراتژی پایه، همه ترکیب‌ها در یک فایل)
    base_strategy_map = defaultdict(list)
    for r in all_results:
        if not r.get("is_inverse"):
            # استراتژی پایه = period_name که همان folder اصلی است
            base_strategy_map[r["period_name"]].append(r)
    for base_folder, recs in base_strategy_map.items():
        fp = os.path.join(args.output_dir, base_folder)
        os.makedirs(fp, exist_ok=True)
        _folder_summary(base_folder, fp, recs)

    # 2. گزارش کلی
    _global_report(all_results, args.output_dir)

    # 3. جداول مقایسه
    _comparison_tables(all_results, args.output_dir)

    # 4. گزارش ماهانه
    _monthly_report(all_results, args.output_dir)

    # 5. تحلیل تکمیلی
    if news_events:
        _complementary(all_results, news_events, args.output_dir)

    log.info("✅ همه گزارش‌ها تولید شدند.")


def _build_all_results(strategies, returns_cache, risk_cache, inv_cache, args, password):
    all_results = []
    results_base = getattr(args, "results_base", "") or ""

    for s in strategies:
        folder = s["folder"]
        group = s.get("group", "aggregated")
        mp = s.get("move_percents") or []
        sl = s.get("stop_loss_initial", -2.0)
        max_move = s.get("max_move_percent")
        enc = s.get("aggregated_file") or (
            os.path.join(results_base, folder, f"{folder}_trades.enc") if results_base else ""
        )

        log.info("report ← استراتژی: %s", folder)

        # بارگذاری trades
        trades_by_sym = {}
        if enc and os.path.exists(enc):
            try:
                per_sym = process_file(enc, mp, sl, password=password)
                meta_from_file = per_sym.pop("_meta", {})
                trades_by_sym = per_sym
                # به‌روزرسانی mp/sl از metadata فایل اگر خالی بود (بدون decrypt مجدد)
                if not mp:
                    mp = meta_from_file.get("move_percents") or []
                if sl == -2.0:
                    sl = meta_from_file.get("stop_loss", -2.0)
                if max_move is None:
                    max_move = meta_from_file.get("max_move_percent")
            except Exception as e:
                log.error("خطا بارگذاری %s برای report: %s", enc, e, exc_info=True)
        else:
            log.warning("استراتژی %s: فایل %s موجود نیست – فقط کش.", folder, enc)

        # symbols از trades یا کش — پویا برای هر تعداد کوین
        if trades_by_sym:
            # process_file هم تک‌نمادها هم ترکیب‌ها را مستقیم ذخیره می‌کند
            combos_in_file = list(trades_by_sym.keys())
        else:
            # از کش: پیدا کردن همه کلیدهایی که با folder_ شروع می‌شوند
            combos_in_file = [k[len(folder)+1:] for k in returns_cache if k.startswith(f"{folder}_")]
        if not combos_in_file:
            combos_in_file = ["unknown"]

        for combo_lbl in combos_in_file:
            ck = f"{folder}_{combo_lbl}"
            rd = trades_by_sym.get(combo_lbl)

            if rd:
                total, wins, losses = rd["total"], rd["win"], rd["loss"]
                wr = rd["win_rate"]
                real, spec = rd["real"], rd["special"]
                mc_count, mc_loss = rd["max_cons_count"], rd["max_cons_loss"]
                dd, sh, pl = rd["max_drawdown"], rd["sharpe"], rd["pl_ratio"]
                inv_d = inv_cache.get(ck) or rd["inv_dist"]
            else:
                rc = returns_cache.get(ck, {"real": 0, "special": 0})
                rk = risk_cache.get(ck, {"max_consecutive_loss": 0, "max_consecutive_count": 0})
                total = wins = losses = 0
                wr = dd = sh = pl = 0.0
                real, spec = rc["real"], rc["special"]
                mc_count = rk["max_consecutive_count"]
                mc_loss = rk["max_consecutive_loss"]
                inv_d = inv_cache.get(ck)

            log.info("  ✓ %s | trades=%d | wr=%.1f%% | real=%.4f | special=%.4f",
                     ck, total, wr, real, spec)

            # بازده معکوس
            inv_real = inv_spec = 0.0
            if inv_d:
                inv_real = sum(safe_percentage(i["درصد_دقیق"]) * i["تعداد_معاملات"] for i in inv_d)
                inv_spec = sum(
                    apply_special_rounding(safe_percentage(i["درصد_دقیق"]), mp) * i["تعداد_معاملات"]
                    for i in inv_d
                )

            main_rec = {
                "folder_name": f"{folder}_{combo_lbl}", "group": group,
                "file_name": f"{folder}_{combo_lbl}", "period_name": folder, "symbol": combo_lbl,
                "total_trades": total, "win_trades": wins, "loss_trades": losses,
                "win_rate": wr, "max_consecutive_loss": mc_loss, "max_consecutive_count": mc_count,
                "max_drawdown": dd, "profit_loss_ratio": pl, "sharpe_ratio": sh,
                "real_return": real, "special_rounded_return": spec,
                "is_inverse": False,
                "_move_percents": mp, "_stop_loss": sl, "_max_move": max_move,
            }
            all_results.append(main_rec)

            if inv_d is not None:
                # محاسبه ریسک برای حالت معکوس بر اساس trades معکوس
                if rd:
                    inv_trades = []
                    inv_sl = abs(sl)
                    max_g = max_move if max_move is not None else (max(mp) if mp else None)
                    for t in rd["trades"]:
                        p = safe_percentage(t.get("profitPercent", 0))
                        if p > 0:
                            inv_p = -inv_sl
                        elif p < 0:
                            g = abs(p)
                            inv_p = min(g, max_g) if max_g is not None else g
                        else:
                            inv_p = 0.0
                        inv_trades.append({"profitPercent": inv_p})
                    inv_mc_count = max_consecutive_count(inv_trades)
                    inv_mc_loss = consecutive_loss_value(inv_mc_count, -inv_sl)
                    inv_dd = max_drawdown(inv_trades)
                    inv_sh = sharpe_ratio(inv_trades)
                    inv_pl = profit_loss_ratio(inv_trades)
                    inv_wins = sum(1 for t in inv_trades if t["profitPercent"] > 0)
                    inv_losses = sum(1 for t in inv_trades if t["profitPercent"] < 0)
                    inv_wr = (inv_wins / len(inv_trades) * 100) if inv_trades else 0.0
                else:
                    inv_mc_count = 0
                    inv_mc_loss = None
                    inv_dd = dd
                    inv_sh = -sh
                    inv_pl = (1 / pl) if pl > 0 else 0
                    inv_wins = losses
                    inv_losses = wins
                    inv_wr = 100.0 - wr

                inv_rec = {
                    "folder_name": f"{folder}_{combo_lbl}_INV", "group": group,
                    "file_name": f"INV_{folder}_{combo_lbl}", "period_name": folder, "symbol": combo_lbl,
                    "total_trades": total, "win_trades": inv_wins, "loss_trades": inv_losses,
                    "win_rate": inv_wr,
                    "max_consecutive_loss": inv_mc_loss, "max_consecutive_count": inv_mc_count,
                    "max_drawdown": inv_dd,
                    "profit_loss_ratio": inv_pl,
                    "sharpe_ratio": inv_sh,
                    "real_return": inv_real, "special_rounded_return": inv_spec,
                    "is_inverse": True,
                    "_move_percents": mp, "_stop_loss": sl, "_max_move": max_move,
                }
                all_results.append(inv_rec)
                log.info("  ✓ INV %s | real=%.4f | special=%.4f | cons=%d", ck, inv_real, inv_spec, inv_mc_count)

    return all_results

# ══════════════════════════════════════════════════════════════
# گزارش‌های CSV
# ══════════════════════════════════════════════════════════════

def _score(returns_ge_1, returns_gt_0, avg_max_loss, total_files):
    if not total_files:
        return 0, "بدون داده"
    s = (returns_ge_1 / total_files) * 40 + (returns_gt_0 / total_files) * 30
    la = abs(avg_max_loss)
    # باگ ۱۴: امتیازدهی پیوسته برای جلوگیری از پرش ناگهانی در مرزها
    if la <= 5:
        s += 30
    elif la <= 10:
        s += 30 * (10 - la) / 5
    elif la <= 20:
        # ادغام بازه ۱۰-۱۵ و ۱۵-۲۰ به یک تابع خطی پیوسته
        # در la=10: امتیاز=30، در la=20: امتیاز=0
        s += 30 * (20 - la) / 10
    else:
        s += 0
    s = round(s, 1)
    rating = ("عالی ★★★★★" if s >= 80 else "خوب ★★★★" if s >= 60
              else "متوسط ★★★" if s >= 40 else "ضعیف ★★" if s >= 20 else "بسیار ضعیف ★")
    return s, rating


def _folder_summary(folder_name, folder_path, recs):
    if not recs:
        return
    path = os.path.join(folder_path, "خلاصه_پوشه.csv")
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow([f"📊 خلاصه پوشه: {folder_name}"])
        w.writerow(["تاریخ تولید", datetime.now().strftime("%Y-%m-%d %H:%M:%S")])
        w.writerow([])
        w.writerow(["ردیف", "ترکیب کوین‌ها", "معاملات", "سودده", "زیانده", "وین‌ریت(%)",
                    "بازده واقعی(%)", "بازده ویژه(%)", "حداکثر ضرر متوالی(%)",
                    "تعداد ضررهای متوالی", "افت سرمایه(%)", "P/L ratio", "شارپ", "وضعیت"])
        for i, r in enumerate(recs, 1):
            sp = r["special_rounded_return"]
            status = "✅ عالی (≥۱%)" if sp >= 1 else ("👍 مثبت" if sp > 0 else "⚠️ منفی")
            ml = f"{r['max_consecutive_loss']:.2f}" if r.get("max_consecutive_loss") is not None else "-"
            sh = f"{r['sharpe_ratio']:.3f}" if r.get("sharpe_ratio") is not None else "-"
            combo_display = r.get("symbol", r["folder_name"])
            w.writerow([i, combo_display, r["total_trades"], r["win_trades"], r["loss_trades"],
                        f"{r.get('win_rate', 0):.2f}", f"{r['real_return']:.4f}", f"{sp:.4f}",
                        ml, r.get("max_consecutive_count", 0),
                        f"{r.get('max_drawdown', 0):.2f}", f"{r.get('profit_loss_ratio', 0):.3f}",
                        sh, status])
        w.writerow([])
        pos = sum(1 for r in recs if r["special_rounded_return"] > 0)
        ge1 = sum(1 for r in recs if r["special_rounded_return"] >= 1)
        n = len(recs)
        avg_sp = statistics.mean([r["special_rounded_return"] for r in recs])
        w.writerow(["ترکیب‌های با بازدهی مثبت", f"{pos}/{n}"])
        w.writerow(["ترکیب‌های با بازدهی ≥۱٪", f"{ge1}/{n}"])
        w.writerow(["میانگین بازدهی ویژه", f"{avg_sp:.4f}%"])
    log.info("خلاصه پوشه %s → %s", folder_name, path)


def _global_report(all_results, out_dir):
    path = os.path.join(out_dir, "گزارش_کلی_نتایج.csv")
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["گروه", "پوشه", "symbol", "معاملات", "سودده", "زیانده", "وین‌ریت(%)",
                    "حداکثر ضرر متوالی(%)", "تعداد ضررهای متوالی", "افت سرمایه(%)",
                    "P/L ratio", "شارپ", "بازده واقعی(%)", "بازده ویژه(%)", "نوع"])
        for r in all_results:
            ml = f"{r['max_consecutive_loss']:.2f}" if r.get("max_consecutive_loss") is not None else "-"
            sh = f"{r['sharpe_ratio']:.3f}" if r.get("sharpe_ratio") is not None else "-"
            w.writerow([r["group"], r["folder_name"], r.get("symbol", ""),
                        r["total_trades"], r["win_trades"], r["loss_trades"],
                        f"{r['win_rate']:.2f}", ml, r.get("max_consecutive_count", 0),
                        f"{r.get('max_drawdown', 0):.2f}", f"{r.get('profit_loss_ratio', 0):.3f}",
                        sh, f"{r['real_return']:.4f}", f"{r['special_rounded_return']:.4f}",
                        "معکوس" if r["is_inverse"] else "اصلی"])
    log.info("گزارش کلی → %s", path)


def _comparison_tables(all_results, out_dir):
    agg = {}
    for r in all_results:
        k = (r["folder_name"], r["group"])
        if k not in agg:
            agg[k] = {"folder": r["folder_name"], "group": r["group"],
                      "win_rates": [], "sharpes": [], "max_losses": [],
                      "real": [], "special": [], "total_trades": 0,
                      "ge1": 0, "gt0": 0, "n": 0}
        a = agg[k]
        a["win_rates"].append(r["win_rate"])
        a["sharpes"].append(r.get("sharpe_ratio") or 0)
        a["max_losses"].append(r["max_consecutive_loss"] if r.get("max_consecutive_loss") is not None else 0)
        a["real"].append(r["real_return"])
        a["special"].append(r["special_rounded_return"])
        a["total_trades"] += r["total_trades"]
        a["ge1"] += 1 if r["special_rounded_return"] >= 1 else 0
        a["gt0"] += 1 if r["special_rounded_return"] > 0 else 0
        a["n"] += 1

    rows = []
    for (folder, group), a in agg.items():
        n = a["n"]
        avg_loss = statistics.mean(a["max_losses"]) if a["max_losses"] else 0
        sc, rt = _score(a["ge1"], a["gt0"], avg_loss, n)
        rows.append({
            "folder": folder, "group": group,
            "n": n, "is_inv": folder.endswith("_INV"),
            "avg_win": statistics.mean(a["win_rates"]) if a["win_rates"] else 0,
            "avg_sh": statistics.mean(a["sharpes"]) if a["sharpes"] else 0,
            "avg_loss": avg_loss,
            "avg_real": statistics.mean(a["real"]) if a["real"] else 0,
            "avg_special": statistics.mean(a["special"]) if a["special"] else 0,
            "total_trades": a["total_trades"],
            "score": sc, "rating": rt,
        })
    rows.sort(key=lambda x: x["score"], reverse=True)

    for suffix, col, col_key in [
        ("بازده_واقعی", "بازده واقعی(%)", "avg_real"),
        ("بازده_ویژه",  "بازده ویژه(%)",  "avg_special"),
    ]:
        path = os.path.join(out_dir, f"جدول_مقایسه_همه_{suffix}.csv")
        with open(path, "w", encoding="utf-8-sig", newline="") as f:
            w = csv.writer(f)
            w.writerow(["رتبه", "پوشه", "گروه", "نوع", "تعداد نمادها", col,
                        "وین‌ریت(%)", "حداکثر ضرر متوالی(%)", "شارپ",
                        "کل معاملات", "امتیاز", "وضعیت"])
            for i, row in enumerate(rows, 1):
                w.writerow([i, row["folder"], row["group"],
                            "معکوس" if row["is_inv"] else "اصلی",
                            row["n"], f"{row[col_key]:.4f}%", f"{row['avg_win']:.2f}",
                            f"{row['avg_loss']:.2f}", f"{row['avg_sh']:.3f}",
                            row["total_trades"], f"{row['score']:.1f}/100", row["rating"]])
        log.info("جدول %s → %s", suffix, path)


def _extract_start(period):
    m = re.search(r'(\d{4}-\d{2}-\d{2})_', period or "")
    return m.group(1) if m else None

def _extract_end(period):
    m = re.search(r'_(\d{4}-\d{2}-\d{2})$', period or "")
    return m.group(1) if m else None

def _events_in_range(events, s, e):
    try:
        sd = datetime.strptime(s, "%Y-%m-%d").date()
        ed = datetime.strptime(e, "%Y-%m-%d").date()
    except Exception:
        return []
    return [ev for ev in events if sd <= ev["date"] <= ed]


def _monthly_records(recs, group):
    mf = {}
    for r in recs:
        start = _extract_start(r.get("period_name", ""))
        if not start:
            continue
        mf.setdefault(start[:7], []).append(r)
    out = []
    for ym, files in mf.items():
        sel = []
        for day in [1, 11, 21]:
            found = next((f for f in files if _extract_start(f.get("period_name","")) == f"{ym}-{day:02d}"), None)
            if found:
                sel.append(found)
        if len(sel) != 3:
            continue
        total_ret = sum(f["special_rounded_return"] for f in sel) if group == "1" else sum(f["real_return"] for f in sel)
        max_l = min((f["max_consecutive_loss"] for f in sel if f.get("max_consecutive_loss") is not None), default=0)
        # باگ ۴ (حل‌شده با باگ ۳): چون max_consecutive_loss همیشه منفی است، min بدترین را انتخاب می‌کند
        out.append({"year_month": ym, "total_return": total_ret, "max_loss": max_l,
                    "files": [f["file_name"] for f in sel], "file_objects": sel})
    out.sort(key=lambda x: x["year_month"])
    return out


def _monthly_report(all_results, out_dir, top_n=3):
    strat_files = defaultdict(list)
    strat_scores = {}
    for r in all_results:
        strat_files[(r["period_name"], r["group"])].append(r)
    for k, files in strat_files.items():
        # باگ ۱۲: فقط رکوردهای غیر معکوس در امتیازدهی شرکت می‌کنند
        non_inv = [f for f in files if not f.get("is_inverse")]
        n = len(non_inv)
        if not n:
            continue
        ge1 = sum(1 for f in non_inv if f["special_rounded_return"] >= 1)
        gt0 = sum(1 for f in non_inv if f["special_rounded_return"] > 0)
        ml = [f["max_consecutive_loss"] for f in non_inv if f.get("max_consecutive_loss") is not None]
        avg_l = statistics.mean(ml) if ml else 0
        sc, _ = _score(ge1, gt0, avg_l, n)
        strat_scores[k] = sc

    top = sorted(strat_scores.items(), key=lambda x: x[1], reverse=True)[:top_n]
    for (period, group), _ in top:
        # باگ ۱۱: فیلتر بر اساس period_name (استراتژی پایه)
        recs = [f for f in strat_files[(period, group)] if not f.get("is_inverse")]
        monthly = _monthly_records(recs, group)
        if not monthly:
            continue
        for rec in monthly:
            n = len(rec["file_objects"])
            ge1 = sum(1 for f in rec["file_objects"] if f["special_rounded_return"] >= 1)
            gt0 = sum(1 for f in rec["file_objects"] if f["special_rounded_return"] > 0)
            sc, rt = _score(ge1, gt0, rec["max_loss"], n)
            rec["score"] = sc; rec["rating"] = rt

        sp = os.path.join(out_dir, group, period)
        os.makedirs(sp, exist_ok=True)
        path = os.path.join(sp, "گزارش_ماهانه_برترین.csv")
        with open(path, "w", encoding="utf-8-sig", newline="") as f:
            w = csv.writer(f)
            w.writerow([f"📊 گزارش ماهانه: {period} (گروه {group})"])
            w.writerow(["تاریخ تولید", datetime.now().strftime("%Y-%m-%d %H:%M:%S")])
            w.writerow([])
            w.writerow(["رتبه", "ماه", "بازده(%)", "وین‌ریت(%)", "حداکثر ضرر(%)", "شارپ", "امتیاز", "وضعیت", "فایل‌ها"])
            for i, rec in enumerate(sorted(monthly, key=lambda x: x["score"], reverse=True), 1):
                fobjs = rec["file_objects"]
                tt = sum(f["total_trades"] for f in fobjs)
                wt = sum(f["win_trades"] for f in fobjs)
                wr = (wt / tt * 100) if tt else 0
                shs = [f["sharpe_ratio"] for f in fobjs if f.get("sharpe_ratio") is not None]
                avg_sh = statistics.mean(shs) if shs else 0
                w.writerow([i, rec["year_month"], f"{rec['total_return']:.4f}", f"{wr:.2f}",
                            f"{rec['max_loss']:.2f}", f"{avg_sh:.3f}",
                            f"{rec['score']:.1f}/100", rec["rating"], "; ".join(rec["files"])])
        log.info("گزارش ماهانه %s → %s", period, path)


def _complementary(all_results, news_events, out_dir):
    strat_map = defaultdict(list)
    for r in all_results:
        if not r.get("is_inverse"):
            strat_map[(r["period_name"], r["group"])].append(r)
    for (period, group), recs in strat_map.items():
        monthly = _monthly_records(recs, group)
        if not monthly:
            continue
        events_data = []
        for rec in monthly:
            # باگ ۱۰: file_objects از _monthly_records قبلاً مرتب‌شده (ترتیب day=1,11,21)
            start = _extract_start(rec["file_objects"][0].get("period_name", ""))
            end = _extract_end(rec["file_objects"][-1].get("period_name", ""))
            if not start or not end:
                continue
            for ev in _events_in_range(news_events, start, end):
                if ev.get("actual") is not None and ev.get("forecast") is not None:
                    events_data.append({
                        "month": rec["year_month"], "return_month": rec["total_return"],
                        "indicator": ev["indicator"],
                        "diff": ev["actual"] - ev["forecast"]
                    })
        if not events_data:
            continue
        path = os.path.join(out_dir, group, period, "تحلیل_تکمیلی_استراتژی.txt")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write("=" * 70 + "\n")
            f.write(f"📈 تحلیل تکمیلی: {period} (گروه {group})\n")
            f.write(f"تاریخ: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
            f.write("=" * 70 + "\n\n")
            for ind in sorted(set(e["indicator"] for e in events_data)):
                ind_ev = [e for e in events_data if e["indicator"] == ind]
                for label, evs in [("ضررده", [e for e in ind_ev if e["return_month"] < 0]),
                                    ("سودده", [e for e in ind_ev if e["return_month"] >= 0])]:
                    diffs = [e["diff"] for e in evs]
                    avg = f"{statistics.mean(diffs):.4f}" if diffs else "---"
                    f.write(f"\n📌 {ind} | ماه‌های {label}: تعداد={len(evs)}  avg_diff={avg}\n")
        log.info("تحلیل تکمیلی %s → %s", period, path)


# ══════════════════════════════════════════════════════════════
# کمکی
# ══════════════════════════════════════════════════════════════

def _write_json(path, data):
    os.makedirs(os.path.dirname(path) if os.path.dirname(path) else ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)

# ══════════════════════════════════════════════════════════════
# main
# ══════════════════════════════════════════════════════════════

def main():
    setup_logging()
    parser = argparse.ArgumentParser(description="ماژول یکپارچه محاسبات")
    sub = parser.add_subparsers(dest="cmd", required=True)

    # ─── returns ───
    p = sub.add_parser("returns")
    p.add_argument("--strategies-json", required=True)
    p.add_argument("--results-base", required=True)
    p.add_argument("--output", required=True)
    p.add_argument("--password", default="")

    # ─── risk ───
    p = sub.add_parser("risk")
    p.add_argument("--strategies-json", required=True)
    p.add_argument("--results-base", required=True)
    p.add_argument("--stoploss-cache", required=True)
    p.add_argument("--output", required=True)
    p.add_argument("--password", default="")

    # ─── inverse ───
    p = sub.add_parser("inverse")
    p.add_argument("--strategies-json", required=True)
    p.add_argument("--results-base", required=True)
    p.add_argument("--stoploss-cache", required=True)
    p.add_argument("--output", required=True)
    p.add_argument("--password", default="")

    # ─── report ───
    p = sub.add_parser("report")
    p.add_argument("--returns-cache", required=True)
    p.add_argument("--strategies-json", required=True)
    p.add_argument("--risk-cache", required=True)
    p.add_argument("--inverse-cache", default="")
    p.add_argument("--results-base", default="")
    p.add_argument("--news-pickle", default="")
    p.add_argument("--output-dir", required=True)
    p.add_argument("--password", default="")

    args = parser.parse_args()
    {"returns": cmd_returns, "risk": cmd_risk, "inverse": cmd_inverse, "report": cmd_report}[args.cmd](args)


if __name__ == "__main__":
    main()
