#!/usr/bin/env python3
"""
backup_system.py - سیستم بک‌آپ خالص (پردازش، پکیج‌بندی، آپلود به تلگرام)
"""

import os
import sys
import json
import csv
import string
import lzma
import tarfile
import hashlib
import random
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

# محدودیت دانلود تلگرام (getFile) برای ربات‌ها ۲۰ مگابایت است.
# حجم هر پکیج را روی ۱۸ مگابایت نگه می‌داریم تا ۲ مگابایت حاشیه امنیت داشته باشیم.
PACK_SIZE = 18 * 1024 * 1024

TELEGRAM_API = "https://api.telegram.org"
ANALYSIS_DIR = Path("analysis_results")
AGGREGATED_DIR = Path("aggregated")
MAPPING_FILE = Path("auto_mapping.json")
LEDGER_FILE = Path("backup_ledger.json")
INTEGRITY_FLAG = Path("integrity_pass.flag")
ALL_COMBINATIONS_FILE = Path("all_combinations.json")
MASTER_STRUCTURE_FILE = Path("master_structure.json")
BACKUP_DONE_FLAG = ".backup_done"
AGGREGATED_MIN_AGE_MINUTES = 30
ONETIME_FLAGS_DIR = Path(".onetime_flags")

# تعداد دفعات تلاش مجدد برای آپلود هر پکیج (به جز تلاش‌های ناشی از خطای ۴۲۹ که جدا حساب می‌شوند)
UPLOAD_MAX_ATTEMPTS = 3
# مکث بین هر آپلود برای رعایت محدودیت ۲۰ پیام در دقیقه تلگرام
UPLOAD_SLEEP_MIN = 2.0
UPLOAD_SLEEP_MAX = 3.0

ONE_TIME_TARGETS = [
    (Path("data/news"),          "data_news"),
    (Path("encrypted_data"),     "encrypted_data"),
    (Path("Test-repo/data.enc"), "test_repo_data_enc"),
    (Path("Test-repo/zips"),     "test_repo_zips"),
    (Path("encrypted_data/src"), "encrypted_data_src"),
]

FIXED_COLUMN_MAP: dict[str, str] = {}
FIXED_VALUE_MAP: dict[str, str] = {
    "0.0": "L", "0.1": "M", "0.2": "Nv", "0.3": "O",
    "Good": "G", "Bad": "B", "Neutral": "N",
    "CPI m/m": "C", "Core CPI m/m": "K",
    "PPI m/m": "P", "Core PPI m/m": "R",
    "FOMC": "F", "CPI y/y": "Y",
}

# ─── رمزنگاری ──────────────────────────────────────────────────────────────────
def _derive_key(password: str, salt: bytes) -> bytes:
    return hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 100_000, dklen=32)

def encrypt_data(data: bytes, password: str) -> bytes:
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
        return b"ENC1" + salt + iv + ct
    except ImportError:
        log.warning("cryptography نصب نیست – بدون رمزنگاری ذخیره می‌شود")
        return b"NOENC" + data

def decrypt_data(data: bytes, password: str) -> bytes:
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

def compress_data(data: bytes) -> bytes:
    return lzma.compress(data, preset=9)

def decompress_data(data: bytes) -> bytes:
    return lzma.decompress(data)

# ─── نگاشت ──────────────────────────────────────────────────────────────────
def load_mapping() -> dict:
    if MAPPING_FILE.exists():
        m = json.loads(MAPPING_FILE.read_text(encoding="utf-8"))
        if not m.get("reverse_values"):
            m["reverse_values"] = {v: k for k, v in m.get("values", {}).items()}
        return m
    values = dict(FIXED_VALUE_MAP)
    return {
        "columns":        dict(FIXED_COLUMN_MAP),
        "values":         values,
        "reverse_values": {v: k for k, v in values.items()},
    }

def save_mapping(mapping: dict) -> None:
    MAPPING_FILE.write_text(json.dumps(mapping, ensure_ascii=False, indent=2), encoding="utf-8")

def _next_code(used: set[str]) -> str:
    for c in string.ascii_uppercase:
        if c not in used:
            return c
    for c1 in string.ascii_uppercase:
        for c2 in string.ascii_lowercase:
            code = c1 + c2
            if code not in used:
                return code
    for c1 in string.ascii_uppercase:
        for c2 in string.ascii_lowercase:
            for c3 in string.ascii_lowercase:
                code = c1 + c2 + c3
                if code not in used:
                    return code
    raise RuntimeError("فضای کد تمام شد")

def auto_detect_mapping(csv_paths: list[Path]) -> dict:
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
                        if cell and len(cell) >= 3 and not _is_numeric(cell) and not _is_date(cell):
                            freq[cell] = freq.get(cell, 0) + 1
        except Exception as e:
            log.warning(f"خطا در خواندن {p}: {e}")
    candidates = sorted(
        [(v, c) for v, c in freq.items() if c > 5 and v not in existing_values],
        key=lambda x: -x[1]
    )
    for value, cnt in candidates:
        code = _next_code(used_codes)
        mapping["values"][value] = code
        mapping["reverse_values"][code] = value
        used_codes.add(code)
        log.info(f"نگاشت جدید: '{value}' (تکرار={cnt}) → '{code}'")
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
    """
    قوانین:
    - ستون‌ها: اگر در mapping["columns"] بود به کد تبدیل می‌شود، در غیر این صورت نام اصلی حفظ می‌شود.
    - تاریخ‌ها (YYYY-MM-DD): به تعداد روز از EPOCH تبدیل می‌شوند.
    - مقادیر موجود در mapping["values"]: به کد تبدیل می‌شوند.
    - اعداد: بدون تغییر می‌مانند.
    - مقادیر جدید (نه عدد، نه تاریخ، نه در نگاشت): بدون تغییر حفظ می‌شوند تا داده‌ای از بین نرود.
    """
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
                    elif _is_numeric(cell):
                        new_row.append(cell)
                    else:
                        if cell:
                            log.info(f"مقدار جدید شناسایی شد: '{cell}' - به نگاشت اضافه نشد (حفظ مقدار اصلی)")
                        new_row.append(cell)
                out_rows.append(new_row)
    buf = io.StringIO()
    csv.writer(buf).writerows(out_rows)
    return buf.getvalue().encode("utf-8")

def reverse_mapping(csv_bytes: bytes, mapping: dict) -> bytes:
    """
    قوانین:
    - ستون‌ها: کد ستون اگر در mapping["columns"] پیدا شد به نام اصلی برمی‌گردد، در غیر این صورت همان کد حفظ می‌شود.
    - تاریخ‌ها: اعداد صحیح (روز از EPOCH) به فرمت YYYY-MM-DD برمی‌گردند.
    - مقادیر: کد اگر در reverse_values پیدا شد به مقدار اصلی برمی‌گردد، در غیر این صورت همان کد حفظ می‌شود.
    """
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
                    try:
                        d = EPOCH + timedelta(days=int(cell))
                        new_row.append(d.strftime("%Y-%m-%d"))
                        continue
                    except Exception:
                        pass
                if cell in rev_val:
                    new_row.append(rev_val[cell])
                else:
                    if cell and not _is_numeric(cell):
                        log.warning(f"⚠️ کد پیدا نشد: '{cell}' - حفظ همان کد")
                    new_row.append(cell)
            out_rows.append(new_row)
    out_buf = io.StringIO()
    csv.writer(out_buf).writerows(out_rows)
    return out_buf.getvalue().encode("utf-8")

# ─── تست یکپارچگی روزانه ──────────────────────────────────────────────────────
def integrity_test(password: str) -> bool:
    today_str = date.today().isoformat()
    log.info(f"بررسی پرچم تست روزانه برای تاریخ {today_str}")

    if INTEGRITY_FLAG.exists():
        try:
            flag_data = json.loads(INTEGRITY_FLAG.read_text(encoding="utf-8"))
        except Exception:
            flag_data = {}
        if flag_data.get("date") == today_str:
            log.info("پرچم تست امروز وجود دارد - اسکیپ تست")
            return True

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

    log.info(f"پرچم تست امروز وجود ندارد - اجرای تست یکپارچگی با فایل نمونه: {tmp_path.name}")

    try:
        mapping = load_mapping()

        mapped = apply_mapping(tmp_path, mapping)
        log.info("مرحله ۱: نگاشت (apply_mapping) روی فایل نمونه - انجام شد")

        compressed = compress_data(mapped)
        log.info("مرحله ۲: فشرده‌سازی (compress_data) - انجام شد")

        encrypted = encrypt_data(compressed, password)
        log.info("مرحله ۳: رمزنگاری (encrypt_data) - انجام شد")

        dec = decrypt_data(encrypted, password)
        log.info("مرحله ۴: برگردان رمزنگاری (decrypt_data) - انجام شد")

        decomp = decompress_data(dec)
        log.info("مرحله ۵: خارج‌سازی فشرده (decompress_data) - انجام شد")

        restored = reverse_mapping(decomp, mapping)
        log.info("مرحله ۶: برگردان نگاشت (reverse_mapping) - انجام شد")

        log.info("مقایسه فایل برگردان‌شده با فایل اصلی - در حال انجام...")
        original_content = tmp_path.read_bytes()
        orig_rows = list(csv.reader(io.StringIO(original_content.decode("utf-8"))))
        rest_rows = list(csv.reader(io.StringIO(restored.decode("utf-8"))))

        if orig_rows == rest_rows:
            sha = hashlib.sha256(original_content).hexdigest()
            INTEGRITY_FLAG.write_text(
                json.dumps({"date": today_str, "sha256": sha}, ensure_ascii=False),
                encoding="utf-8",
            )
            log.info(f"✅ تست یکپارچگی موفق - داده‌ها کاملاً مطابقت دارند - هش: {sha} - پرچم روزانه ایجاد شد")
            return True
        else:
            offset = next(
                (i for i in range(min(len(orig_rows), len(rest_rows))) if orig_rows[i] != rest_rows[i]),
                min(len(orig_rows), len(rest_rows)),
            )
            log.error(
                f"❌ خطای بحرانی: تست یکپارچگی شکست خورد - داده‌ها با هم مطابقت ندارند! "
                f"- تفاوت در آفست {offset} - کل فرآیند متوقف می‌شود"
            )
            return False
    finally:
        tmp_path.unlink(missing_ok=True)

# ─── تلگرام ──────────────────────────────────────────────────────────────────
def telegram_send_document(bot_token: str, chat_id: str, data: bytes,
                            filename: str, pkg_number: int, pkg_total: int) -> Optional[str]:
    url = f"{TELEGRAM_API}/bot{bot_token}/sendDocument"
    label = f"پکیج {pkg_number}/{pkg_total}: {filename}"
    attempt = 0
    while attempt < UPLOAD_MAX_ATTEMPTS:
        attempt += 1
        log.info(f"آپلود {label} - حجم: {len(data)} بایت - تلاش {attempt}...")
        start = time.time()
        try:
            resp = requests.post(
                url,
                data={"chat_id": chat_id},
                files={"document": (filename, data, "application/octet-stream")},
                timeout=180,
            )
            if resp.status_code == 429:
                try:
                    retry_after = resp.json().get("parameters", {}).get("retry_after", 5)
                except Exception:
                    retry_after = 5
                log.warning(
                    f"⚠️ خطای ۴۲۹ دریافت شد - صبر به مدت {retry_after} ثانیه (retry_after) - تلاش مجدد..."
                )
                time.sleep(retry_after)
                attempt -= 1  # خطای ۴۲۹ به عنوان شکست واقعی حساب نمی‌شود
                continue

            result = resp.json()
            if result.get("ok"):
                elapsed = time.time() - start
                file_id = result["result"]["document"]["file_id"]
                log.info(f"✅ آپلود موفق - file_id: {file_id} - زمان: {elapsed:.1f} ثانیه")
                return file_id
            log.error(f"❌ خطا در آپلود: {result.get('description')} - تلاش مجدد...")
        except Exception as e:
            log.error(f"❌ خطا در آپلود: {e} - تلاش مجدد...")
        time.sleep(3)

    log.error(f"❌ آپلود {label} پس از {UPLOAD_MAX_ATTEMPTS} تلاش ناموفق بود - کل فرآیند متوقف می‌شود")
    return None

# ─── دفترچه آپلود ────────────────────────────────────────────────────────────
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

# ─── فیلتر استراتژی‌های تکمیل‌شده ─────────────────────────────────────────────
def load_completed_strategies() -> set[str]:
    if not ALL_COMBINATIONS_FILE.exists():
        return set()
    try:
        data = json.loads(ALL_COMBINATIONS_FILE.read_text(encoding="utf-8"))
    except Exception as e:
        log.warning(f"خطا در خواندن {ALL_COMBINATIONS_FILE}: {e}")
        return set()
    if isinstance(data, dict):
        if "completed_strategies" in data:
            return set(str(s) for s in data["completed_strategies"])
        return set(str(k) for k in data.keys())
    if isinstance(data, list):
        return set(str(s) for s in data)
    return set()

def _strategy_folder(csv_path: Path) -> Optional[Path]:
    try:
        idx = csv_path.parts.index(ANALYSIS_DIR.name)
    except ValueError:
        return None
    if len(csv_path.parts) < idx + 3:
        return None
    return Path(*csv_path.parts[: idx + 3])

def _strategy_name(csv_path: Path) -> Optional[str]:
    folder = _strategy_folder(csv_path)
    return folder.name if folder else None

def filter_completed_strategies(csv_files: list[Path]) -> list[Path]:
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

# ─── فایل‌های Aggregated ─────────────────────────────────────────────────────
def collect_aggregated_files() -> list[Path]:
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

# ─── پرچم‌گذاری پوشه‌ها ───────────────────────────────────────────────────────
def _touch_backup_done(folder: Path) -> None:
    try:
        folder.mkdir(parents=True, exist_ok=True)
        (folder / BACKUP_DONE_FLAG).write_text(datetime.utcnow().isoformat(), encoding="utf-8")
    except Exception as e:
        log.warning(f"عدم موفقیت در ایجاد پرچم {folder}: {e}")

# ─── فایل‌های یکبار مصرف ─────────────────────────────────────────────────────
def _onetime_flag_path(flag_name: str) -> Path:
    return ONETIME_FLAGS_DIR / f"{flag_name}{BACKUP_DONE_FLAG}"

def collect_onetime_files() -> list[tuple[Path, str]]:
    result: list[tuple[Path, str]] = []
    for target, flag_name in ONE_TIME_TARGETS:
        if _onetime_flag_path(flag_name).exists():
            continue
        if not target.exists():
            continue
        if target.is_file():
            result.append((target, flag_name))
        else:
            for p in sorted(target.rglob("*")):
                if p.is_file() and p.name != BACKUP_DONE_FLAG:
                    result.append((p, flag_name))
    return result

def mark_onetime_done(flag_names: set[str]) -> None:
    ONETIME_FLAGS_DIR.mkdir(exist_ok=True)
    for flag_name in flag_names:
        flag = _onetime_flag_path(flag_name)
        try:
            flag.write_text(datetime.utcnow().isoformat(), encoding="utf-8")
            log.info(f"✅ پرچم یکبار مصرف گذاشته شد: {flag}")
        except Exception as e:
            log.warning(f"خطا در گذاشتن پرچم {flag}: {e}")

def process_onetime_file(path: Path, password: str) -> bytes:
    raw = path.read_bytes()
    compressed = compress_data(raw)
    return encrypt_data(compressed, password)

# ─── structure.json و master_structure.json ─────────────────────────────────
def _sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()

def write_structure_json(folder: Path) -> dict:
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

# ─── پردازش فایل‌ها ────────────────────────────────────────────────────────
def process_analysis_file(path: Path, mapping: dict, password: str) -> bytes:
    size = path.stat().st_size
    log.info(f"پردازش فایل: {path} - حجم: {size} بایت")
    mapped = apply_mapping(path, mapping)
    log.info("  - نگاشت (apply_mapping) - انجام شد")
    compressed = compress_data(mapped)
    log.info("  - فشرده‌سازی (compress_data) - انجام شد")
    encrypted = encrypt_data(compressed, password)
    log.info(f"  - رمزنگاری (encrypt_data) - انجام شد - حجم نهایی: {len(encrypted)} بایت")
    return encrypted

def process_aggregated_file(path: Path, password: str) -> bytes:
    size = path.stat().st_size
    log.info(f"پردازش فایل aggregated: {path} - حجم: {size} بایت")
    raw = path.read_bytes()
    compressed = compress_data(raw)
    encrypted = encrypt_data(compressed, password)
    log.info(f"  - رمزنگاری (encrypt_data) - انجام شد - حجم نهایی: {len(encrypted)} بایت")
    return encrypted

# ─── بسته‌بندی ─────────────────────────────────────────────────────────────
def _create_tar(files: list[tuple[Path, bytes]]) -> tuple[bytes, list[str]]:
    file_list = []
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:xz") as tar:
        for path, data in files:
            rel = str(path)
            file_list.append(rel)
            info = tarfile.TarInfo(name=rel)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
        idx = json.dumps({"files": file_list, "created": datetime.utcnow().isoformat()},
                         ensure_ascii=False, indent=2).encode("utf-8")
        idx_info = tarfile.TarInfo(name="index.json")
        idx_info.size = len(idx)
        tar.addfile(idx_info, io.BytesIO(idx))
    return buf.getvalue(), file_list

def build_packages(files: list[tuple[Path, bytes]]) -> list[dict]:
    packages: list[dict] = []
    current_files: list[tuple[Path, bytes]] = []
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
        log.info(
            f"  - پکیج {pack_idx}: {name} - تعداد فایل‌های داخل: {len(file_list)} - حجم: {len(pkg_data)} بایت"
        )
        pack_idx += 1
        current_files = []
        current_size = 0

    for path, data in files:
        if len(data) >= PACK_SIZE:
            flush()
            name = f"backup_{timestamp}_{pack_idx:03d}_large.tar.xz.enc"
            pkg_data, file_list = _create_tar([(path, data)])
            packages.append({"name": name, "data": pkg_data, "files": file_list})
            log.info(
                f"  - پکیج {pack_idx}: {name} - تعداد فایل‌های داخل: {len(file_list)} - حجم: {len(pkg_data)} بایت"
            )
            pack_idx += 1
            continue
        if current_size + len(data) > PACK_SIZE:
            flush()
        current_files.append((path, data))
        current_size += len(data)
    flush()
    return packages

# ─── بک‌آپ هفتگی مخازن (اختیاری – در صورت تنظیم WEEKLY_REPOS) ────────────────
def weekly_full_backup(bot_token: str, chat_id: str, password: str,
                        repo_names: list[str]) -> None:
    today = date.today()
    if today.weekday() not in (3, 4):
        log.info("روز ارسال هفتگی نیست")
        return
    gh_token = os.environ.get("GITHUB_TOKEN", "")
    gh_user = os.environ.get("GITHUB_ACTOR", "")
    for repo in repo_names:
        log.info(f"📦 شروع ارسال هفتگی مخزن {repo}")
        with tempfile.TemporaryDirectory() as tmpdir:
            clone_url = f"https://{gh_user}:{gh_token}@github.com/{gh_user}/{repo}.git"
            result = subprocess.run(
                ["git", "clone", "--depth", "1", clone_url, tmpdir],
                capture_output=True, text=True
            )
            if result.returncode != 0:
                log.error(f"کلون ناموفق: {repo} – {result.stderr}")
                continue
            buf = io.BytesIO()
            skip_names = {".git"}
            with tarfile.open(fileobj=buf, mode="w:xz") as tar:
                def _filter(tarinfo: tarfile.TarInfo) -> Optional[tarfile.TarInfo]:
                    parts = Path(tarinfo.name).parts
                    if any(p in skip_names for p in parts):
                        return None
                    for target, _flag in ONE_TIME_TARGETS:
                        target_parts = target.parts
                        if parts[1: 1 + len(target_parts)] == target_parts:
                            flag_dir = target if target.is_dir() else target.parent
                            if (flag_dir / BACKUP_DONE_FLAG).exists():
                                return None
                    return tarinfo
                tar.add(tmpdir, arcname=repo, filter=_filter)
            packed = buf.getvalue()
            encrypted = encrypt_data(packed, password)
            fname = f"weekly_{repo}_{today.isoformat()}.tar.xz.enc"
            fid = telegram_send_document(bot_token, chat_id, encrypted, fname, 1, 1)
            if fid:
                log.info(f"✅ ارسال هفتگی {repo} تمام شد")
            else:
                log.error(f"❌ ارسال هفتگی {repo} ناموفق بود")
            time.sleep(random.uniform(UPLOAD_SLEEP_MIN, UPLOAD_SLEEP_MAX))

# ─── فراخوانی ورکفلو (GitHub Actions) ────────────────────────────────────────
def trigger_loop_workflow(gh_token: str, repo_full: str, workflow_file: str) -> None:
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

# ─── پایپ‌لاین اصلی ─────────────────────────────────────────────────────────
def run_pipeline(args: argparse.Namespace) -> None:
    start_time = time.time()

    try:
        password = os.environ["RESULTS_PASSWORD"]
        bot_token = os.environ["TELEGRAM_BOT_TOKEN"]
        chat_id = os.environ["TELEGRAM_CHAT_ID"]
    except KeyError as e:
        log.error(f"❌ متغیر محیطی تنظیم نشده: {e}")
        sys.exit(1)

    now_str = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")
    log.info(f"شروع فرآیند بک‌آپ - {now_str}")

    try:
        ledger = load_ledger()
        log.info(f"بارگذاری دفترچه آپلود (ledger) - تعداد فایل‌های قبلی: {len(ledger['uploads'])}")

        if not integrity_test(password):
            log.error("❌ تست یکپارچگی ناموفق – خروج")
            sys.exit(1)

        total_uploaded_files = 0
        total_uploaded_bytes = 0

        # ─── فایل‌های یکبار مصرف ───────────────────────────────────────────
        onetime_files = collect_onetime_files()
        onetime_flag_names_to_mark: set[str] = set()
        onetime_to_pack: list[tuple[Path, bytes]] = []

        for ot_path, flag_name in onetime_files:
            rel = str(ot_path)
            if is_uploaded(ledger, rel):
                continue
            try:
                processed = process_onetime_file(ot_path, password)
                onetime_to_pack.append((ot_path, processed))
                onetime_flag_names_to_mark.add(flag_name)
                log.info(f"یکبار مصرف آماده آپلود: {rel}")
            except Exception as e:
                log.error(f"خطا در پردازش فایل یکبار مصرف {rel}: {e}")

        if onetime_to_pack:
            log.info(f"🚀 آپلود {len(onetime_to_pack)} فایل یکبار مصرف...")
            packages = build_packages(onetime_to_pack)
            onetime_succeeded = False
            for i, pkg in enumerate(packages, start=1):
                fid = telegram_send_document(bot_token, chat_id, pkg["data"], pkg["name"], i, len(packages))
                if not fid:
                    log.error("❌ آپلود فایل یکبار مصرف ناموفق بود - کل فرآیند متوقف می‌شود")
                    sys.exit(1)
                for rel_path in pkg["files"]:
                    record_upload(ledger, rel_path, fid, len(pkg["data"]))
                total_uploaded_files += len(pkg["files"])
                total_uploaded_bytes += len(pkg["data"])
                log.info(f"✅ یکبار مصرف ارسال شد: {pkg['name']}")
                onetime_succeeded = True
                time.sleep(random.uniform(UPLOAD_SLEEP_MIN, UPLOAD_SLEEP_MAX))
            if onetime_succeeded:
                mark_onetime_done(onetime_flag_names_to_mark)

        # ─── پردازش فایل‌های CSV ───────────────────────────────────────────
        csv_files_all = list(ANALYSIS_DIR.rglob("*.csv")) if ANALYSIS_DIR.exists() else []
        log.info(f"جستجوی فایل‌های CSV در {ANALYSIS_DIR}/ - تعداد کل فایل‌ها: {len(csv_files_all)}")

        csv_files = filter_completed_strategies(csv_files_all)
        log.info(f"اعمال فیلتر استراتژی‌های تکمیل‌شده - تعداد فایل‌های باقی‌مانده: {len(csv_files)}")

        if not csv_files:
            log.warning("auto_detect_mapping: لیست فایل‌های CSV خالی است")
            mapping = load_mapping()
        else:
            log.info(f"شروع auto_detect_mapping روی {len(csv_files)} فایل - شناسایی مقادیر جدید...")
            values_before = len(load_mapping()["values"])
            mapping = auto_detect_mapping(csv_files)
            log.info(f"تعداد مقادیر جدید شناسایی‌شده: {len(mapping['values']) - values_before}")

        to_pack: list[tuple[Path, bytes]] = []

        for csv_path in csv_files:
            rel = str(csv_path)
            if is_uploaded(ledger, rel):
                continue
            try:
                processed = process_analysis_file(csv_path, mapping, password)
                to_pack.append((csv_path, processed))
                log.info("  - فایل به لیست آپلود اضافه شد")
            except Exception as e:
                log.error(f"خطا در پردازش {rel}: {e}")

        # ─── پردازش فایل‌های Aggregated ────────────────────────────────────
        enc_files_all = list(AGGREGATED_DIR.rglob("*.enc")) if AGGREGATED_DIR.exists() else []
        log.info(f"جستجوی فایل‌های .enc در {AGGREGATED_DIR}/ - تعداد کل: {len(enc_files_all)}")

        enc_files = collect_aggregated_files()
        log.info(
            f"فیلتر فایل‌های با سن کمتر از {AGGREGATED_MIN_AGE_MINUTES} دقیقه - "
            f"تعداد باقی‌مانده: {len(enc_files)}"
        )

        for enc_path in enc_files:
            rel = str(enc_path)
            if is_uploaded(ledger, rel):
                continue
            try:
                processed = process_aggregated_file(enc_path, password)
                to_pack.append((enc_path, processed))
                log.info("  - فایل به لیست آپلود اضافه شد")
            except Exception as e:
                log.error(f"خطا در پردازش aggregated {rel}: {e}")

        backup_succeeded = False

        if not to_pack:
            log.info("هیچ فایل جدیدی برای بک‌آپ وجود ندارد")
        else:
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

            log.info(f"ساخت structure.json برای پوشه‌ها - تعداد پوشه‌ها: {len(touched_folders)}")
            folder_structs: dict[Path, dict] = {}
            for folder in touched_folders:
                struct = write_structure_json(folder)
                folder_structs[folder] = struct
                log.info(
                    f"  - پوشه: {folder.name} - تعداد فایل‌ها: {struct['total_files']} "
                    f"- حجم کل: {struct['total_size_bytes']} بایت"
                )
                log.info("  - structure.json ساخته شد")
                to_pack.append((folder / "structure.json", (folder / "structure.json").read_bytes()))
                if MASTER_STRUCTURE_FILE.exists():
                    try:
                        shutil.copy(MASTER_STRUCTURE_FILE, folder / MASTER_STRUCTURE_FILE.name)
                        to_pack.append(
                            (folder / MASTER_STRUCTURE_FILE.name, (folder / MASTER_STRUCTURE_FILE.name).read_bytes())
                        )
                        log.info(f"✅ master_structure.json در {folder} کپی شد")
                    except Exception as e:
                        log.warning(f"خطا در کپی master_structure.json به {folder}: {e}")

            total_size = sum(len(d) for _, d in to_pack)
            log.info(f"شروع ساخت پکیج‌ها - تعداد کل فایل‌ها: {len(to_pack)} - حجم کل: {total_size} بایت")
            log.info("حجم هر پکیج: ۱۸ مگابایت")
            packages = build_packages(to_pack)

            log.info(f"شروع آپلود پکیج‌ها به تلگرام - تعداد کل پکیج‌ها: {len(packages)}")
            for i, pkg in enumerate(packages, start=1):
                fid = telegram_send_document(bot_token, chat_id, pkg["data"], pkg["name"], i, len(packages))
                if not fid:
                    log.error("❌ کل فرآیند متوقف می‌شود")
                    sys.exit(1)

                for rel_path in pkg["files"]:
                    record_upload(ledger, rel_path, fid, len(pkg["data"]))
                total_uploaded_files += len(pkg["files"])
                total_uploaded_bytes += len(pkg["data"])
                log.info(f"✅ ارسال شد: {pkg['name']}")
                backup_succeeded = True

                for folder, struct in folder_structs.items():
                    if str(folder / "structure.json") in pkg["files"] or any(
                        f.startswith(str(folder) + os.sep) or f == str(folder) for f in pkg["files"]
                    ):
                        update_structure_telegram_id(folder, fid)
                        struct["telegram_file_id"] = fid
                        update_master_structure(struct, pkg["name"])
                        log.info("  - master_structure.json به‌روزرسانی شد")

                time.sleep(random.uniform(UPLOAD_SLEEP_MIN, UPLOAD_SLEEP_MAX))

            log.info(f"ثبت آپلودها در دفترچه (backup_ledger.json) - تعداد فایل‌های ثبت‌شده: {total_uploaded_files}")

            log.info(f"پرچم‌گذاری پوشه‌ها با .backup_done - تعداد پوشه‌ها: {len(touched_folders)}")
            for folder in touched_folders:
                _touch_backup_done(folder)

            deleted = 0
            for csv_path in csv_files:
                if is_uploaded(ledger, str(csv_path)):
                    try:
                        csv_path.unlink()
                        deleted += 1
                        log.info(f"حذف شد: {csv_path}")
                    except Exception as e:
                        log.warning(f"حذف ناموفق {csv_path}: {e}")
            log.info(f"حذف فایل‌های CSV آپلودشده - تعداد فایل‌های حذف‌شده: {deleted}")

        # ─── بک‌آپ هفتگی مخازن (در صورت تنظیم WEEKLY_REPOS) ─────────────────
        weekly_repos = [r.strip() for r in os.environ.get("WEEKLY_REPOS", "").split(",") if r.strip()]
        if weekly_repos:
            weekly_full_backup(bot_token, chat_id, password, weekly_repos)

        elapsed = time.time() - start_time
        log.info(
            f"✅ فرآیند بک‌آپ با موفقیت به پایان رسید - زمان کل: {elapsed:.1f} ثانیه "
            f"- تعداد فایل‌های آپلودشده: {total_uploaded_files} - حجم کل آپلودشده: {total_uploaded_bytes} بایت"
        )

    except SystemExit:
        raise
    except Exception as e:
        log.error(f"❌ فرآیند بک‌آپ با خطا متوقف شد - دلیل: {e}")
        raise

# ─── نقطه ورود ────────────────────────────────────────────────────────────────
def main() -> None:
    parser = argparse.ArgumentParser(description="سیستم بک‌آپ خالص")
    subparsers = parser.add_subparsers(dest="command")

    run_p = subparsers.add_parser("run", help="اجرای پایپ‌لاین بک‌آپ")
    run_p.add_argument("--force", action="store_true",
                        help="اجرای اجباری (بدون زمان‌بندی، بک‌آپ همیشه بدون بررسی زمان اجرا می‌شود)")

    loop_p = subparsers.add_parser("trigger-loop", help="فراخوانی ورکفلو چرخه")
    loop_p.add_argument("--workflow", default="backup_pipeline.yml")

    subparsers.add_parser("notify-failure", help="ارسال پیام شکست به تلگرام")

    args = parser.parse_args()

    if args.command == "trigger-loop":
        gh_token = os.environ.get("GITHUB_TOKEN", "")
        repo_full = os.environ.get("GITHUB_REPOSITORY", "")
        trigger_loop_workflow(gh_token, repo_full, args.workflow)

    elif args.command == "notify-failure":
        bot_token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
        chat_id   = os.environ.get("TELEGRAM_CHAT_ID", "")
        if bot_token and chat_id:
            msg = f"⚠️ بک‌آپ ناموفق بود!\nزمان: {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}"
            try:
                requests.post(
                    f"{TELEGRAM_API}/bot{bot_token}/sendMessage",
                    json={"chat_id": chat_id, "text": msg},
                    timeout=15,
                )
                log.info("پیام شکست ارسال شد")
            except Exception as e:
                log.error(f"خطا در ارسال پیام شکست: {e}")
        else:
            log.error("TELEGRAM_BOT_TOKEN یا TELEGRAM_CHAT_ID تنظیم نشده‌اند")

    elif args.command == "run":
        run_pipeline(args)

    else:
        args.force = False
        run_pipeline(args)

if __name__ == "__main__":
    main()
