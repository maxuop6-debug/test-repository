#!/usr/bin/env python3
"""
generate_correlation_chunks.py

ورودی‌ها:
  --signatures-dir DIR   پوشه حاوی فایل‌های .jsonl استخراج‌شده از archive‌ها
  --queue-file FILE       all_combinations_correlation.json (آرایه‌ای از آیتم‌های صف)
  --output-dir DIR        پوشه خروجی برای chunk‌ها

خروجی‌ها:
  chunk_0.json, chunk_1.json, ...   هر chunk آرایه‌ای از آیتم‌های صف
  num_chunks.txt                    تعداد chunk‌های تولیدشده
  empty.flag                        اگر هیچ آیتم قابل پردازشی وجود نداشته باشد

منطق:
  - صف (queue-file) را می‌خواند؛ هر آیتم می‌تواند یکی از دو فرمت داشته باشد:
      A) {"signature": "...", "coin_composition": "...", ...}
      B) {"signature_path": "module/strategy/base_name", ...}
  - فرمت B را به فرمت A تبدیل می‌کند:
      signature      = base_name (آخرین قسمت مسیر)
      coin_composition = اولین قسمت base_name قبل از اولین "_"
  - آیتم‌هایی که فایل .jsonl متناظرشان در signatures-dir وجود ندارد حذف می‌شوند
    (این آیتم‌ها در صف‌اند ولی archive هنوز extract نشده — دور بعدی پردازش می‌شوند)
  - آیتم‌های باقی‌مانده را unique_by(coin_composition + "|||" + signature) می‌کند
  - در chunk‌های MAX_CHUNK_SIZE تایی تقسیم می‌کند
"""

import argparse
import json
import os
import sys
from pathlib import Path

MAX_CHUNK_SIZE = 500


def find_jsonl_for_item(item: dict, signatures_dir: Path) -> bool:
    """
    بررسی می‌کند که فایل .jsonl متناظر آیتم در signatures_dir وجود دارد.
    جستجو recursive و case-sensitive است.
    """
    sig = item.get("signature", "")
    coin = item.get("coin_composition", "")
    if not sig:
        return False

    # اگر coin_composition داریم، مسیر دقیق‌تری جستجو می‌کنیم
    # ساختار رایج: signatures_dir/<هر زیرمسیری>/<sig>.jsonl
    # یا: signatures_dir/<coin_composition>/<sig>.jsonl
    # جستجو را با find پوشانیم تا از ساختار دقیق بی‌نیاز باشیم

    # ابتدا مسیر ترجیحی را چک کن
    if coin:
        preferred = signatures_dir / coin / f"{sig}.jsonl"
        if preferred.exists():
            return True

    # جستجوی recursive برای هر نام فایل منطبق
    sig_filename = f"{sig}.jsonl"
    for found in signatures_dir.rglob(sig_filename):
        return True

    return False


def normalize_item(raw_item: dict) -> dict | None:
    """
    هر دو فرمت آیتم صف را به فرمت استاندارد {coin_composition, signature, ...}
    تبدیل می‌کند. None برمی‌گرداند اگر آیتم قابل تبدیل نباشد.
    """
    if not isinstance(raw_item, dict):
        return None

    # فرمت A: از پیش normalized
    if "signature" in raw_item and "coin_composition" in raw_item:
        sig = str(raw_item["signature"]).strip()
        coin = str(raw_item["coin_composition"]).strip()
        if sig and coin:
            return {**raw_item, "signature": sig, "coin_composition": coin}
        return None

    # فرمت B: signature_path = "module/strategy/base_name"
    if "signature_path" in raw_item:
        sig_path = str(raw_item["signature_path"]).strip()
        if not sig_path:
            return None
        parts = sig_path.split("/")
        base_name = parts[-1] if parts else sig_path
        # coin_composition = اولین قسمت base_name قبل از اولین "_"
        coin = base_name.split("_")[0] if "_" in base_name else base_name
        return {
            **raw_item,
            "signature": base_name,
            "coin_composition": coin,
        }

    return None


def main():
    parser = argparse.ArgumentParser(description="تولید chunk‌های correlation از صف")
    parser.add_argument("--signatures-dir", required=True,
                        help="پوشه حاوی فایل‌های .jsonl")
    parser.add_argument("--queue-file", required=True,
                        help="مسیر all_combinations_correlation.json")
    parser.add_argument("--output-dir", required=True,
                        help="پوشه خروجی chunk‌ها")
    args = parser.parse_args()

    signatures_dir = Path(args.signatures_dir)
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    # ── خواندن صف ──────────────────────────────────────────────────
    queue_path = Path(args.queue_file)
    if not queue_path.exists() or queue_path.stat().st_size == 0:
        print("⚠️ فایل صف یافت نشد یا خالی است → empty.flag", flush=True)
        (output_dir / "empty.flag").touch()
        return

    with open(queue_path, encoding="utf-8") as f:
        try:
            raw_queue = json.load(f)
        except json.JSONDecodeError as e:
            print(f"❌ خطا در parse صف: {e}", flush=True)
            (output_dir / "empty.flag").touch()
            return

    if not isinstance(raw_queue, list) or len(raw_queue) == 0:
        print("ℹ️ صف خالی است → empty.flag", flush=True)
        (output_dir / "empty.flag").touch()
        return

    print(f"[DEBUG] تعداد آیتم‌های خام صف: {len(raw_queue)}", flush=True)

    # ── normalize ──────────────────────────────────────────────────
    normalized = []
    for item in raw_queue:
        n = normalize_item(item)
        if n is not None:
            normalized.append(n)

    print(f"[DEBUG] تعداد آیتم‌های normalize‌شده: {len(normalized)}", flush=True)

    # ── حذف duplicates با کلید ترکیبی ─────────────────────────────
    seen_keys: set[str] = set()
    deduped = []
    for item in normalized:
        key = item["coin_composition"] + "|||" + item["signature"]
        if key not in seen_keys:
            seen_keys.add(key)
            deduped.append(item)

    print(f"[DEBUG] تعداد آیتم‌های یکتا (بعد از dedup): {len(deduped)}", flush=True)

    # ── فیلتر: فقط آیتم‌هایی که فایل .jsonl دارند ────────────────
    if signatures_dir.exists():
        before_filter = len(deduped)
        deduped = [item for item in deduped
                   if find_jsonl_for_item(item, signatures_dir)]
        skipped = before_filter - len(deduped)
        if skipped > 0:
            print(f"[DEBUG] {skipped} آیتم بدون فایل .jsonl حذف شدند "
                  f"(archive هنوز extract نشده)", flush=True)
    else:
        print(f"⚠️ signatures-dir وجود ندارد: {signatures_dir} — "
              f"فیلتر .jsonl رد می‌شود", flush=True)

    print(f"[DEBUG] تعداد آیتم‌های نهایی برای پردازش: {len(deduped)}", flush=True)

    if len(deduped) == 0:
        print("ℹ️ هیچ آیتم قابل پردازشی یافت نشد → empty.flag", flush=True)
        (output_dir / "empty.flag").touch()
        return

    # ── تقسیم به chunk ────────────────────────────────────────────
    chunks = [deduped[i:i + MAX_CHUNK_SIZE]
              for i in range(0, len(deduped), MAX_CHUNK_SIZE)]
    num_chunks = len(chunks)

    for idx, chunk in enumerate(chunks):
        chunk_path = output_dir / f"chunk_{idx}.json"
        with open(chunk_path, "w", encoding="utf-8") as f:
            json.dump(chunk, f, ensure_ascii=False, indent=2)
        print(f"[DEBUG] chunk_{idx}.json: {len(chunk)} آیتم", flush=True)

    (output_dir / "num_chunks.txt").write_text(str(num_chunks), encoding="utf-8")

    print(f"✅ {num_chunks} chunk تولید شد — مجموع {len(deduped)} آیتم", flush=True)


if __name__ == "__main__":
    main()
