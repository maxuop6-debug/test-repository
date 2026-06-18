#!/usr/bin/env python3
"""
restore.py – Decrypt and extract a backup bundle.

Usage:
    python restore.py <bundle.tar.xz.enc> <password> [output_dir]

Example:
    python restore.py backup_2026-06-18_14-30-00_000.tar.xz.enc MySecret ./restored
"""

import sys, os, hashlib, lzma, tarfile, json, io, csv, datetime
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

ORIGIN = datetime.date(2000, 1, 1)

def decrypt(data: bytes, key: bytes) -> bytes:
    return AESGCM(key).decrypt(data[:12], data[12:], None)

def load_mapping(mapping_path="auto_mapping.json"):
    if not os.path.exists(mapping_path):
        return {}, {}
    with open(mapping_path) as f:
        m = json.load(f)
    inv_vals = {v: k for k, v in m.get("values", {}).items()}
    inv_cols = {v: k for k, v in m.get("columns", {}).items()}
    return inv_vals, inv_cols

def unmap_csv(data: bytes, inv_vals, inv_cols) -> bytes:
    text = data.decode("utf-8", errors="replace")
    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames:
        return data
    orig_cols = [inv_cols.get(c, c) for c in reader.fieldnames]
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=orig_cols)
    writer.writeheader()
    for row in reader:
        new_row = {}
        for mapped_col, val in row.items():
            orig_col = inv_cols.get(mapped_col, mapped_col)
            if orig_col == "date":
                try:
                    new_row[orig_col] = (ORIGIN + datetime.timedelta(days=int(val))).isoformat()
                except:
                    new_row[orig_col] = inv_vals.get(val, val)
            else:
                new_row[orig_col] = inv_vals.get(val, val)
        writer.writerow(new_row)
    return buf.getvalue().encode()

def main():
    if len(sys.argv) < 3:
        print(__doc__); sys.exit(1)

    bundle_path = sys.argv[1]
    password = sys.argv[2].encode()
    out_dir = sys.argv[3] if len(sys.argv) > 3 else "./restored"
    key = hashlib.sha256(password).digest()

    print(f"Reading: {bundle_path}")
    with open(bundle_path, "rb") as f:
        encrypted = f.read()

    # Outer layer: encrypted tar.xz
    print("Decrypting outer bundle…")
    tar_xz_data = decrypt(encrypted, key)

    print("Extracting tar.xz…")
    with tarfile.open(fileobj=io.BytesIO(tar_xz_data), mode="r:xz") as tar:
        members = tar.getmembers()
        # Load index
        idx_member = next((m for m in members if m.name == "index.json"), None)
        if idx_member:
            index = json.loads(tar.extractfile(idx_member).read())
            print(f"Bundle index: {json.dumps(index, indent=2)}")

        inv_vals, inv_cols = load_mapping()

        for member in members:
            if member.name == "index.json":
                continue
            fobj = tar.extractfile(member)
            if fobj is None:
                continue
            raw = fobj.read()

            # Process based on extension
            name = member.name
            if name.endswith(".lzxenc"):
                # Decrypt + decompress
                try:
                    dec = decrypt(raw, key)
                except Exception as e:
                    print(f"  Decrypt failed for {name}: {e}")
                    dec = raw
                try:
                    decompressed = lzma.decompress(dec)
                except Exception as e:
                    print(f"  Decompress failed for {name}: {e}")
                    decompressed = dec

                base_name = name[:-len(".lzxenc")]
                # If it's an analysis CSV, unmap
                if "analysis_results" in name and base_name.endswith(".csv"):
                    if inv_vals or inv_cols:
                        decompressed = unmap_csv(decompressed, inv_vals, inv_cols)
                        print(f"  Unmapped: {base_name}")
                final_path = os.path.join(out_dir, base_name)
                final_data = decompressed
            elif name.endswith(".enc"):
                try:
                    final_data = decrypt(raw, key)
                except Exception as e:
                    print(f"  Decrypt failed for {name}: {e}")
                    final_data = raw
                final_path = os.path.join(out_dir, name[:-4])
            else:
                final_data = raw
                final_path = os.path.join(out_dir, name)

            os.makedirs(os.path.dirname(final_path) or ".", exist_ok=True)
            with open(final_path, "wb") as f:
                f.write(final_data)
            print(f"  Restored: {final_path}")

    print(f"\n✅ Restore complete → {out_dir}")

if __name__ == "__main__":
    main()
