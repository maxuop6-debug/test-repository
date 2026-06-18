#!/usr/bin/env python3
"""
backup_system.py - سیستم بک‌آپ هوشمند یکپارچه
تمام منطق پردازش در یک فایل واحد
"""

import os
import sys
import json
import csv
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
IRAN_UTC_OFFSET = 3.5                  # ساعت (UTC+3:30)

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
    meta_files = [LEDGER_FILE, MAPPING_FILE, INTEGRITY_FLAG]
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

            # آرشیو کل مخزن
            buf = io.BytesIO()
            with tarfile.open(fileobj=buf, mode="w:xz") as tar:
                tar.add(tmpdir, arcname=repo)
            packed = buf.getvalue()
            encrypted = encrypt_data(packed, password)
            fname = f"weekly_{repo}_{today.isoformat()}.tar.xz.enc"
            fid = telegram_send_document(bot_token, chat_id, encrypted, fname,
                                          caption=f"📦 هفتگی: {repo}")
            if fid:
                log.info(f"✅ ارسال هفتگی: {repo}")


# ─── فراخوانی چرخه ──────────────────────────────────────────────────────────────

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

    # ─── ۲. جمع‌آوری فایل‌های CSV و تشخیص نگاشت ───
    csv_files = list(ANALYSIS_DIR.rglob("*.csv")) if ANALYSIS_DIR.exists() else []
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

    enc_files = list(AGGREGATED_DIR.rglob("*.enc")) if AGGREGATED_DIR.exists() else []
    for enc_path in enc_files:
        rel = str(enc_path)
        if is_uploaded(ledger, rel):
            continue
        try:
            processed = process_aggregated_file(enc_path, password)
            to_pack.append((enc_path, processed))
        except Exception as e:
            log.error(f"خطا در پردازش aggregated {rel}: {e}")

    if not to_pack:
        log.info("هیچ فایل جدیدی برای بک‌آپ وجود ندارد")
    else:
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

        # ─── ۶. پاکسازی فایل‌های analysis_results ───
        for csv_path in csv_files:
            if is_uploaded(ledger, str(csv_path)):
                try:
                    csv_path.unlink()
                    log.info(f"حذف شد: {csv_path}")
                except Exception as e:
                    log.warning(f"حذف ناموفق {csv_path}: {e}")

    # ─── ۷. ارسال متادیتا ───
    send_metadata(bot_token, chat_id, password)

    # ─── ۸. ارسال هفتگی ───
    weekly_repos = os.environ.get("WEEKLY_REPOS", "now-test-repo").split(",")
    weekly_full_backup(bot_token, chat_id, password, [r.strip() for r in weekly_repos])

    # ─── ۹. فراخوانی چرخه ───
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

    elif args.command == "run":
        run_pipeline(args)

    else:
        # پیش‌فرض: اجرای پایپ‌لاین
        args.force = False
        run_pipeline(args)


if __name__ == "__main__":
    main()
