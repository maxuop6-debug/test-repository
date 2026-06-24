#!/usr/bin/env python3
# build_portfolios_queue.py - تولید و به‌روزرسانی all_combinations_portfolios.json
# منطق: signatureهایی که در golden_scores.parquet امتیاز >= 45 دارند
# تنها مکانیزم dedup: completed_portfolios.json

import os
import sys
import json
import argparse
import subprocess
import base64
import tempfile

# ================================ توابع کمکی ================================

def gh_api_get(repo, path):
    cmd = f"gh api repos/{repo}/contents/{path} --jq '.content' 2>/dev/null"
    result = subprocess.run(cmd, shell=True, capture_output=True, text=True)
    if result.returncode != 0 or not result.stdout.strip():
        return None
    return base64.b64decode(result.stdout.strip()).decode('utf-8')

def gh_api_get_binary(repo, path, dest_path):
    """دانلود یک فایل binary (مثل .parquet.enc) از مخزن سوم"""
    token = os.environ.get('GH_TOKEN')
    cmd = [
        "curl", "-s", "-L",
        "-H", f"Authorization: token {token}",
        "-H", "Accept: application/vnd.github.v3.raw",
        f"https://api.github.com/repos/{repo}/contents/{path}",
        "-o", dest_path
    ]
    result = subprocess.run(cmd, capture_output=True)
    return result.returncode == 0 and os.path.exists(dest_path) and os.path.getsize(dest_path) > 0

def upload_file_with_curl(repo, remote_path, local_file_path, sha=None):
    with open(local_file_path, 'rb') as f:
        content_bytes = f.read()
    encoded = base64.b64encode(content_bytes).decode()

    temp_payload = "/tmp/payload_portfolios.json"
    with open(temp_payload, 'w') as f:
        f.write('{"message":"update portfolios queue","content":"')
    with open(temp_payload, 'ab') as f:
        f.write(encoded.encode())
    with open(temp_payload, 'a') as f:
        f.write('","branch":"main"')
        if sha:
            f.write(f',"sha":"{sha}"')
        f.write('}')

    token = os.environ.get('GH_TOKEN')
    if not token:
        print("❌ GH_TOKEN تنظیم نشده است.")
        return False

    cmd = [
        "curl", "-s", "-X", "PUT",
        "-H", f"Authorization: token {token}",
        "-H", "Content-Type: application/json",
        "-d", f"@{temp_payload}",
        f"https://api.github.com/repos/{repo}/contents/{remote_path}"
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    os.unlink(temp_payload)

    if result.returncode != 0:
        print(f"⚠️ خطا در curl: {result.stderr}")
        return False

    try:
        resp = json.loads(result.stdout)
        if 'content' in resp:
            return True
        else:
            print(f"⚠️ پاسخ ناموفق: {resp.get('message', 'خطای ناشناخته')}")
            return False
    except:
        print(f"⚠️ خطا در parse پاسخ: {result.stdout[:200]}")
        return False

def get_file_sha(repo, path):
    cmd = f"gh api repos/{repo}/contents/{path} --jq '.sha' 2>/dev/null"
    result = subprocess.run(cmd, shell=True, capture_output=True, text=True)
    if result.returncode == 0 and result.stdout.strip():
        return result.stdout.strip()
    return None

# ================================ رمزگشایی و خواندن parquet ================================

def decrypt_file(enc_path, dec_path, password):
    """رمزگشایی فایل .enc با Node.js (مثل aggregate-results در YAML)"""
    node_code = f"""
const crypto=require('crypto'),fs=require('fs');
const pass='{password.replace("'", "\\'")}';
const data=fs.readFileSync('{enc_path}');
const key=crypto.scryptSync(pass,'salt',32);
const iv=data.slice(0,16),enc=data.slice(16);
const decipher=crypto.createDecipheriv('aes-256-cbc',key,iv);
const dec=Buffer.concat([decipher.update(enc),decipher.final()]);
fs.writeFileSync('{dec_path}',dec);
"""
    result = subprocess.run(["node", "-e", node_code], capture_output=True, text=True)
    return result.returncode == 0

def get_qualified_signatures_from_parquet(parquet_path, min_score=45):
    """
    خواندن golden_scores.parquet و فیلتر کردن signatureهایی با score >= min_score.
    از pandas استفاده می‌کنیم.
    """
    try:
        import pandas as pd
        df = pd.read_parquet(parquet_path)
        print(f"   ستون‌های parquet: {list(df.columns)}")
        print(f"   تعداد کل سطرها: {len(df)}")

        # پیدا کردن ستون امتیاز (score یا معادل آن)
        score_col = None
        for col in ['score', 'golden_score', 'total_score', 'final_score']:
            if col in df.columns:
                score_col = col
                break
        if score_col is None:
            # اگر ستون score نبود، از اولین ستون عددی که احتمالاً score است استفاده کن
            numeric_cols = df.select_dtypes(include='number').columns.tolist()
            if numeric_cols:
                score_col = numeric_cols[0]
                print(f"   ⚠️ ستون 'score' یافت نشد. از ستون '{score_col}' به‌عنوان جایگزین استفاده می‌شود.")
            else:
                print("   ❌ هیچ ستون عددی در parquet یافت نشد.")
                return []

        # پیدا کردن ستون signature
        sig_col = None
        for col in ['signature', 'signature_path', 'sig', 'name']:
            if col in df.columns:
                sig_col = col
                break
        if sig_col is None:
            # از اولین ستون رشته‌ای استفاده کن
            str_cols = df.select_dtypes(include='object').columns.tolist()
            if str_cols:
                sig_col = str_cols[0]
                print(f"   ⚠️ ستون 'signature' یافت نشد. از ستون '{sig_col}' استفاده می‌شود.")
            else:
                print("   ❌ هیچ ستون رشته‌ای در parquet یافت نشد.")
                return []

        filtered = df[df[score_col] >= min_score]
        print(f"   📊 تعداد signatureهای با score >= {min_score}: {len(filtered)}")

        signatures = [
            {"signature_path": str(row[sig_col]), "score": float(row[score_col])}
            for _, row in filtered.iterrows()
        ]
        return signatures

    except ImportError:
        print("   ❌ pandas نصب نشده است. در حال نصب...")
        subprocess.run([sys.executable, "-m", "pip", "install", "pandas", "pyarrow", "--quiet"], check=True)
        # تلاش مجدد
        import pandas as pd
        return get_qualified_signatures_from_parquet(parquet_path, min_score)
    except Exception as e:
        print(f"   ❌ خطا در خواندن parquet: {e}")
        return []

# ================================ تابع اصلی ================================

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--progress", default="/tmp/dummy", help="برای سازگاری")
    parser.add_argument("--strategies", default=None, help="برای سازگاری (استفاده نمی‌شود)")
    parser.add_argument("--output-dir", default="/tmp/dummy", help="برای سازگاری")
    parser.add_argument("--batch-size", type=int, default=100, help="سایز بسته")
    parser.add_argument("--min-score", type=float, default=45.0, help="حداقل امتیاز برای portfolios")
    args = parser.parse_args()

    third_repo = os.environ.get('THIRD_REPO')
    gh_token = os.environ.get('GH_TOKEN')
    results_password = os.environ.get('RESULTS_PASSWORD')

    if not third_repo or not gh_token:
        print("❌ THIRD_REPO یا GH_TOKEN تنظیم نشده است.")
        sys.exit(1)
    if not results_password:
        print("❌ RESULTS_PASSWORD تنظیم نشده است.")
        sys.exit(1)

    # ========== 1. دانلود completed_portfolios.json ==========
    print("📥 دانلود completed_portfolios.json از مخزن سوم...")
    completed_content = gh_api_get(third_repo, "completed_portfolios.json")
    if completed_content is not None:
        try:
            completed_portfolios = json.loads(completed_content)
            if not isinstance(completed_portfolios, list):
                completed_portfolios = []
        except:
            completed_portfolios = []
    else:
        completed_portfolios = []

    completed_set = set(completed_portfolios)
    print(f"   تعداد signatureهای قبلاً ثبت‌شده: {len(completed_set)}")

    # ========== 2. دانلود و رمزگشایی golden_scores.parquet ==========
    print("📥 دانلود golden_scores.parquet.enc از مخزن سوم...")
    enc_path = "/tmp/golden_scores.parquet.enc"
    dec_path = "/tmp/golden_scores.parquet"

    # دانلود فایل binary
    if not gh_api_get_binary(third_repo, "analysis_results/golden_scores.parquet.enc", enc_path):
        print("❌ خطا در دانلود golden_scores.parquet.enc")
        print("   (احتمالاً golden هنوز اجرا نشده و فایل وجود ندارد)")
        sys.exit(0)  # exit 0 چون شاید golden هنوز اجرا نشده

    print("🔓 رمزگشایی golden_scores.parquet.enc...")
    if not decrypt_file(enc_path, dec_path, results_password):
        print("❌ خطا در رمزگشایی golden_scores.parquet.enc")
        sys.exit(1)

    # ========== 3. خواندن signatureهای واجد شرایط ==========
    print(f"📊 فیلتر signatureها با score >= {args.min_score}...")
    qualified = get_qualified_signatures_from_parquet(dec_path, args.min_score)

    # پاکسازی فایل‌های موقت
    for f in [enc_path, dec_path]:
        try:
            os.unlink(f)
        except:
            pass

    if not qualified:
        print("✅ هیچ signature واجد شرایطی یافت نشد.")
        sys.exit(0)

    # ========== 4. فیلتر: فقط signatureهای جدید ==========
    new_signatures = [
        s for s in qualified
        if s["signature_path"] not in completed_set
    ]
    print(f"   signatureهای جدید (نه در completed_portfolios): {len(new_signatures)}")

    if not new_signatures:
        print("✅ هیچ signature جدیدی برای اضافه کردن وجود ندارد.")
        sys.exit(0)

    # ========== 5. دانلود all_combinations_portfolios.json موجود ==========
    print("📥 دانلود all_combinations_portfolios.json از مخزن سوم...")
    combos_content = gh_api_get(third_repo, "all_combinations_portfolios.json")
    if combos_content is not None:
        try:
            existing_combos = json.loads(combos_content)
            if not isinstance(existing_combos, list):
                existing_combos = []
        except:
            existing_combos = []
    else:
        existing_combos = []
    print(f"   ترکیب‌های موجود در صف: {len(existing_combos)}")

    # ========== 6. اضافه کردن signatureهای جدید ==========
    updated_combos = existing_combos + new_signatures
    print(f"   📊 تعداد کل ترکیب‌ها پس از اضافه شدن: {len(updated_combos)}")

    # ========== 7. آپلود all_combinations_portfolios.json ==========
    tmp_file = "/tmp/all_combinations_portfolios.json"
    with open(tmp_file, "w", encoding="utf-8") as f:
        json.dump(updated_combos, f, indent=2, ensure_ascii=False)

    print("📤 آپلود all_combinations_portfolios.json...")
    sha_combos = get_file_sha(third_repo, "all_combinations_portfolios.json")
    if upload_file_with_curl(third_repo, "all_combinations_portfolios.json", tmp_file, sha_combos):
        print("✅ all_combinations_portfolios.json با موفقیت آپلود شد.")
    else:
        print("❌ خطا در آپلود all_combinations_portfolios.json")
        sys.exit(1)

    # ========== 8. آپدیت completed_portfolios.json ==========
    new_sig_paths = [s["signature_path"] for s in new_signatures]
    updated_completed = completed_portfolios + new_sig_paths

    print(f"📤 آپدیت completed_portfolios.json با {len(new_sig_paths)} signature جدید...")
    tmp_completed = "/tmp/completed_portfolios.json"
    with open(tmp_completed, "w", encoding="utf-8") as f:
        json.dump(updated_completed, f, indent=2, ensure_ascii=False)

    sha_completed = get_file_sha(third_repo, "completed_portfolios.json")
    if upload_file_with_curl(third_repo, "completed_portfolios.json", tmp_completed, sha_completed):
        print("✅ completed_portfolios.json با موفقیت آپدیت شد.")
    else:
        print("❌ خطا در آپدیت completed_portfolios.json — all_combinations_portfolios.json آپلود شد")
        print("   اما completed_portfolios آپدیت نشد. در اجرای بعدی همین signatureها دوباره پردازش می‌شوند.")
        sys.exit(1)

    print("✅ عملیات ساخت صف portfolios با موفقیت پایان یافت.")

if __name__ == "__main__":
    main()
