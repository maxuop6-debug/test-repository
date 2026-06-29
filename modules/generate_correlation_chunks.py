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


def build_jsonl_index(signatures_dir: Path) -> tuple:
    """
    یک‌بار همه فایل‌های .jsonl را index می‌کند.
    برمی‌گرداند:
      - stems: مجموعه stem نام فایل‌ها (بدون پسوند، بدون مسیر)
      - rel_paths: مجموعه مسیرهای نسبی به‌صورت "parent_name/stem"
    """
    stems = set()
    rel_paths = set()
    for p in signatures_dir.rglob("*.jsonl"):
        stems.add(p.stem)
        rel_paths.add(f"{p.parent.name}/{p.stem}")
    return stems, rel_paths


def find_jsonl_for_item(item: dict, stems: set, rel_paths: set) -> bool:
    """
    بررسی می‌کند که فایل .jsonl متناظر آیتم در index وجود دارد.
    از index از پیش‌ساخته‌شده استفاده می‌کند (O(1) به‌جای rglob تکراری).
    """
    sig = item.get("signature", "")
    coin = item.get("coin_composition", "")
    if not sig:
        return False

    # ۱. مطابقت دقیق stem
    if sig in stems:
        return True

    # ۲. مطابقت مسیر ترکیبی coin/sig
    if coin and f"{coin}/{sig}" in rel_paths:
        return True

    # ۳. اگر sig شامل پسوند .jsonl بود، بدون پسوند چک کن
    sig_no_ext = sig[:-6] if sig.endswith(".jsonl") else sig
    if sig_no_ext != sig and sig_no_ext in stems:
        return True

    return False


def normalize_item(raw_item: dict):
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
    seen_keys = set()
    deduped = []
    for item in normalized:
        key = item["coin_composition"] + "|||" + item["signature"]
        if key not in seen_keys:
            seen_keys.add(key)
            deduped.append(item)

    print(f"[DEBUG] تعداد آیتم‌های یکتا (بعد از dedup): {len(deduped)}", flush=True)

    # ── ساخت index یک‌باره از همه فایل‌های .jsonl ─────────────────
    if signatures_dir.exists():
        print(f"[DEBUG] در حال ساخت index از {signatures_dir} ...", flush=True)
        stems, rel_paths = build_jsonl_index(signatures_dir)
        print(f"[DEBUG] index آماده شد: {len(stems)} فایل .jsonl منحصربه‌فرد", flush=True)

        # [DIAG] نمونه از stems و آیتم‌های صف برای تشخیص mismatch
        sample_stems = sorted(stems)[:5]
        print(f"[DIAG] نمونه stems در index: {sample_stems}", flush=True)
        sample_items = deduped[:3]
        for it in sample_items:
            print(f"[DIAG] آیتم صف — coin={it.get('coin_composition')} sig={it.get('signature')}", flush=True)

        before_filter = len(deduped)
        matched = []
        missed = []
        for item in deduped:
            if find_jsonl_for_item(item, stems, rel_paths):
                matched.append(item)
            else:
                missed.append(item)

        skipped = before_filter - len(matched)
        if skipped > 0:
            print(f"[DEBUG] {skipped} آیتم بدون فایل .jsonl حذف شدند "
                  f"(archive هنوز extract نشده)", flush=True)
            # [DIAG] نمایش چند آیتم miss‌شده برای تشخیص علت
            for it in missed[:5]:
                print(f"[DIAG] miss — coin={it.get('coin_composition')} "
                      f"sig={it.get('signature')}", flush=True)

        deduped = matched
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
