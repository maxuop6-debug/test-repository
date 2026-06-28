#!/usr/bin/env python3
# build_all_queues.py - ترکیب چهار اسکریپت صف‌ساز با لاگ‌گیری دقیق
# مراحل: work → golden → correlation → portfolios

import os
import sys
import json
import subprocess
import base64
from datetime import datetime
from pathlib import Path

# ================================ لاگ‌گیری ================================

LOG_FILE = None  # در main مقداردهی می‌شود

def log(message, level='INFO'):
    """چاپ پیام با timestamp و سطح لاگ — هم stdout هم فایل"""
    ts = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    line = f"[{ts}] [{level}] {message}"
    print(line, flush=True)
    if LOG_FILE:
        try:
            with open(LOG_FILE, 'a', encoding='utf-8') as f:
                f.write(line + '\n')
        except Exception:
            pass

# ================================ توابع مشترک ================================

def gh_api_get(repo, path):
    """دریافت محتوای یک فایل از مخزن با gh api"""
    log(f"  [API GET] {repo}/{path}")
    cmd = f"gh api repos/{repo}/contents/{path} --jq '.content' 2>/dev/null"
    result = subprocess.run(cmd, shell=True, capture_output=True, text=True)
    if result.returncode != 0 or not result.stdout.strip():
        log(f"  [API GET] نتیجه: یافت نشد (returncode={result.returncode})", 'WARNING')
        return None
    try:
        content = base64.b64decode(result.stdout.strip()).decode('utf-8')
        log(f"  [API GET] نتیجه: دریافت شد ({len(content)} کاراکتر)")
        return content
    except Exception as e:
        log(f"  [API GET] خطا در decode: {e}", 'ERROR')
        return None

def gh_api_get_binary(repo, path, dest_path):
    """دانلود فایل binary از مخزن (برای فایل‌های بزرگ از download_url استفاده می‌کند)"""
    log(f"  [API GET BINARY] {repo}/{path} → {dest_path}")
    token = os.environ.get('GH_TOKEN')

    cmd = f"gh api repos/{repo}/contents/{path} 2>/dev/null"
    result = subprocess.run(cmd, shell=True, capture_output=True, text=True)
    if result.returncode != 0 or not result.stdout.strip():
        log(f"  [API GET BINARY] خطا در دریافت metadata (returncode={result.returncode})", 'ERROR')
        return False

    try:
        meta = json.loads(result.stdout)
    except Exception as e:
        log(f"  [API GET BINARY] خطا در parse metadata: {e}", 'ERROR')
        return False

    file_size = meta.get('size', 0)
    download_url = meta.get('download_url', '')
    log(f"  [API GET BINARY] اندازه فایل: {file_size} bytes")

    if file_size > 1_000_000 or not meta.get('content'):
        if not download_url:
            log("  [API GET BINARY] download_url یافت نشد", 'ERROR')
            return False
        log("  [API GET BINARY] دانلود از download_url (فایل بزرگ)...")
        cmd_dl = [
            "curl", "-s", "-L",
            "-H", f"Authorization: token {token}",
            download_url, "-o", dest_path
        ]
        r = subprocess.run(cmd_dl, capture_output=True)
        if r.returncode != 0:
            log(f"  [API GET BINARY] خطا در curl download: {r.stderr}", 'ERROR')
            return False
    else:
        content_b64 = meta.get('content', '').replace('\n', '')
        if not content_b64:
            log("  [API GET BINARY] content خالی در پاسخ API", 'ERROR')
            return False
        decoded = base64.b64decode(content_b64)
        with open(dest_path, 'wb') as f:
            f.write(decoded)

    exists = os.path.exists(dest_path) and os.path.getsize(dest_path) > 0
    if exists:
        log(f"  [API GET BINARY] دانلود موفق ({os.path.getsize(dest_path)} bytes)")
    else:
        log("  [API GET BINARY] فایل دانلود‌شده خالی یا ناموجود است", 'ERROR')
    return exists

def upload_file_with_curl(repo, remote_path, local_file_path, sha=None, commit_msg="update queue"):
    """آپلود فایل با curl (از Argument list too long جلوگیری می‌کند)"""
    log(f"  [UPLOAD] {local_file_path} → {repo}/{remote_path}")
    with open(local_file_path, 'rb') as f:
        content_bytes = f.read()
    encoded = base64.b64encode(content_bytes).decode()

    temp_payload = "/tmp/payload_queue.json"
    with open(temp_payload, 'w') as f:
        f.write(f'{{"message":"{commit_msg}","content":"')
    with open(temp_payload, 'ab') as f:
        f.write(encoded.encode())
    with open(temp_payload, 'a') as f:
        f.write('","branch":"main"')
        if sha:
            f.write(f',"sha":"{sha}"')
        f.write('}')

    token = os.environ.get('GH_TOKEN')
    if not token:
        log("  [UPLOAD] GH_TOKEN تنظیم نشده است", 'ERROR')
        return False

    cmd = [
        "curl", "-s", "-X", "PUT",
        "-H", f"Authorization: token {token}",
        "-H", "Content-Type: application/json",
        "-d", f"@{temp_payload}",
        f"https://api.github.com/repos/{repo}/contents/{remote_path}"
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    try:
        os.unlink(temp_payload)
    except Exception:
        pass

    if result.returncode != 0:
        log(f"  [UPLOAD] خطا در curl: {result.stderr}", 'ERROR')
        return False

    try:
        resp = json.loads(result.stdout)
        if 'content' in resp:
            log(f"  [UPLOAD] موفق ✅")
            return True
        else:
            log(f"  [UPLOAD] پاسخ ناموفق: {resp.get('message', 'خطای ناشناخته')}", 'ERROR')
            return False
    except Exception:
        log(f"  [UPLOAD] خطا در parse پاسخ: {result.stdout[:200]}", 'ERROR')
        return False

def get_file_sha(repo, path):
    """دریافت SHA یک فایل از مخزن"""
    cmd = f"gh api repos/{repo}/contents/{path} --jq '.sha' 2>/dev/null"
    result = subprocess.run(cmd, shell=True, capture_output=True, text=True)
    if result.returncode == 0 and result.stdout.strip():
        sha = result.stdout.strip()
        log(f"  [SHA] {path}: {sha[:12]}...")
        return sha
    log(f"  [SHA] {path}: یافت نشد (فایل جدید)")
    return None

def decrypt_file(enc_path, dec_path, password):
    """رمزگشایی AES-256-CBC با Node.js"""
    log(f"  [DECRYPT] {enc_path} → {dec_path}")
    pass_file = "/tmp/dec_password.txt"
    with open(pass_file, 'w') as f:
        f.write(password)

    node_code = f"""
const crypto=require('crypto'),fs=require('fs');
const pass=fs.readFileSync('{pass_file}','utf8').trim();
const data=fs.readFileSync('{enc_path}');
const key=crypto.scryptSync(pass,'salt',32);
const iv=data.slice(0,16),enc=data.slice(16);
const decipher=crypto.createDecipheriv('aes-256-cbc',key,iv);
const dec=Buffer.concat([decipher.update(enc),decipher.final()]);
fs.writeFileSync('{dec_path}',dec);
"""
    result = subprocess.run(["node", "-e", node_code], capture_output=True, text=True)
    try:
        os.unlink(pass_file)
    except Exception:
        pass
    if result.returncode != 0:
        log(f"  [DECRYPT] خطای Node.js: {result.stderr[:300]}", 'ERROR')
        return False
    log("  [DECRYPT] رمزگشایی موفق ✅")
    return True

def check_file_exists(repo, path):
    """بررسی وجود فایل در مخزن"""
    cmd = f"gh api repos/{repo}/contents/{path} --jq '.sha' 2>/dev/null"
    result = subprocess.run(cmd, shell=True, capture_output=True, text=True)
    exists = result.returncode == 0 and bool(result.stdout.strip())
    log(f"  [CHECK] {path}: {'موجود ✅' if exists else 'ناموجود ❌'}")
    return exists

# ================================ تابع مشترک دریافت signatures از آرشیوها ================================

def get_all_signatures_from_archives(repo, key_name="signature_path"):
    """
    لیست تمام signatureهای موجود در signature_archives/ را برمی‌گرداند.
    key_name: نام کلید دیکشنری خروجی ('signature_path' برای golden/portfolios، 'path' برای correlation)

    از دو ساختار پشتیبانی می‌کند:
    - ساختار استاندارد: signature_archives/{module}/{strategy}/{coin}/{base_name}.tar.gz.enc
    - ساختار مسطح: signature_archives/{filename}.tar.gz.enc (فایل‌ها مستقیماً در ریشه)
    """
    log("  [ARCHIVES] دریافت لیست محتوای signature_archives/...")
    cmd = f"gh api repos/{repo}/contents/signature_archives --jq '.[].name' 2>/dev/null"
    result = subprocess.run(cmd, shell=True, capture_output=True, text=True)
    if result.returncode != 0 or not result.stdout.strip():
        log("  [ARCHIVES] هیچ محتوایی در signature_archives/ یافت نشد", 'WARNING')
        return []

    top_level_names = [m.strip() for m in result.stdout.strip().split('\n') if m.strip()]
    log(f"  [ARCHIVES] محتوای سطح اول: {top_level_names}")

    # بررسی ساختار: اگر combo_10day یا combo_monthly وجود داشت → ساختار استاندارد
    standard_modules = {"combo_10day", "combo_monthly"}
    found_modules = [n for n in top_level_names if n in standard_modules]

    all_signatures = []

    if found_modules:
        # ساختار واقعی: module/strategy/file.tar.gz.enc  (۳ سطح، نه ۴)
        log(f"  [ARCHIVES] ساختار استاندارد شناسایی شد — ماژول‌ها: {found_modules}")
        for module in found_modules:
            cmd = f"gh api repos/{repo}/contents/signature_archives/{module} --jq '.[].name' 2>/dev/null"
            result = subprocess.run(cmd, shell=True, capture_output=True, text=True)
            if result.returncode != 0 or not result.stdout.strip():
                log(f"  [ARCHIVES] ماژول {module}: استراتژی‌ای یافت نشد", 'WARNING')
                continue
            strategies = [s.strip() for s in result.stdout.strip().split('\n') if s.strip()]
            log(f"  [ARCHIVES] {module}: {len(strategies)} استراتژی")

            for strategy in strategies:
                path = f"signature_archives/{module}/{strategy}"
                cmd = f"gh api repos/{repo}/contents/{path} --jq '.[].name' 2>/dev/null"
                result = subprocess.run(cmd, shell=True, capture_output=True, text=True)
                if result.returncode != 0 or not result.stdout.strip():
                    log(f"  [ARCHIVES] {module}/{strategy}: فایلی یافت نشد", 'WARNING')
                    continue
                files = [fn.strip() for fn in result.stdout.strip().split('\n') if fn.strip()]
                log(f"  [ARCHIVES] {module}/{strategy}: {len([f for f in files if f.endswith('.tar.gz.enc')])} فایل .tar.gz.enc")

                for fname in files:
                    if fname.endswith('.tar.gz.enc'):
                        base_name = fname[:-len('.tar.gz.enc')]
                        sig_path = f"{module}/{strategy}/{base_name}"
                        all_signatures.append({key_name: sig_path})
    else:
        # ساختار مسطح: فایل‌های tar.gz.enc مستقیماً در ریشه signature_archives/
        log("  [ARCHIVES] ساختار مسطح شناسایی شد — فایل‌ها مستقیماً در ریشه signature_archives/")
        for fname in top_level_names:
            if fname.endswith('.tar.gz.enc'):
                # sig_path همان نام فایل (بدون زیرپوشه)
                all_signatures.append({key_name: fname})
        log(f"  [ARCHIVES] تعداد فایل‌های tar.gz.enc در ریشه: {len(all_signatures)}")

    log(f"  [ARCHIVES] تعداد کل signatureهای یافت‌شده: {len(all_signatures)}")
    return all_signatures

# ================================ مرحله ۱: Work Queue ================================

def build_work_queue(repo, token):
    log("=" * 60)
    log("🔄 مرحله ۱: ساخت صف کاری (work queue)")
    log("=" * 60)

    # تنظیمات ثابت
    fixed_modules = ["combo_10day", "combo_monthly"]
    coins = [
        # تک‌ارزها (۵ عدد)
        "BTCUSDT", "ETHUSDT", "XRPUSDT", "SOLUSDT", "PAXGUSDT",
        # دوارز (۱۰ عدد)
        "BTCUSDT+ETHUSDT", "BTCUSDT+XRPUSDT", "BTCUSDT+SOLUSDT", "BTCUSDT+PAXGUSDT",
        "ETHUSDT+XRPUSDT", "ETHUSDT+SOLUSDT", "ETHUSDT+PAXGUSDT",
        "XRPUSDT+SOLUSDT", "XRPUSDT+PAXGUSDT", "SOLUSDT+PAXGUSDT",
        # سه‌ارز (۱۰ عدد)
        "BTCUSDT+ETHUSDT+XRPUSDT", "BTCUSDT+ETHUSDT+SOLUSDT", "BTCUSDT+ETHUSDT+PAXGUSDT",
        "BTCUSDT+XRPUSDT+SOLUSDT", "BTCUSDT+XRPUSDT+PAXGUSDT",
        "BTCUSDT+SOLUSDT+PAXGUSDT", "ETHUSDT+XRPUSDT+SOLUSDT",
        "ETHUSDT+XRPUSDT+PAXGUSDT", "ETHUSDT+SOLUSDT+PAXGUSDT",
        "XRPUSDT+SOLUSDT+PAXGUSDT",
        # چهارارز (۵ عدد)
        "BTCUSDT+ETHUSDT+XRPUSDT+SOLUSDT", "BTCUSDT+ETHUSDT+XRPUSDT+PAXGUSDT",
        "BTCUSDT+ETHUSDT+SOLUSDT+PAXGUSDT", "BTCUSDT+XRPUSDT+SOLUSDT+PAXGUSDT",
        "ETHUSDT+XRPUSDT+SOLUSDT+PAXGUSDT",
        # پنج‌ارز (۱ عدد)
        "BTCUSDT+ETHUSDT+XRPUSDT+SOLUSDT+PAXGUSDT",
    ]
    intervals_10day = [
        "fixed_5d","fixed_10d","fixed_15d","fixed_30d",
        "CPI_pre_1d","CPI_pre_2d","CPI_pre_3d","CPI_pre_5d",
        "CPI_post_1d","CPI_post_2d","CPI_post_3d","CPI_post_5d","CPI_post_7d","CPI_post_10d","CPI_post_15d",
        "CoreCPI_pre_1d","CoreCPI_pre_2d","CoreCPI_pre_3d","CoreCPI_pre_5d",
        "CoreCPI_post_1d","CoreCPI_post_2d","CoreCPI_post_3d","CoreCPI_post_5d","CoreCPI_post_7d","CoreCPI_post_10d","CoreCPI_post_15d",
        "PPI_pre_1d","PPI_pre_2d","PPI_pre_3d","PPI_pre_5d",
        "PPI_post_1d","PPI_post_2d","PPI_post_3d","PPI_post_5d","PPI_post_7d","PPI_post_10d","PPI_post_15d",
        "CorePPI_pre_1d","CorePPI_pre_2d","CorePPI_pre_3d","CorePPI_pre_5d",
        "CorePPI_post_1d","CorePPI_post_2d","CorePPI_post_3d","CorePPI_post_5d","CorePPI_post_7d","CorePPI_post_10d","CorePPI_post_15d",
        "FOMC_pre_1d","FOMC_pre_2d","FOMC_pre_3d","FOMC_pre_5d",
        "FOMC_post_1d","FOMC_post_2d","FOMC_post_3d","FOMC_post_5d","FOMC_post_7d","FOMC_post_10d","FOMC_post_15d",
        "CPI_y_y_pre_1d","CPI_y_y_pre_2d","CPI_y_y_pre_3d","CPI_y_y_pre_5d",
        "CPI_y_y_post_1d","CPI_y_y_post_2d","CPI_y_y_post_3d","CPI_y_y_post_5d","CPI_y_y_post_7d","CPI_y_y_post_10d","CPI_y_y_post_15d"
    ]
    # باگ تکرار fix: combo_monthly.py مقدار --interval را در محاسبه استفاده نمی‌کند
    # (فقط در نام فایل خروجی درج می‌شود — تابع process_analysis حتی پارامتر interval
    # نمی‌گیرد). بنابراین تولید ۷۰ ترکیب با همان intervals_10day برای combo_monthly
    # باعث می‌شد به ازای هر استراتژی ۶۵۱۰ ترکیب ساخته شود در حالی که فقط
    # ۳۱ کوین × ۳ مدل = ۹۳ ترکیب واقعاً متفاوت وجود دارد (مابقی اجرای کاملاً
    # تکراری همان تحلیل با نام فایل متفاوت بودند). یک مقدار interval ثابت
    # کافی است.
    intervals_monthly = ["monthly"]
    module_intervals = {
        "combo_10day": intervals_10day,
        "combo_monthly": intervals_monthly,
    }
    models = ["simple_hybrid", "fibonacci_full", "fibonacci_hybrid"]

    # خواندن strategies.txt یا دریافت از API
    strategies_file = os.environ.get('STRATEGIES_FILE', '/tmp/strategies.txt')
    log(f"  خواندن استراتژی‌ها از: {strategies_file}")
    if os.path.exists(strategies_file):
        with open(strategies_file, 'r') as f:
            strategies = [line.strip() for line in f if line.strip()]
        log(f"  تعداد استراتژی‌های خوانده‌شده از strategies.txt: {len(strategies)}")
    else:
        log(f"  فایل {strategies_file} یافت نشد — تلاش برای دریافت از API مخزن سوم...", 'WARNING')
        cmd = f"gh api repos/{repo}/contents/aggregated --jq '.[].name' 2>/dev/null"
        result = subprocess.run(cmd, shell=True, capture_output=True, text=True)
        if result.returncode == 0 and result.stdout.strip():
            strategies = [s.strip() for s in result.stdout.strip().split('\n') if s.strip()]
            log(f"  تعداد استراتژی‌های دریافت‌شده از API (aggregated/): {len(strategies)}")
        else:
            log("  دریافت استراتژی‌ها از API هم ناموفق بود", 'ERROR')
            raise RuntimeError("strategies.txt یافت نشد و دریافت از API نیز ناموفق بود")

    if not strategies:
        log("  هیچ استراتژی‌ای یافت نشد", 'WARNING')
        return 0

    # دانلود completed_strategies.json
    log("  دانلود completed_strategies.json...")
    completed_content = gh_api_get(repo, "completed_strategies.json")
    if completed_content is not None:
        try:
            completed_strategies = json.loads(completed_content)
            if not isinstance(completed_strategies, list):
                completed_strategies = []
        except Exception:
            completed_strategies = []
    else:
        completed_strategies = []
    log(f"  تعداد استراتژی‌های قبلاً پردازش‌شده: {len(completed_strategies)}")

    # دانلود all_combinations.json
    log("  دانلود all_combinations.json...")
    all_combos_content = gh_api_get(repo, "all_combinations.json")
    if all_combos_content is not None:
        try:
            existing_combos = json.loads(all_combos_content)
            if not isinstance(existing_combos, list):
                existing_combos = []
        except Exception:
            existing_combos = []
    else:
        existing_combos = []
    log(f"  تعداد ترکیب‌های موجود در صف: {len(existing_combos)}")

    # فیلتر استراتژی‌های جدید
    strategies_to_process = [s for s in strategies if s not in completed_strategies]
    log(f"  تعداد استراتژی‌های جدید (نه در completed): {len(strategies_to_process)}")

    if not strategies_to_process:
        log("  هیچ استراتژی جدیدی برای پردازش وجود ندارد ✅")
        return 0

    log(f"  استراتژی‌های جدید: {strategies_to_process}")

    # تولید ترکیب‌ها
    new_combos = []
    for strat in strategies_to_process:
        strat_combos = []
        for mod in fixed_modules:
            mod_intervals = module_intervals[mod]
            for coin in coins:
                for interval in mod_intervals:
                    for model in models:
                        strat_combos.append({
                            "module": mod,
                            "strat": strat,
                            "coin": coin,
                            "interval": interval,
                            "model": model
                        })
        log(f"  استراتژی '{strat}': {len(strat_combos)} ترکیب تولید شد")
        new_combos.extend(strat_combos)

    log(f"  تعداد کل ترکیب‌های جدید: {len(new_combos)}")

    updated_combos = existing_combos + new_combos
    log(f"  تعداد کل ترکیب‌ها پس از اضافه شدن: {len(updated_combos)}")

    # آپلود all_combinations.json
    tmp_file = "/tmp/all_combinations.json"
    with open(tmp_file, "w", encoding="utf-8") as f:
        json.dump(updated_combos, f, indent=2, ensure_ascii=False)

    sha_all = get_file_sha(repo, "all_combinations.json")
    if not upload_file_with_curl(repo, "all_combinations.json", tmp_file, sha_all, "update work queue"):
        raise RuntimeError("آپلود all_combinations.json ناموفق بود")

    # آپدیت completed_strategies.json
    updated_completed = completed_strategies + strategies_to_process
    tmp_completed = "/tmp/completed_strategies.json"
    with open(tmp_completed, "w", encoding="utf-8") as f:
        json.dump(updated_completed, f, indent=2, ensure_ascii=False)

    sha_completed = get_file_sha(repo, "completed_strategies.json")
    if not upload_file_with_curl(repo, "completed_strategies.json", tmp_completed, sha_completed, "update completed strategies"):
        raise RuntimeError("آپدیت completed_strategies.json ناموفق بود")

    log(f"✅ تعداد {len(new_combos)} ترکیب جدید به all_combinations.json اضافه شد")
    return len(new_combos)

# ================================ مرحله ۲: Golden Queue ================================

def build_golden_queue(repo, token):
    log("=" * 60)
    log("🔄 مرحله ۲: ساخت صف golden")
    log("=" * 60)

    # دانلود completed_golden.json
    log("  دانلود completed_golden.json...")
    completed_content = gh_api_get(repo, "completed_golden.json")
    if completed_content is not None:
        try:
            completed_golden = json.loads(completed_content)
            if not isinstance(completed_golden, list):
                completed_golden = []
        except Exception:
            completed_golden = []
    else:
        completed_golden = []

    completed_set = set(completed_golden)
    log(f"  تعداد signatureهایی که قبلاً در completed_golden.json ثبت شده‌اند: {len(completed_set)}")

    # دریافت signatures از آرشیوها
    all_signatures = get_all_signatures_from_archives(repo, key_name="signature_path")
    log(f"  تعداد signatureهای پیدا شده در signature_archives/: {len(all_signatures)}")

    if not all_signatures:
        log("  هیچ signature‌ای در آرشیوها یافت نشد ✅")
        return 0

    # فیلتر جدید — هم از completed و هم از صف موجود حذف می‌کنیم
    # (بدون این، آیتم‌هایی که هنوز در صف‌اند ولی پردازش نشدند دوباره اضافه می‌شوند)
    new_signatures = [s for s in all_signatures if s["signature_path"] not in completed_set]
    log(f"  تعداد signatureهای جدید (نه در completed_golden): {len(new_signatures)}")

    if not new_signatures:
        log("  هیچ signature جدیدی برای اضافه کردن وجود ندارد ✅")
        return 0

    # دانلود صف موجود
    log("  دانلود all_combinations_golden.json...")
    combos_content = gh_api_get(repo, "all_combinations_golden.json")
    if combos_content is not None:
        try:
            existing_combos = json.loads(combos_content)
            if not isinstance(existing_combos, list):
                existing_combos = []
        except Exception:
            existing_combos = []
    else:
        existing_combos = []
    log(f"  ترکیب‌های موجود در صف: {len(existing_combos)}")

    # باگ dedup fix: آیتم‌هایی که هنوز در صف‌اند ولی پردازش نشدند رو هم حذف می‌کنیم
    # (completed_golden فقط پردازش‌شده‌ها رو داره — اگر cleanup کار نکرده باشه،
    # این آیتم‌ها در صف می‌مانند ولی در completed نیستند → بدون این فیلتر دوباره اضافه می‌شوند)
    existing_in_queue = {
        s["signature_path"] if isinstance(s, dict) else s
        for s in existing_combos
    }
    new_signatures = [
        s for s in new_signatures
        if s["signature_path"] not in existing_in_queue
    ]
    log(f"  تعداد signatureهای واقعاً جدید (نه در صف و نه در completed): {len(new_signatures)}")

    updated_combos = existing_combos + new_signatures
    log(f"  تعداد کل ترکیب‌ها پس از اضافه شدن: {len(updated_combos)}")

    # آپلود
    tmp_file = "/tmp/all_combinations_golden.json"
    with open(tmp_file, "w", encoding="utf-8") as f:
        json.dump(updated_combos, f, indent=2, ensure_ascii=False)

    sha_combos = get_file_sha(repo, "all_combinations_golden.json")
    if not upload_file_with_curl(repo, "all_combinations_golden.json", tmp_file, sha_combos, "update golden queue"):
        raise RuntimeError("آپلود all_combinations_golden.json ناموفق بود")

    # آپدیت completed_golden.json
    new_sig_paths = [s["signature_path"] for s in new_signatures]
    updated_completed = completed_golden + new_sig_paths
    tmp_completed = "/tmp/completed_golden.json"
    with open(tmp_completed, "w", encoding="utf-8") as f:
        json.dump(updated_completed, f, indent=2, ensure_ascii=False)

    sha_completed = get_file_sha(repo, "completed_golden.json")
    if not upload_file_with_curl(repo, "completed_golden.json", tmp_completed, sha_completed, "update completed golden"):
        raise RuntimeError("آپدیت completed_golden.json ناموفق بود")

    log(f"✅ تعداد {len(new_signatures)} signature جدید به all_combinations_golden.json اضافه شد")
    return len(new_signatures)

# ================================ مرحله ۳: Correlation Queue ================================

def build_correlation_queue(repo, token):
    log("=" * 60)
    log("🔄 مرحله ۳: ساخت صف correlation")
    log("=" * 60)

    # دانلود completed_correlation.json
    log("  دانلود completed_correlation.json...")
    completed_content = gh_api_get(repo, "completed_correlation.json")
    if completed_content is not None:
        try:
            completed_correlation = json.loads(completed_content)
            if not isinstance(completed_correlation, list):
                completed_correlation = []
        except Exception:
            completed_correlation = []
    else:
        completed_correlation = []

    completed_set = set(completed_correlation)
    log(f"  تعداد signatureهایی که قبلاً در completed_correlation.json ثبت شده‌اند: {len(completed_set)}")

    # دریافت signatures — با فرمت {coin_composition, signature} (تفاوت با golden)
    all_signatures_raw = get_all_signatures_from_archives(repo, key_name="signature")
    log(f"  تعداد signatureهای پیدا شده در signature_archives/: {len(all_signatures_raw)}")

    if not all_signatures_raw:
        log("  هیچ signature‌ای در آرشیوها یافت نشد ✅")
        return 0

    # ساخت آیتم‌های صف با کلیدهای coin_composition و signature
    # coin_composition = قسمت قبل از اولین _ در نام فایل signature
    def _extract_coin_composition(sig_value):
        # sig_value ممکن است مسیر کامل (module/strategy/coin/base) یا فقط نام فایل باشد
        filename = sig_value.split('/')[-1]  # آخرین قسمت مسیر
        # اگر نام فایل با پسوند tar.gz.enc باشد، آن را حذف می‌کنیم
        if filename.endswith('.tar.gz.enc'):
            filename = filename[:-len('.tar.gz.enc')]
        return filename.split('_')[0]

    all_signatures = []
    for raw in all_signatures_raw:
        sig_value = raw["signature"]
        coin_comp = _extract_coin_composition(sig_value)
        all_signatures.append({"coin_composition": coin_comp, "signature": sig_value})

    completed_set = set(completed_correlation)
    new_signatures = [s for s in all_signatures if s["signature"] not in completed_set]
    log(f"  تعداد signatureهای جدید (نه در completed_correlation): {len(new_signatures)}")

    if not new_signatures:
        log("  هیچ signature جدیدی برای اضافه کردن وجود ندارد ✅")
        return 0

    # دانلود صف موجود
    log("  دانلود all_combinations_correlation.json...")
    combos_content = gh_api_get(repo, "all_combinations_correlation.json")
    if combos_content is not None:
        try:
            existing_combos = json.loads(combos_content)
            if not isinstance(existing_combos, list):
                existing_combos = []
        except Exception:
            existing_combos = []
    else:
        existing_combos = []
    log(f"  ترکیب‌های موجود در صف: {len(existing_combos)}")

    updated_combos = existing_combos + new_signatures

    tmp_file = "/tmp/all_combinations_correlation.json"
    with open(tmp_file, "w", encoding="utf-8") as f:
        json.dump(updated_combos, f, indent=2, ensure_ascii=False)

    sha_combos = get_file_sha(repo, "all_combinations_correlation.json")
    if not upload_file_with_curl(repo, "all_combinations_correlation.json", tmp_file, sha_combos, "update correlation queue"):
        raise RuntimeError("آپلود all_combinations_correlation.json ناموفق بود")

    new_sig_paths = [s["signature"] for s in new_signatures]
    updated_completed = completed_correlation + new_sig_paths
    tmp_completed = "/tmp/completed_correlation.json"
    with open(tmp_completed, "w", encoding="utf-8") as f:
        json.dump(updated_completed, f, indent=2, ensure_ascii=False)

    sha_completed = get_file_sha(repo, "completed_correlation.json")
    if not upload_file_with_curl(repo, "completed_correlation.json", tmp_completed, sha_completed, "update completed correlation"):
        raise RuntimeError("آپدیت completed_correlation.json ناموفق بود")

    log(f"✅ تعداد {len(new_signatures)} signature جدید به all_combinations_correlation.json اضافه شد")
    return len(new_signatures)

# ================================ مرحله ۴: Portfolios Queue ================================

def _install_pandas():
    log("  نصب pandas و pyarrow...")
    r = subprocess.run(
        [sys.executable, "-m", "pip", "install", "pandas", "pyarrow",
         "--quiet", "--break-system-packages"],
        capture_output=True, text=True
    )
    if r.returncode != 0:
        r2 = subprocess.run(
            [sys.executable, "-m", "pip", "install", "pandas", "pyarrow", "--quiet"],
            capture_output=True, text=True
        )
        if r2.returncode != 0:
            log(f"  نصب ناموفق: {r2.stderr[:200]}", 'ERROR')
            return False
    log("  pandas و pyarrow نصب شدند ✅")
    return True

def _get_qualified_signatures(parquet_path, min_score=45):
    """خواندن golden_scores.parquet و فیلتر score >= min_score"""
    try:
        import pandas as pd
    except ImportError:
        if not _install_pandas():
            return []
        import pandas as pd

    try:
        df = pd.read_parquet(parquet_path)
        log(f"  ستون‌های parquet: {list(df.columns)}")
        log(f"  تعداد کل سطرها: {len(df)}")

        score_col = None
        for col in ['score', 'golden_score', 'total_score', 'final_score']:
            if col in df.columns:
                score_col = col
                break
        if score_col is None:
            numeric_cols = df.select_dtypes(include='number').columns.tolist()
            if numeric_cols:
                score_col = numeric_cols[0]
                log(f"  ستون 'score' یافت نشد، از '{score_col}' استفاده می‌شود", 'WARNING')
            else:
                log("  هیچ ستون عددی در parquet یافت نشد", 'ERROR')
                return []

        sig_col = None
        for col in ['signature', 'signature_path', 'sig', 'name']:
            if col in df.columns:
                sig_col = col
                break
        if sig_col is None:
            str_cols = df.select_dtypes(include='object').columns.tolist()
            if str_cols:
                sig_col = str_cols[0]
                log(f"  ستون 'signature' یافت نشد، از '{sig_col}' استفاده می‌شود", 'WARNING')
            else:
                log("  هیچ ستون رشته‌ای در parquet یافت نشد", 'ERROR')
                return []

        filtered = df[df[score_col] >= min_score]
        log(f"  تعداد استراتژی‌های با امتیاز >= {min_score}: {len(filtered)}")

        return [
            {"signature_path": str(row[sig_col]), "score": float(row[score_col])}
            for _, row in filtered.iterrows()
        ]
    except Exception as e:
        log(f"  خطا در خواندن parquet: {e}", 'ERROR')
        return []

def build_portfolios_queue(repo, token, password, min_score=45.0):
    log("=" * 60)
    log("🔄 مرحله ۴: ساخت صف portfolios")
    log("=" * 60)

    # دانلود completed_portfolios.json
    log("  دانلود completed_portfolios.json...")
    completed_content = gh_api_get(repo, "completed_portfolios.json")
    if completed_content is not None:
        try:
            completed_portfolios = json.loads(completed_content)
            if not isinstance(completed_portfolios, list):
                completed_portfolios = []
        except Exception:
            completed_portfolios = []
    else:
        completed_portfolios = []

    completed_set = set(completed_portfolios)
    log(f"  تعداد signatureهای قبلاً ثبت‌شده: {len(completed_set)}")

    # دانلود و رمزگشایی golden_scores.parquet.enc
    enc_path = "/tmp/golden_scores.parquet.enc"
    dec_path = "/tmp/golden_scores.parquet"

    log("  دانلود golden_scores.parquet.enc...")
    if not gh_api_get_binary(repo, "analysis_results/golden_scores.parquet.enc", enc_path):
        log("  golden_scores.parquet.enc یافت نشد — احتمالاً golden هنوز اجرا نشده. رد می‌شویم.", 'WARNING')
        return 0

    log("  رمزگشایی golden_scores.parquet.enc...")
    if not decrypt_file(enc_path, dec_path, password):
        raise RuntimeError("رمزگشایی golden_scores.parquet.enc ناموفق بود")

    if not os.path.exists(dec_path) or os.path.getsize(dec_path) == 0:
        raise RuntimeError("فایل رمزگشایی‌شده خالی است")

    log(f"  رمزگشایی موفق ({os.path.getsize(dec_path)} bytes) ✅")

    # فیلتر signatureهای واجد شرایط
    log(f"  فیلتر signatureها با score >= {min_score}...")
    qualified = _get_qualified_signatures(dec_path, min_score)

    for fp in [enc_path, dec_path]:
        try:
            os.unlink(fp)
        except Exception:
            pass

    if not qualified:
        log("  هیچ signature واجد شرایطی یافت نشد ✅")
        return 0

    # فیلتر جدید
    new_signatures = [s for s in qualified if s["signature_path"] not in completed_set]
    log(f"  تعداد signatureهای جدید (نه در completed_portfolios): {len(new_signatures)}")

    if not new_signatures:
        log("  هیچ signature جدیدی برای اضافه کردن وجود ندارد ✅")
        return 0

    # دانلود صف موجود
    log("  دانلود all_combinations_portfolios.json...")
    combos_content = gh_api_get(repo, "all_combinations_portfolios.json")
    if combos_content is not None:
        try:
            existing_combos = json.loads(combos_content)
            if not isinstance(existing_combos, list):
                existing_combos = []
        except Exception:
            existing_combos = []
    else:
        existing_combos = []
    log(f"  ترکیب‌های موجود در صف: {len(existing_combos)}")

    updated_combos = existing_combos + new_signatures

    tmp_file = "/tmp/all_combinations_portfolios.json"
    with open(tmp_file, "w", encoding="utf-8") as f:
        json.dump(updated_combos, f, indent=2, ensure_ascii=False)

    sha_combos = get_file_sha(repo, "all_combinations_portfolios.json")
    if not upload_file_with_curl(repo, "all_combinations_portfolios.json", tmp_file, sha_combos, "update portfolios queue"):
        raise RuntimeError("آپلود all_combinations_portfolios.json ناموفق بود")

    new_sig_paths = [s["signature_path"] for s in new_signatures]
    updated_completed = completed_portfolios + new_sig_paths
    tmp_completed = "/tmp/completed_portfolios.json"
    with open(tmp_completed, "w", encoding="utf-8") as f:
        json.dump(updated_completed, f, indent=2, ensure_ascii=False)

    sha_completed = get_file_sha(repo, "completed_portfolios.json")
    if not upload_file_with_curl(repo, "completed_portfolios.json", tmp_completed, sha_completed, "update completed portfolios"):
        raise RuntimeError("آپدیت completed_portfolios.json ناموفق بود")

    log(f"✅ تعداد {len(new_signatures)} signature جدید به all_combinations_portfolios.json اضافه شد")
    return len(new_signatures)

# ================================ تابع اصلی ================================

def main():
    # تنظیم فایل لاگ
    global LOG_FILE
    log_dir = Path("logs")
    log_dir.mkdir(exist_ok=True)
    LOG_FILE = str(log_dir / "queue_builder.log")

    log("=" * 60)
    log("🚀 شروع build_all_queues.py")
    log("=" * 60)

    # خواندن environment variables
    third_repo = os.environ.get('THIRD_REPO')
    gh_token = os.environ.get('GH_TOKEN')
    results_password = os.environ.get('RESULTS_PASSWORD') or os.environ.get('DATA_PASSWORD')

    log(f"  THIRD_REPO: {'✅ ' + third_repo if third_repo else '❌ تنظیم نشده'}")
    log(f"  GH_TOKEN: {'✅ تنظیم شده' if gh_token else '❌ تنظیم نشده'}")
    log(f"  RESULTS_PASSWORD: {'✅ تنظیم شده' if results_password else '⚠️ تنظیم نشده (portfolios نیاز دارد)'}")

    if not third_repo or not gh_token:
        log("THIRD_REPO یا GH_TOKEN تنظیم نشده است. خروج.", 'ERROR')
        sys.exit(1)

    results = {}

    # مرحله ۱: work
    try:
        count = build_work_queue(third_repo, gh_token)
        results['work'] = count
        log(f"✅ مرحله ۱ (work) با موفقیت تمام شد — {count} ترکیب جدید")
    except Exception as e:
        log(f"❌ مرحله ۱ (work) با خطا مواجه شد: {e}", 'ERROR')
        sys.exit(1)

    # مرحله ۲: golden
    try:
        count = build_golden_queue(third_repo, gh_token)
        results['golden'] = count
        log(f"✅ مرحله ۲ (golden) با موفقیت تمام شد — {count} signature جدید")
    except Exception as e:
        log(f"❌ مرحله ۲ (golden) با خطا مواجه شد: {e}", 'ERROR')
        sys.exit(1)

    # مرحله ۳: correlation
    try:
        count = build_correlation_queue(third_repo, gh_token)
        results['correlation'] = count
        log(f"✅ مرحله ۳ (correlation) با موفقیت تمام شد — {count} signature جدید")
    except Exception as e:
        log(f"❌ مرحله ۳ (correlation) با خطا مواجه شد: {e}", 'ERROR')
        sys.exit(1)

    # مرحله ۴: portfolios
    if not results_password:
        log("⚠️ RESULTS_PASSWORD تنظیم نشده — مرحله portfolios رد می‌شود", 'WARNING')
        results['portfolios'] = 0
    else:
        try:
            count = build_portfolios_queue(third_repo, gh_token, results_password)
            results['portfolios'] = count
            log(f"✅ مرحله ۴ (portfolios) با موفقیت تمام شد — {count} signature جدید")
        except Exception as e:
            log(f"❌ مرحله ۴ (portfolios) با خطا مواجه شد: {e}", 'ERROR')
            sys.exit(1)

    # خلاصه نهایی
    log("=" * 60)
    log("📊 خلاصه کل عملیات:")
    log(f"   work        → {results.get('work', 0)} ترکیب جدید")
    log(f"   golden      → {results.get('golden', 0)} signature جدید")
    log(f"   correlation → {results.get('correlation', 0)} signature جدید")
    log(f"   portfolios  → {results.get('portfolios', 0)} signature جدید")
    log("✅ همه‌ی صف‌ها با موفقیت ساخته شدند")
    log(f"📄 فایل لاگ: {LOG_FILE}")
    log("=" * 60)

if __name__ == "__main__":
    main()
