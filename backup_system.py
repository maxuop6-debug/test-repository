#!/usr/bin/env python3
"""
backup_system.py - سیستم بک‌آپ هوشمند یکپارچه
تمام منطق پردازش در یک فایل واحد
"""

import os
import sys
import json
import csv
import re
import string
import lzma
import tarfile
import hashlib
import random
import struct
import shutil
import logging
import argparse
import tempfile
import io
import time
import subprocess
from datetime import datetime, date, timedelta
from pathlib import Path
from typing import Optional

import requests

# ─── تنظیمات لاگ ───────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("backup")

# ─── ثابت‌ها ────────────────────────────────────────────────────────────────────
EPOCH = date(2000, 1, 1)
PACK_SIZE = 10 * 1024 * 1024          # 10 MB
TELEGRAM_API = "https://api.telegram.org"
ANALYSIS_DIR = Path("analysis_results")
AGGREGATED_DIR = Path("aggregated")
MAPPING_FILE = Path("auto_mapping.json")
LEDGER_FILE = Path("backup_ledger.json")
SCHEDULE_FILE = Path("schedule_times.json")
INTEGRITY_FLAG = Path("integrity_pass.flag")
STARTUP_TEST_FLAG = Path("startup_test_sent.flag")   # پرچم پیام تستی اولین اجرا
IRAN_UTC_OFFSET = 3.5                  # ساعت (UTC+3:30)

# ─── ثابت‌های کارهای جدید (فیلتر/۳۰دقیقه/پرچم/ساختار) ─────────────────────────
ALL_COMBINATIONS_FILE = Path("all_combinations.json")
MASTER_STRUCTURE_FILE = Path("master_structure.json")
BACKUP_DONE_FLAG = ".backup_done"
AGGREGATED_MIN_AGE_MINUTES = 30

# فایل‌های یکبار مصرف: هرکدام می‌تواند پوشه یا فایل باشد
ONE_TIME_TARGETS = [
    Path("data/news"),
    Path("encrypted_data"),
    Path("Test-repo/data.enc"),
    Path("Test-repo/zips"),
    Path("encrypted_data/src"),
]

# ─── ثابت‌های بخش فیلم/سریال و پردازش فورواردی ─────────────────────────────────
MOVIES_DIR = Path("movie_messages")
OFFSET_FILE = Path("update_offset.json")
PENDING_LINKS_FILE = Path("pending_links.json")
FORWARD_KEYWORDS = ["فیلم", "انیمه", "انیمیشن", "سریال"]
URL_RE = re.compile(r"https?://\S+")
MENTION_RE = re.compile(r"@\w+")

# نگاشت ثابت
FIXED_COLUMN_MAP: dict[str, str] = {}

FIXED_VALUE_MAP: dict[str, str] = {
    "0.0": "L", "0.1": "M", "0.2": "Nv", "0.3": "O",   # Nv برای جلوگیری از تداخل با Neutral
    "Good": "G", "Bad": "B", "Neutral": "N",
    "CPI m/m": "C", "Core CPI m/m": "K",
    "PPI m/m": "P", "Core PPI m/m": "R",
    "FOMC": "F", "CPI y/y": "Y",
}

# ─── رمزنگاری AES-256 (بدون کتابخانه خارجی) ───────────────────────────────────

def _derive_key(password: str, salt: bytes) -> bytes:
    """PBKDF2-HMAC-SHA256 key derivation"""
    import hashlib
    return hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 100_000, dklen=32)


def encrypt_data(data: bytes, password: str) -> bytes:
    """رمزنگاری AES-256-CBC با PKCS7 padding"""
    try:
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
        from cryptography.hazmat.primitives import padding as sym_padding
        from cryptography.hazmat.backends import default_backend
        salt = os.urandom(16)
        iv = os.urandom(16)
        key = _derive_key(password, salt)
        padder = sym_padding.PKCS7(128).padder()
        padded = padder.update(data) + padder.finalize()
        cipher = Cipher(algorithms.AES(key), modes.CBC(iv), backend=default_backend())
        enc = cipher.encryptor()
        ct = enc.update(padded) + enc.finalize()
        # فرمت: magic(4) + salt(16) + iv(16) + ciphertext
        return b"ENC1" + salt + iv + ct
    except ImportError:
        log.warning("cryptography not installed – storing without encryption")
        return b"NOENC" + data


def decrypt_data(data: bytes, password: str) -> bytes:
    """رمزگشایی"""
    if data[:5] == b"NOENC":
        return data[5:]
    if data[:4] != b"ENC1":
        raise ValueError("فرمت رمزنگاری نامعتبر")
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    from cryptography.hazmat.primitives import padding as sym_padding
    from cryptography.hazmat.backends import default_backend
    salt = data[4:20]
    iv = data[20:36]
    ct = data[36:]
    key = _derive_key(password, salt)
    cipher = Cipher(algorithms.AES(key), modes.CBC(iv), backend=default_backend())
    dec = cipher.decryptor()
    padded = dec.update(ct) + dec.finalize()
    unpadder = sym_padding.PKCS7(128).unpadder()
    return unpadder.update(padded) + unpadder.finalize()


# ─── فشرده‌سازی LZMA ────────────────────────────────────────────────────────────

def compress_data(data: bytes) -> bytes:
    return lzma.compress(data, preset=9)


def decompress_data(data: bytes) -> bytes:
    return lzma.decompress(data)


# ─── نگاشت ─────────────────────────────────────────────────────────────────────

def load_mapping() -> dict:
    if MAPPING_FILE.exists():
        m = json.loads(MAPPING_FILE.read_text(encoding="utf-8"))
        # اگر reverse_values خالی بود، از values بازسازی کن
        if not m.get("reverse_values"):
            m["reverse_values"] = {v: k for k, v in m.get("values", {}).items()}
        return m
    # ساخت mapping اولیه با reverse_values کامل
    values = dict(FIXED_VALUE_MAP)
    return {
        "columns":        dict(FIXED_COLUMN_MAP),
        "values":         values,
        "reverse_values": {v: k for k, v in values.items()},
    }


def save_mapping(mapping: dict) -> None:
    MAPPING_FILE.write_text(json.dumps(mapping, ensure_ascii=False, indent=2), encoding="utf-8")


def _next_code(used: set[str]) -> str:
    """تولید کد بعدی: A..Z سپس Ba..Bz سپس Baa..."""
    import string
    # تک‌حرفی
    for c in string.ascii_uppercase:
        if c not in used:
            return c
    # دوحرفی (حرف بزرگ + کوچک)
    for c1 in string.ascii_uppercase:
        for c2 in string.ascii_lowercase:
            code = c1 + c2
            if code not in used:
                return code
    # سه‌حرفی
    for c1 in string.ascii_uppercase:
        for c2 in string.ascii_lowercase:
            for c3 in string.ascii_lowercase:
                code = c1 + c2 + c3
                if code not in used:
                    return code
    raise RuntimeError("فضای کد تمام شد")


def auto_detect_mapping(csv_paths: list[Path]) -> dict:
    """کشف خودکار کلمات جدید برای نگاشت"""
    mapping = load_mapping()
    existing_values = set(mapping["values"].keys())
    used_codes = set(mapping["values"].values()) | set(mapping["columns"].values())

    freq: dict[str, int] = {}
    for p in csv_paths:
        try:
            with open(p, newline="", encoding="utf-8") as f:
                reader = csv.reader(f)
                for row in reader:
                    for cell in row:
                        cell = cell.strip()
                        # غیرعددی، حداقل ۳ حرف، نه تاریخ
                        if cell and len(cell) >= 3 and not _is_numeric(cell) and not _is_date(cell):
                            freq[cell] = freq.get(cell, 0) + 1
        except Exception as e:
            log.warning(f"خطا در خواندن {p}: {e}")

    # کاندیداها: بیش از ۵ تکرار و در نگاشت نیستند
    candidates = sorted(
        [(v, c) for v, c in freq.items() if c > 5 and v not in existing_values],
        key=lambda x: -x[1]
    )

    for value, _ in candidates:
        code = _next_code(used_codes)
        mapping["values"][value] = code
        mapping["reverse_values"][code] = value
        used_codes.add(code)
        log.info(f"نگاشت جدید: '{value}' → '{code}'")

    save_mapping(mapping)
    return mapping


def _is_numeric(s: str) -> bool:
    try:
        float(s)
        return True
    except ValueError:
        return False


def _is_date(s: str) -> bool:
    try:
        datetime.strptime(s, "%Y-%m-%d")
        return True
    except ValueError:
        return False


def _date_to_int(s: str) -> str:
    try:
        d = datetime.strptime(s.strip(), "%Y-%m-%d").date()
        return str((d - EPOCH).days)
    except ValueError:
        return s


def apply_mapping(csv_path: Path, mapping: dict) -> bytes:
    """اعمال نگاشت روی CSV و برگرداندن bytes"""
    col_map = mapping.get("columns", {})
    val_map = mapping.get("values", {})

    out_rows = []
    with open(csv_path, newline="", encoding="utf-8") as f:
        reader = csv.reader(f)
        headers = next(reader, None)
        if headers:
            mapped_headers = [col_map.get(h, h) for h in headers]
            out_rows.append(mapped_headers)
            for row in reader:
                new_row = []
                for h, cell in zip(headers, row):
                    cell = cell.strip()
                    if _is_date(cell):
                        new_row.append(_date_to_int(cell))
                    elif cell in val_map:
                        new_row.append(val_map[cell])
                    else:
                        new_row.append(cell)
                out_rows.append(new_row)

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerows(out_rows)
    return buf.getvalue().encode("utf-8")


def reverse_mapping(csv_bytes: bytes, mapping: dict) -> bytes:
    """معکوس نگاشت"""
    rev_col = {v: k for k, v in mapping.get("columns", {}).items()}
    rev_val = mapping.get("reverse_values", {v: k for k, v in mapping.get("values", {}).items()})

    buf = io.StringIO(csv_bytes.decode("utf-8"))
    reader = csv.reader(buf)
    out_rows = []
    headers = next(reader, None)
    if headers:
        orig_headers = [rev_col.get(h, h) for h in headers]
        out_rows.append(orig_headers)
        for row in reader:
            new_row = []
            for cell in row:
                cell = cell.strip()
                if _is_numeric(cell) and "." not in cell:
                    # احتمالاً تاریخ عددی است
                    try:
                        d = EPOCH + timedelta(days=int(cell))
                        new_row.append(d.strftime("%Y-%m-%d"))
                        continue
                    except Exception:
                        pass
                new_row.append(rev_val.get(cell, cell))
            out_rows.append(new_row)

    out_buf = io.StringIO()
    csv.writer(out_buf).writerows(out_rows)
    return out_buf.getvalue().encode("utf-8")


# ─── تست یکپارچگی ──────────────────────────────────────────────────────────────

def integrity_test(password: str, bot_token: str, chat_id: str) -> bool:
    """تست کامل زنجیره پردازش – بدون وابستگی به تلگرام"""
    if INTEGRITY_FLAG.exists():
        log.info("تست یکپارچگی قبلاً انجام شده – اسکیپ")
        return True

    log.info("شروع تست یکپارچگی...")

    # داده نمونه با تمام مقادیر نگاشت‌شده
    sample_rows = [["date", "indicator", "threshold", "status"]]
    statuses   = ["Good", "Bad", "Neutral", "Good"]
    thresholds = ["0.0", "0.1", "0.2", "0.3"]
    for i in range(50):
        d = (EPOCH + timedelta(days=i * 7)).strftime("%Y-%m-%d")
        sample_rows.append([d, "CPI m/m", thresholds[i % 4], statuses[i % 4]])

    with tempfile.NamedTemporaryFile(suffix=".csv", delete=False, mode="w",
                                     newline="", encoding="utf-8") as tf:
        csv.writer(tf).writerows(sample_rows)
        tmp_path = Path(tf.name)

    try:
        mapping = load_mapping()

        # زنجیره کامل بدون تلگرام
        mapped     = apply_mapping(tmp_path, mapping)
        compressed = compress_data(mapped)
        encrypted  = encrypt_data(compressed, password)
        dec        = decrypt_data(encrypted, password)
        decomp     = decompress_data(dec)
        restored   = reverse_mapping(decomp, mapping)

        # مقایسه خط‌به‌خط
        original_content = tmp_path.read_bytes()
        orig_rows = list(csv.reader(io.StringIO(original_content.decode("utf-8"))))
        rest_rows = list(csv.reader(io.StringIO(restored.decode("utf-8"))))

        if orig_rows == rest_rows:
            INTEGRITY_FLAG.write_text(datetime.utcnow().isoformat())
            log.info("✅ تست یکپارچگی موفق")
            return True
        else:
            for i, (a, b) in enumerate(zip(orig_rows, rest_rows)):
                if a != b:
                    log.error(f"تفاوت ردیف {i}: {a} ≠ {b}")
                    break
            log.error("❌ تست یکپارچگی ناموفق – داده‌ها مطابقت ندارند")
            return False
    finally:
        tmp_path.unlink(missing_ok=True)


# ─── تلگرام ────────────────────────────────────────────────────────────────────

def telegram_send_document(bot_token: str, chat_id: str, data: bytes,
                            filename: str, caption: str = "") -> Optional[str]:
    """ارسال فایل به تلگرام و برگرداندن file_id"""
    url = f"{TELEGRAM_API}/bot{bot_token}/sendDocument"
    for attempt in range(3):
        try:
            resp = requests.post(
                url,
                data={"chat_id": chat_id, "caption": caption},
                files={"document": (filename, data, "application/octet-stream")},
                timeout=120,
            )
            result = resp.json()
            if result.get("ok"):
                return result["result"]["document"]["file_id"]
            log.warning(f"تلگرام: {result.get('description')} (تلاش {attempt+1})")
        except Exception as e:
            log.warning(f"خطای ارسال: {e} (تلاش {attempt+1})")
        time.sleep(5 * (attempt + 1))
    return None


def telegram_download_file(bot_token: str, file_id: str) -> bytes:
    """دانلود فایل از تلگرام"""
    # دریافت مسیر فایل
    url = f"{TELEGRAM_API}/bot{bot_token}/getFile"
    resp = requests.get(url, params={"file_id": file_id}, timeout=30)
    file_path = resp.json()["result"]["file_path"]
    # دانلود
    dl_url = f"{TELEGRAM_API}/file/bot{bot_token}/{file_path}"
    resp2 = requests.get(dl_url, timeout=120)
    return resp2.content


# ─── دفترچه ────────────────────────────────────────────────────────────────────

def load_ledger() -> dict:
    if LEDGER_FILE.exists():
        return json.loads(LEDGER_FILE.read_text(encoding="utf-8"))
    return {"uploads": []}


def save_ledger(ledger: dict) -> None:
    LEDGER_FILE.write_text(json.dumps(ledger, ensure_ascii=False, indent=2), encoding="utf-8")


def is_uploaded(ledger: dict, rel_path: str) -> bool:
    return any(u.get("path") == rel_path for u in ledger["uploads"])


def record_upload(ledger: dict, rel_path: str, file_id: str, size: int) -> None:
    ledger["uploads"].append({
        "path": rel_path,
        "file_id": file_id,
        "size": size,
        "timestamp": datetime.utcnow().isoformat(),
    })
    save_ledger(ledger)


# ─── کار ۱: فیلتر analysis_results بر اساس all_combinations.json ───────────────

def load_completed_strategies() -> set[str]:
    """خواندن استراتژی‌های تکمیل‌شده از all_combinations.json"""
    if not ALL_COMBINATIONS_FILE.exists():
        return set()
    try:
        data = json.loads(ALL_COMBINATIONS_FILE.read_text(encoding="utf-8"))
    except Exception as e:
        log.warning(f"خطا در خواندن {ALL_COMBINATIONS_FILE}: {e}")
        return set()
    if isinstance(data, dict):
        # ممکن است {"completed_strategies": [...]} یا مستقیماً دیکشنری کلید-استراتژی باشد
        if "completed_strategies" in data:
            return set(str(s) for s in data["completed_strategies"])
        return set(str(k) for k in data.keys())
    if isinstance(data, list):
        return set(str(s) for s in data)
    return set()


def _strategy_folder(csv_path: Path) -> Optional[Path]:
    """analysis_results/[ماژول]/[استراتژی]/[کوین]/[فایل].csv -> پوشه استراتژی"""
    try:
        idx = csv_path.parts.index(ANALYSIS_DIR.name)
    except ValueError:
        return None
    if len(csv_path.parts) < idx + 3:
        return None
    return Path(*csv_path.parts[: idx + 3])  # .../analysis_results/module/strategy


def _strategy_name(csv_path: Path) -> Optional[str]:
    folder = _strategy_folder(csv_path)
    return folder.name if folder else None


def filter_completed_strategies(csv_files: list[Path]) -> list[Path]:
    """فقط CSVهایی که استراتژی‌شان در all_combinations.json نیست را نگه می‌دارد"""
    completed = load_completed_strategies()
    if not completed:
        return csv_files
    kept = []
    for p in csv_files:
        name = _strategy_name(p)
        if name is not None and name in completed:
            continue
        kept.append(p)
    return kept


# ─── کار ۲: شرط ۳۰ دقیقه + پرچم برای aggregated/ ───────────────────────────────

def collect_aggregated_files() -> list[Path]:
    """فقط زیرپوشه‌هایی که ≥۳۰ دقیقه تغییر نکرده و .backup_done ندارند"""
    if not AGGREGATED_DIR.exists():
        return []
    now = time.time()
    result: list[Path] = []
    for sub in sorted(AGGREGATED_DIR.iterdir()):
        if not sub.is_dir():
            continue
        if (sub / BACKUP_DONE_FLAG).exists():
            continue
        enc_files = list(sub.glob("*.enc"))
        if not enc_files:
            continue
        latest_mtime = max(f.stat().st_mtime for f in enc_files)
        age_minutes = (now - latest_mtime) / 60
        if age_minutes < AGGREGATED_MIN_AGE_MINUTES:
            continue
        result.extend(enc_files)
    return result


# ─── کار ۳: گذاشتن پرچم .backup_done بعد از آپلود موفق ─────────────────────────

def _touch_backup_done(folder: Path) -> None:
    try:
        folder.mkdir(parents=True, exist_ok=True)
        (folder / BACKUP_DONE_FLAG).write_text(datetime.utcnow().isoformat(), encoding="utf-8")
    except Exception as e:
        log.warning(f"عدم موفقیت در ایجاد پرچم {folder}: {e}")


def mark_uploaded_folders_done(uploaded_rel_paths: list[str]) -> set[Path]:
    """بعد از آپلود موفق، پرچم .backup_done را در پوشه‌های استراتژی/aggregated می‌گذارد.
    پوشه‌های پرچم‌گذاری‌شده را برمی‌گرداند (برای ساخت structure.json)."""
    touched: set[Path] = set()
    for rel in uploaded_rel_paths:
        p = Path(rel)
        if ANALYSIS_DIR.name in p.parts:
            folder = _strategy_folder(p)
            if folder:
                touched.add(folder)
        elif AGGREGATED_DIR.name in p.parts:
            try:
                idx = p.parts.index(AGGREGATED_DIR.name)
                folder = Path(*p.parts[: idx + 2])
                touched.add(folder)
            except ValueError:
                pass
    for folder in touched:
        _touch_backup_done(folder)
    return touched


# ─── کار ۴: فایل‌های یکبار مصرف ─────────────────────────────────────────────────

def collect_onetime_files() -> list[Path]:
    """جمع‌آوری فایل‌های یکبار مصرف که هنوز .backup_done ندارند"""
    files: list[Path] = []
    for target in ONE_TIME_TARGETS:
        if not target.exists():
            continue
        flag_dir = target if target.is_dir() else target.parent
        if (flag_dir / BACKUP_DONE_FLAG).exists():
            continue
        if target.is_file():
            files.append(target)
        else:
            files.extend(
                p for p in sorted(target.rglob("*"))
                if p.is_file() and p.name != BACKUP_DONE_FLAG
            )
    return files


def mark_onetime_targets_done() -> None:
    """بعد از آپلود موفق فایل‌های یکبار مصرف، پرچم .backup_done می‌گذارد"""
    for target in ONE_TIME_TARGETS:
        if not target.exists():
            continue
        flag_dir = target if target.is_dir() else target.parent
        _touch_backup_done(flag_dir)


def process_onetime_file(path: Path, password: str) -> bytes:
    """فشرده‌سازی + رمزنگاری ساده برای فایل‌های یکبار مصرف (بدون نگاشت)"""
    raw = path.read_bytes()
    compressed = compress_data(raw)
    return encrypt_data(compressed, password)


# ─── کار ۸: structure.json (هر پوشه) + master_structure.json (ریشه) ───────────

def _sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def write_structure_json(folder: Path) -> dict:
    """قبل از بسته‌بندی، structure.json را داخل پوشه می‌سازد"""
    files_info = []
    if folder.exists():
        for f in sorted(folder.iterdir()):
            if f.is_file() and f.name not in ("structure.json", BACKUP_DONE_FLAG):
                files_info.append({
                    "name": f.name,
                    "size": f.stat().st_size,
                    "hash": _sha256_of(f),
                })
    struct = {
        "folder_name": folder.name,
        "folder_path": str(folder),
        "files": files_info,
        "total_files": len(files_info),
        "total_size_bytes": sum(fi["size"] for fi in files_info),
        "upload_date": datetime.utcnow().isoformat() + "Z",
        "telegram_file_id": None,
    }
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "structure.json").write_text(
        json.dumps(struct, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return struct


def update_structure_telegram_id(folder: Path, file_id: str) -> None:
    sp = folder / "structure.json"
    if not sp.exists():
        return
    try:
        d = json.loads(sp.read_text(encoding="utf-8"))
        d["telegram_file_id"] = file_id
        sp.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as e:
        log.warning(f"عدم بروزرسانی structure.json در {folder}: {e}")


def load_master_structure() -> dict:
    if MASTER_STRUCTURE_FILE.exists():
        try:
            return json.loads(MASTER_STRUCTURE_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {
        "last_updated": None,
        "total_folders": 0,
        "total_files": 0,
        "total_size_bytes": 0,
        "folders": [],
    }


def update_master_structure(folder_struct: dict, package_name: str) -> None:
    """master_structure.json در ریشه مخزن را با اطلاعات پوشه جدید به‌روز می‌کند"""
    master = load_master_structure()
    master["folders"] = [
        f for f in master["folders"] if f.get("folder_path") != folder_struct["folder_path"]
    ]
    master["folders"].append({
        "folder_name": folder_struct["folder_name"],
        "folder_path": folder_struct["folder_path"],
        "total_files": folder_struct["total_files"],
        "total_size_bytes": folder_struct["total_size_bytes"],
        "upload_date": folder_struct["upload_date"],
        "telegram_file_id": folder_struct.get("telegram_file_id"),
        "package_name": package_name,
    })
    master["total_folders"] = len(master["folders"])
    master["total_files"] = sum(f["total_files"] for f in master["folders"])
    master["total_size_bytes"] = sum(f["total_size_bytes"] for f in master["folders"])
    master["last_updated"] = datetime.utcnow().isoformat() + "Z"
    MASTER_STRUCTURE_FILE.write_text(
        json.dumps(master, ensure_ascii=False, indent=2), encoding="utf-8"
    )


# ─── پردازش فایل‌ها ─────────────────────────────────────────────────────────────

def process_analysis_file(path: Path, mapping: dict, password: str) -> bytes:
    """نگاشت + فشرده‌سازی + رمزنگاری برای فایل‌های analysis_results"""
    mapped = apply_mapping(path, mapping)
    compressed = compress_data(mapped)
    return encrypt_data(compressed, password)


def process_aggregated_file(path: Path, password: str) -> bytes:
    """فشرده‌سازی + رمزنگاری برای فایل‌های aggregated (که .enc هستند)"""
    raw = path.read_bytes()
    # اگر از قبل رمزنگاری شده، فقط بسته‌بندی کن
    compressed = compress_data(raw)
    return encrypt_data(compressed, password)


# ─── بسته‌بندی هوشمند ──────────────────────────────────────────────────────────

def build_packages(files: list[tuple[Path, bytes]], base_dirs: list[Path]) -> list[dict]:
    """
    ساخت بسته‌های حداکثر ۱۰ مگابایت
    هر بسته یک dict: {"name": str, "data": bytes, "files": [str]}
    """
    packages = []
    current_files = []
    current_size = 0
    timestamp = datetime.utcnow().strftime("%Y-%m-%d_%H-%M-%S")
    pack_idx = 0

    def flush():
        nonlocal current_files, current_size, pack_idx
        if not current_files:
            return
        name = f"backup_{timestamp}_{pack_idx:03d}.tar.xz.enc"
        pkg_data, file_list = _create_tar(current_files)
        packages.append({"name": name, "data": pkg_data, "files": file_list})
        pack_idx += 1
        current_files = []
        current_size = 0

    for path, data in files:
        # تعیین مسیر نسبی
        for base in base_dirs:
            try:
                rel = path.relative_to(base.parent)
                break
            except ValueError:
                rel = path
        
        if len(data) >= PACK_SIZE:
            # فایل بزرگ: بسته‌ی جداگانه
            flush()
            name = f"backup_{timestamp}_{pack_idx:03d}_large.tar.xz.enc"
            pkg_data, file_list = _create_tar([(path, data)])
            packages.append({"name": name, "data": pkg_data, "files": file_list})
            pack_idx += 1
            continue

        if current_size + len(data) > PACK_SIZE:
            flush()

        current_files.append((path, data))
        current_size += len(data)

    flush()
    return packages


def _create_tar(files: list[tuple[Path, bytes]]) -> tuple[bytes, list[str]]:
    """ساخت فایل tar.xz در حافظه"""
    file_list = []
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:xz") as tar:
        for path, data in files:
            rel = str(path)
            file_list.append(rel)
            info = tarfile.TarInfo(name=rel)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
        # اضافه کردن index.json
        idx = json.dumps({"files": file_list, "created": datetime.utcnow().isoformat()},
                         ensure_ascii=False, indent=2).encode("utf-8")
        idx_info = tarfile.TarInfo(name="index.json")
        idx_info.size = len(idx)
        tar.addfile(idx_info, io.BytesIO(idx))
    return buf.getvalue(), file_list


# ─── زمان‌بندی ──────────────────────────────────────────────────────────────────

def generate_schedule() -> dict:
    """تولید ۴ زمان رندوم بین ۱۲ ظهر تا ۱۲ شب به وقت ایران"""
    today = date.today().isoformat()
    if SCHEDULE_FILE.exists():
        sched = json.loads(SCHEDULE_FILE.read_text(encoding="utf-8"))
        if sched.get("date") == today:
            return sched

    # وقت ایران = UTC+3:30
    # ۱۲ ظهر ایران = 8:30 UTC، ۱۲ شب ایران = 20:30 UTC
    start_min = 8 * 60 + 30   # 8:30 UTC
    end_min = 20 * 60 + 30    # 20:30 UTC

    times_min = sorted(random.sample(range(start_min, end_min), 4))
    times_str = [f"{m // 60:02d}:{m % 60:02d}" for m in times_min]

    sched = {"date": today, "times_utc": times_str}
    SCHEDULE_FILE.write_text(json.dumps(sched, indent=2), encoding="utf-8")
    log.info(f"زمان‌بندی امروز (UTC): {times_str}")
    return sched


def should_run_now(sched: dict, window_minutes: int = 5) -> bool:
    """آیا زمان فعلی با یکی از زمان‌های برنامه‌ریزی‌شده مطابقت دارد؟"""
    now_utc = datetime.utcnow()
    now_min = now_utc.hour * 60 + now_utc.minute

    for t in sched.get("times_utc", []):
        h, m = map(int, t.split(":"))
        sched_min = h * 60 + m
        if abs(now_min - sched_min) <= window_minutes:
            return True
    return False


# ─── ارسال متادیتا ──────────────────────────────────────────────────────────────

def send_metadata(bot_token: str, chat_id: str, password: str) -> None:
    """ارسال دفترچه، نگاشت و پرچم به تلگرام"""
    meta_files = [LEDGER_FILE, MAPPING_FILE, INTEGRITY_FLAG, SCHEDULE_FILE, MASTER_STRUCTURE_FILE]
    existing = [(p, p.read_bytes()) for p in meta_files if p.exists()]
    if not existing:
        return

    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:xz") as tar:
        for p, data in existing:
            info = tarfile.TarInfo(name=p.name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))

    packed = buf.getvalue()
    encrypted = encrypt_data(packed, password)
    fname = f"metadata_{date.today().isoformat()}.tar.xz.enc"
    fid = telegram_send_document(bot_token, chat_id, encrypted, fname,
                                  caption=f"📋 متادیتا – {date.today().isoformat()}")
    if fid:
        log.info(f"✅ متادیتا ارسال شد: {fname}")


# ─── ارسال هفتگی ────────────────────────────────────────────────────────────────

def weekly_full_backup(bot_token: str, chat_id: str, password: str,
                        repo_names: list[str]) -> None:
    """کلون و ارسال کامل مخازن"""
    today = date.today()
    if today.weekday() not in (3, 4):   # پنج‌شنبه=3، جمعه=4
        log.info("روز ارسال هفتگی نیست")
        return

    gh_token = os.environ.get("GITHUB_TOKEN", "")
    gh_user = os.environ.get("GITHUB_ACTOR", "")

    for repo in repo_names:
        with tempfile.TemporaryDirectory() as tmpdir:
            clone_url = f"https://{gh_user}:{gh_token}@github.com/{gh_user}/{repo}.git"
            result = subprocess.run(
                ["git", "clone", "--depth", "1", clone_url, tmpdir],
                capture_output=True, text=True
            )
            if result.returncode != 0:
                log.error(f"کلون ناموفق: {repo} – {result.stderr}")
                continue

            # آرشیو کل مخزن (با حذف فایل‌های یکبار مصرفِ پرچم‌خورده)
            buf = io.BytesIO()
            skip_names = {".git"}
            with tarfile.open(fileobj=buf, mode="w:xz") as tar:
                def _filter(tarinfo: tarfile.TarInfo) -> Optional[tarfile.TarInfo]:
                    parts = Path(tarinfo.name).parts
                    if any(p in skip_names for p in parts):
                        return None
                    # رد کردن فایل‌های یکبار مصرفی که پرچم .backup_done دارند
                    for target in ONE_TIME_TARGETS:
                        target_parts = target.parts
                        if parts[1: 1 + len(target_parts)] == target_parts:
                            # parts[0] = نام مخزن (arcname)
                            flag_dir = target if target.is_dir() else target.parent
                            if (flag_dir / BACKUP_DONE_FLAG).exists():
                                return None
                    return tarinfo
                tar.add(tmpdir, arcname=repo, filter=_filter)
            packed = buf.getvalue()
            encrypted = encrypt_data(packed, password)
            fname = f"weekly_{repo}_{today.isoformat()}.tar.xz.enc"
            fid = telegram_send_document(bot_token, chat_id, encrypted, fname,
                                          caption=f"📦 هفتگی: {repo}")
            if fid:
                log.info(f"✅ ارسال هفتگی: {repo}")


# ─── ارسال پیام فیلم/سریال بعد از بک‌آپ ────────────────────────────────────────

def load_unused_movies() -> list[Path]:
    """لیست فایل‌های movie_messages/*.json با used=false"""
    if not MOVIES_DIR.exists():
        return []
    unused = []
    for p in sorted(MOVIES_DIR.glob("*.json")):
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            if not data.get("used", False):
                unused.append(p)
        except Exception as e:
            log.warning(f"خطا در خواندن {p}: {e}")
    return unused


def _send_one_movie_message(bot_token: str, chat_id: str, path: Path) -> bool:
    """ارسال یک فایل movie_messages مشخص. در صورت موفقیت True و used=true می‌کند"""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        log.warning(f"خطا در خواندن {path}: {e}")
        return False

    text = data.get("text", "")
    image_url = data.get("image_url")
    photo_file_id = data.get("photo_file_id")

    try:
        if photo_file_id:
            url = f"{TELEGRAM_API}/bot{bot_token}/sendPhoto"
            payload = {"chat_id": chat_id, "photo": photo_file_id, "caption": text}
        elif image_url:
            url = f"{TELEGRAM_API}/bot{bot_token}/sendPhoto"
            payload = {"chat_id": chat_id, "photo": image_url, "caption": text}
        else:
            url = f"{TELEGRAM_API}/bot{bot_token}/sendMessage"
            payload = {"chat_id": chat_id, "text": text}
        resp = requests.post(url, json=payload, timeout=30)
        result = resp.json()
        if not result.get("ok"):
            log.error(f"ارسال پیام فیلم ناموفق: {result.get('description')}")
            return False
    except Exception as e:
        log.error(f"خطای ارسال پیام فیلم: {e}")
        return False

    data["used"] = True
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    log.info(f"✅ پیام فیلم/سریال ارسال شد: {path.name}")
    return True


def send_movie_message(bot_token: str, chat_id: str) -> None:
    """انتخاب و ارسال یک پیام فیلم/سریال استفاده‌نشده (ارسال تکی – برای دستور send-movie و پیام تست اولیه)"""
    unused = load_unused_movies()
    if not unused:
        log.info("هیچ پیام استفاده‌نشده‌ای در movie_messages/ وجود ندارد")
        return
    _send_one_movie_message(bot_token, chat_id, random.choice(unused))


def send_movie_messages_after_backup(bot_token: str, chat_id: str) -> None:
    """بعد از هر بک‌آپ موفق: ۱ یا ۲ پیام رندوم از بین used=false ارسال می‌کند"""
    unused = load_unused_movies()
    if not unused:
        log.info("هیچ پیام استفاده‌نشده‌ای در movie_messages/ وجود ندارد")
        return
    count = min(random.randint(1, 2), len(unused))
    chosen = random.sample(unused, count)
    sent = 0
    for path in chosen:
        if _send_one_movie_message(bot_token, chat_id, path):
            sent += 1
    log.info(f"✅ {sent} پیام فیلم/سریال بعد از بک‌آپ موفق ارسال شد")


# ─── پردازش پیام‌های فورواردی + لینک اختصاصی + بررسی عضویت ────────────────────

def _load_offset() -> int:
    if OFFSET_FILE.exists():
        try:
            return json.loads(OFFSET_FILE.read_text(encoding="utf-8")).get("offset", 0)
        except Exception:
            return 0
    return 0


def _save_offset(offset: int) -> None:
    OFFSET_FILE.write_text(json.dumps({"offset": offset}), encoding="utf-8")


def _load_pending() -> dict:
    if PENDING_LINKS_FILE.exists():
        try:
            return json.loads(PENDING_LINKS_FILE.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def _save_pending(data: dict) -> None:
    PENDING_LINKS_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _gen_unique_id(existing: set) -> str:
    while True:
        uid = "".join(random.choices(string.ascii_lowercase + string.digits, k=8))
        if uid not in existing:
            return uid


def _get_updates(bot_token: str, offset: int) -> list:
    url = f"{TELEGRAM_API}/bot{bot_token}/getUpdates"
    params = {"offset": offset, "timeout": 10, "allowed_updates": json.dumps(["channel_post", "message"])}
    try:
        resp = requests.get(url, params=params, timeout=30)
        data = resp.json()
    except Exception as e:
        log.error(f"خطا در getUpdates: {e}")
        return []
    if not data.get("ok"):
        log.warning(f"getUpdates ناموفق: {data}")
        return []
    return data["result"]


def _tg_send_message(bot_token: str, chat_id, text: str, reply_markup: Optional[dict] = None) -> None:
    url = f"{TELEGRAM_API}/bot{bot_token}/sendMessage"
    payload = {"chat_id": chat_id, "text": text}
    if reply_markup:
        payload["reply_markup"] = json.dumps(reply_markup)
    try:
        requests.post(url, json=payload, timeout=30)
    except Exception as e:
        log.error(f"خطا در ارسال پیام: {e}")


def _tg_send_photo(bot_token: str, chat_id, photo_file_id: str, caption: str) -> None:
    url = f"{TELEGRAM_API}/bot{bot_token}/sendPhoto"
    payload = {"chat_id": chat_id, "photo": photo_file_id, "caption": caption}
    try:
        requests.post(url, json=payload, timeout=30)
    except Exception as e:
        log.error(f"خطا در ارسال عکس: {e}")


def _check_membership(bot_token: str, main_chat_id: str, user_id: int) -> bool:
    url = f"{TELEGRAM_API}/bot{bot_token}/getChatMember"
    try:
        resp = requests.get(url, params={"chat_id": main_chat_id, "user_id": user_id}, timeout=15)
        data = resp.json()
    except Exception as e:
        log.error(f"خطا در بررسی عضویت: {e}")
        return False
    if not data.get("ok"):
        return False
    return data["result"]["status"] in ("member", "administrator", "creator")


def _has_forward_keyword(text: str) -> bool:
    return any(k in text for k in FORWARD_KEYWORDS)


def _extract_links_from_post(post: dict) -> list[str]:
    """استخراج لینک از متن خام و همچنین از entities (text_link)"""
    text = post.get("text") or post.get("caption") or ""
    # لینک‌های خام در متن
    links = URL_RE.findall(text)
    # لینک‌های پنهان در entities (مثل hyperlink روی کلمه)
    entities = post.get("entities") or post.get("caption_entities") or []
    for ent in entities:
        if ent.get("type") == "text_link":
            url = ent.get("url", "")
            if url and url not in links:
                links.append(url)
    return links


def _clean_forward_text(text: str, main_channel_username: str) -> str:
    """پاکسازی متن: حذف لینک‌های خام و جایگزینی منشن‌ها"""
    text_no_links = URL_RE.sub("", text).strip()
    text_no_at = MENTION_RE.sub(f"@{main_channel_username}", text_no_links)
    return text_no_at.strip()


def _process_forwarded_update(update: dict, bot_token: str, bot_username: str,
                               main_chat_id: str, main_channel_username: str,
                               intermediate_chat_id: str, pending: dict) -> None:
    post = update.get("channel_post")
    if not post:
        return
    if str(post.get("chat", {}).get("id")) != str(intermediate_chat_id):
        return

    text = post.get("text") or post.get("caption") or ""
    if not text or not _has_forward_keyword(text):
        return

    links = _extract_links_from_post(post)
    cleaned_text = _clean_forward_text(text, main_channel_username)
    if not links:
        log.info("پیام فورواردی بدون لینک – نادیده گرفته شد")
        return

    uid = _gen_unique_id(set(pending.keys()))
    pending[uid] = {"link": links[0], "created": datetime.utcnow().isoformat()}
    _save_pending(pending)

    deep_link = f"https://t.me/{bot_username}?start={uid}"
    msg = (
        f"{cleaned_text}\n\n"
        f"📥 برای دانلود، روی لینک زیر کلیک کنید:\n"
        f"🔗 {deep_link}\n\n"
        f"🔹 ابتدا در کانال ما عضو شوید:\n"
        f"👉 @{main_channel_username}"
    )

    # ── ذخیره در movie_messages/ برای استفاده بعدی توسط send_movie_message ──
    MOVIES_DIR.mkdir(exist_ok=True)
    movie_entry = {
        "text": msg,
        "image_url": None,
        "used": False,
        "source_uid": uid,
        "created": datetime.utcnow().isoformat(),
    }
    photo_id = post["photo"][-1]["file_id"] if post.get("photo") else None
    if photo_id:
        movie_entry["photo_file_id"] = photo_id
    movie_file = MOVIES_DIR / f"movie_{uid}.json"
    movie_file.write_text(json.dumps(movie_entry, ensure_ascii=False, indent=2), encoding="utf-8")
    log.info(f"💾 پیام در movie_messages ذخیره شد و برای ارسال بعد از بک‌آپ موفق صف شد: {movie_file.name}")
    # ⚠️ عمداً اینجا چیزی به main_chat_id ارسال نمی‌شود.
    # ارسال واقعی فقط در send_movie_messages_after_backup (بعد از هر بک‌آپ موفق، ۱ یا ۲ پیام رندوم) انجام می‌شود.


def _process_start_command(update: dict, bot_token: str, main_chat_id: str,
                            main_channel_username: str, pending: dict) -> None:
    msg = update.get("message")
    if not msg or "text" not in msg:
        return
    text = msg["text"]
    if not text.startswith("/start"):
        return

    chat_id = msg["chat"]["id"]
    user_id = msg["from"]["id"]
    parts = text.split(maxsplit=1)

    if len(parts) < 2:
        _tg_send_message(bot_token, chat_id, "سلام! لینک دانلود را از کانال دریافت کنید.")
        return

    uid = parts[1].strip()
    entry = pending.get(uid)
    if not entry:
        _tg_send_message(bot_token, chat_id, "❌ این لینک منقضی شده یا نامعتبر است.")
        return

    if _check_membership(bot_token, main_chat_id, user_id):
        _tg_send_message(bot_token, chat_id, f"✅ لینک دانلود شما:\n{entry['link']}")
    else:
        keyboard = {"inline_keyboard": [[
            {"text": "📢 عضویت در کانال", "url": f"https://t.me/{main_channel_username}"}
        ]]}
        _tg_send_message(
            bot_token, chat_id,
            "⚠️ برای دریافت لینک، ابتدا باید عضو کانال ما شوید:\n\n"
            "بعد از عضویت دوباره لینک را بزنید و استارت کنید.\n"
            "⏳ حدوداً ۳۰ ثانیه صبر کنید تا ربات جواب بدهد.",
            reply_markup=keyboard,
        )


def _send_startup_movie(bot_token: str, chat_id: str) -> None:
    """اولین اجرا: یک پیام فیلم/سریال تصادفی ارسال می‌کند و پرچم می‌گذارد تا دفعات بعد skip شود"""
    if STARTUP_TEST_FLAG.exists():
        return
    log.info("اولین اجرا – ارسال پیام فیلم/سریال تستی...")
    send_movie_message(bot_token, chat_id)
    STARTUP_TEST_FLAG.write_text(datetime.utcnow().isoformat())
    log.info("✅ پیام فیلم/سریال تستی ارسال شد، پرچم گذاشته شد")


def process_forward_updates() -> None:
    """دریافت آپدیت‌های جدید تلگرام و پردازش پیام‌های فورواردی + دستورات /start"""
    bot_token = os.environ.get("TELEGRAM_BOT_TOKEN")
    bot_username = os.environ.get("TELEGRAM_BOT_USERNAME")
    main_chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    main_channel_username = os.environ.get("MAIN_CHANNEL_USERNAME")
    intermediate_chat_id = os.environ.get("INTERMEDIATE_CHAT_ID")

    missing = [n for n, v in [
        ("TELEGRAM_BOT_TOKEN", bot_token), ("TELEGRAM_BOT_USERNAME", bot_username),
        ("TELEGRAM_CHAT_ID", main_chat_id), ("MAIN_CHANNEL_USERNAME", main_channel_username),
        ("INTERMEDIATE_CHAT_ID", intermediate_chat_id),
    ] if not v]
    if missing:
        log.error(f"متغیرهای محیطی زیر تنظیم نشده‌اند: {', '.join(missing)}")
        return

    offset = _load_offset()
    pending = _load_pending()

    # ── اولین اجرا: ارسال پیام فیلم/سریال و گذاشتن پرچم ──
    _send_startup_movie(bot_token, main_chat_id)

    updates = _get_updates(bot_token, offset)
    if not updates:
        log.info("هیچ آپدیت جدیدی نیست")
        return

    for update in updates:
        new_offset = update["update_id"] + 1
        try:
            if "channel_post" in update:
                _process_forwarded_update(update, bot_token, bot_username, main_chat_id,
                                           main_channel_username, intermediate_chat_id, pending)
            elif "message" in update:
                _process_start_command(update, bot_token, main_chat_id,
                                        main_channel_username, pending)
        except Exception as e:
            log.error(f"خطا در پردازش آپدیت {update.get('update_id')}: {e}")
        finally:
            # offset رو همیشه ذخیره کن، حتی اگر خطا بخوره
            if new_offset > offset:
                offset = new_offset
                _save_offset(offset)

    log.info(f"✅ {len(updates)} آپدیت پردازش شد، offset={offset}")




def trigger_loop_workflow(gh_token: str, repo_full: str, workflow_file: str) -> None:
    """فراخوانی ورکفلو دیگر از طریق GitHub API"""
    url = f"https://api.github.com/repos/{repo_full}/actions/workflows/{workflow_file}/dispatches"
    headers = {
        "Authorization": f"Bearer {gh_token}",
        "Accept": "application/vnd.github+json",
    }
    payload = {"ref": "main"}
    try:
        resp = requests.post(url, json=payload, headers=headers, timeout=30)
        if resp.status_code in (204, 200):
            log.info(f"✅ ورکفلو {workflow_file} فراخوانی شد")
        else:
            log.warning(f"فراخوانی ورکفلو ناموفق: {resp.status_code} – {resp.text}")
    except Exception as e:
        log.error(f"خطا در فراخوانی ورکفلو: {e}")


# ─── پایپ‌لاین اصلی ─────────────────────────────────────────────────────────────

def run_pipeline(args: argparse.Namespace) -> None:
    password = os.environ["RESULTS_PASSWORD"]
    bot_token = os.environ["TELEGRAM_BOT_TOKEN"]
    chat_id = os.environ["TELEGRAM_CHAT_ID"]
    gh_token = os.environ.get("GITHUB_TOKEN", "")
    repo_full = os.environ.get("GITHUB_REPOSITORY", "")

    # ─── ۰. تست یکپارچگی ───
    if not integrity_test(password, bot_token, chat_id):
        sys.exit(1)

    # ─── ۱. زمان‌بندی ───
    sched = generate_schedule()
    if not args.force and not should_run_now(sched):
        log.info("⏰ زمان بک‌آپ نرسیده – خروج")
        # باز هم چرخه را فراخوانی کن
        if gh_token and repo_full:
            trigger_loop_workflow(gh_token, repo_full, "loop_backup.yml")
        return

    log.info("🚀 شروع بک‌آپ...")

    # ─── ۲. جمع‌آوری CSVها با فیلتر all_combinations.json (کار ۱) ───
    csv_files_all = list(ANALYSIS_DIR.rglob("*.csv")) if ANALYSIS_DIR.exists() else []
    csv_files = filter_completed_strategies(csv_files_all)
    if len(csv_files) != len(csv_files_all):
        log.info(f"🧹 {len(csv_files_all) - len(csv_files)} فایل به دلیل استراتژی تکمیل‌شده فیلتر شد")
    mapping = auto_detect_mapping(csv_files)

    # ─── ۳. پردازش فایل‌ها ───
    ledger = load_ledger()
    to_pack: list[tuple[Path, bytes]] = []

    for csv_path in csv_files:
        rel = str(csv_path)
        if is_uploaded(ledger, rel):
            log.debug(f"قبلاً آپلود شده: {rel}")
            continue
        try:
            processed = process_analysis_file(csv_path, mapping, password)
            to_pack.append((csv_path, processed))
            log.info(f"پردازش شد: {rel}")
        except Exception as e:
            log.error(f"خطا در پردازش {rel}: {e}")

    # کار ۲: فقط زیرپوشه‌های aggregated که ≥۳۰ دقیقه ساکت بوده‌اند و .backup_done ندارند
    enc_files = collect_aggregated_files()
    for enc_path in enc_files:
        rel = str(enc_path)
        if is_uploaded(ledger, rel):
            continue
        try:
            processed = process_aggregated_file(enc_path, password)
            to_pack.append((enc_path, processed))
        except Exception as e:
            log.error(f"خطا در پردازش aggregated {rel}: {e}")

    # کار ۴: فایل‌های یکبار مصرف
    onetime_files = collect_onetime_files()
    for ot_path in onetime_files:
        rel = str(ot_path)
        if is_uploaded(ledger, rel):
            continue
        try:
            processed = process_onetime_file(ot_path, password)
            to_pack.append((ot_path, processed))
        except Exception as e:
            log.error(f"خطا در پردازش فایل یکبار مصرف {rel}: {e}")

    backup_succeeded = False

    if not to_pack:
        log.info("هیچ فایل جدیدی برای بک‌آپ وجود ندارد")
    else:
        # ─── کار ۸ (قبل از بسته‌بندی): structure.json داخل هر پوشه آپلود‌شونده ───
        touched_folders: set[Path] = set()
        for path, _data in to_pack:
            if ANALYSIS_DIR.name in path.parts:
                folder = _strategy_folder(path)
            elif AGGREGATED_DIR.name in path.parts:
                idx = path.parts.index(AGGREGATED_DIR.name)
                folder = Path(*path.parts[: idx + 2])
            else:
                folder = None
            if folder:
                touched_folders.add(folder)

        folder_structs: dict[Path, dict] = {}
        for folder in touched_folders:
            struct = write_structure_json(folder)
            folder_structs[folder] = struct
            to_pack.append((folder / "structure.json", (folder / "structure.json").read_bytes()))

        # ─── ۴. بسته‌بندی ───
        packages = build_packages(to_pack, [ANALYSIS_DIR, AGGREGATED_DIR])

        # ─── ۵. آپلود ───
        for pkg in packages:
            fid = telegram_send_document(bot_token, chat_id,
                                          pkg["data"], pkg["name"],
                                          caption=f"📦 {pkg['name']}\n{len(pkg['files'])} فایل")
            if fid:
                for rel_path in pkg["files"]:
                    record_upload(ledger, rel_path, fid, len(pkg["data"]))
                log.info(f"✅ ارسال شد: {pkg['name']}")
                backup_succeeded = True

                # کار ۸: به‌روزرسانی telegram_file_id در structure.json + master_structure.json
                for folder, struct in folder_structs.items():
                    if str(folder / "structure.json") in pkg["files"] or any(
                        f.startswith(str(folder) + os.sep) or f == str(folder) for f in pkg["files"]
                    ):
                        update_structure_telegram_id(folder, fid)
                        struct["telegram_file_id"] = fid
                        update_master_structure(struct, pkg["name"])

        # کار ۳: پرچم .backup_done در پوشه‌های استراتژی/aggregated آپلود‌شده در این اجرا
        for folder in touched_folders:
            _touch_backup_done(folder)

        # کار ۴: پرچم .backup_done برای فایل‌های یکبار مصرف آپلودشده
        if onetime_files and backup_succeeded:
            mark_onetime_targets_done()

        # ─── ۶. پاکسازی فایل‌های analysis_results ───
        for csv_path in csv_files:
            if is_uploaded(ledger, str(csv_path)):
                try:
                    csv_path.unlink()
                    log.info(f"حذف شد: {csv_path}")
                except Exception as e:
                    log.warning(f"حذف ناموفق {csv_path}: {e}")

    # ─── ۷. ارسال ۱ یا ۲ پیام فیلم/سریال رندوم (فقط در صورت بک‌آپ موفق) ───
    if backup_succeeded:
        send_movie_messages_after_backup(bot_token, chat_id)

    # ─── ۸. ارسال متادیتا (شامل master_structure.json) ───
    send_metadata(bot_token, chat_id, password)

    # ─── ۹. ارسال هفتگی ───
    weekly_repos = os.environ.get("WEEKLY_REPOS", "now-test-repo").split(",")
    weekly_full_backup(bot_token, chat_id, password, [r.strip() for r in weekly_repos])

    # ─── ۱۰. فراخوانی چرخه ───
    if gh_token and repo_full:
        trigger_loop_workflow(gh_token, repo_full, "loop_backup.yml")

    log.info("✅ پایپ‌لاین با موفقیت تمام شد")



# ─── نقطه ورود ─────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="سیستم بک‌آپ هوشمند")
    subparsers = parser.add_subparsers(dest="command")

    # دستور اصلی
    run_p = subparsers.add_parser("run", help="اجرای پایپ‌لاین")
    run_p.add_argument("--force", action="store_true", help="اجرای اجباری بدون بررسی زمان")

    # دستور تولید زمان‌بندی
    subparsers.add_parser("schedule", help="تولید زمان‌بندی روزانه")

    # دستور فراخوانی چرخه
    loop_p = subparsers.add_parser("trigger-loop", help="فراخوانی ورکفلو چرخه")
    loop_p.add_argument("--workflow", default="backup_pipeline.yml")

    # دستور اطلاع‌رسانی خطا
    subparsers.add_parser("notify-failure", help="ارسال پیام شکست به تلگرام")

    # دستور پردازش پیام‌های فورواردی + لینک‌های اختصاصی
    subparsers.add_parser("process-updates", help="پردازش پیام‌های فورواردی و دستورات /start")

    # دستور ارسال مستقل پیام فیلم/سریال
    subparsers.add_parser("send-movie", help="ارسال یک پیام فیلم/سریال تصادفی")

    args = parser.parse_args()

    if args.command == "schedule":
        sched = generate_schedule()
        print(json.dumps(sched, ensure_ascii=False, indent=2))

    elif args.command == "trigger-loop":
        gh_token = os.environ.get("GITHUB_TOKEN", "")
        repo_full = os.environ.get("GITHUB_REPOSITORY", "")
        trigger_loop_workflow(gh_token, repo_full, args.workflow)

    elif args.command == "notify-failure":
        bot_token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
        chat_id   = os.environ.get("TELEGRAM_CHAT_ID", "")
        if bot_token and chat_id:
            msg = f"⚠️ بک‌آپ ناموفق بود!\nزمان: {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}"
            requests.post(
                f"{TELEGRAM_API}/bot{bot_token}/sendMessage",
                json={"chat_id": chat_id, "text": msg},
                timeout=15,
            )
            log.info("پیام شکست ارسال شد")

    elif args.command == "process-updates":
        process_forward_updates()

    elif args.command == "send-movie":
        bot_token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
        chat_id   = os.environ.get("TELEGRAM_CHAT_ID", "")
        if bot_token and chat_id:
            send_movie_message(bot_token, chat_id)
        else:
            log.error("TELEGRAM_BOT_TOKEN یا TELEGRAM_CHAT_ID تنظیم نشده‌اند")

    elif args.command == "run":
        run_pipeline(args)

    else:
        # پیش‌فرض: اجرای پایپ‌لاین
        args.force = False
        run_pipeline(args)


if __name__ == "__main__":
    main()
