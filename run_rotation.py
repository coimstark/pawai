#!/usr/bin/env python3
"""Driver rotasi proxy untuk auto-test voting pawainusantara.

Alur:
  1. Ambil daftar proxy dari proxifly/free-proxy-list (prioritas negara ID)
  2. Pre-filter dengan curl (paralel) -> proxy hidup
  3. Untuk tiap proxy hidup: jalankan auto_test.py sebagai proses terpisah
     (isolasi penuh, menghindari korupsi event loop Playwright)
  4. Berhenti saat ada vote sukses

Usage:
  .venv/bin/python run_rotation.py [--max-proxies N] [--name X]
"""
import argparse
import os
import re
import subprocess
import sys
import time

import auto_test

HERE = os.path.dirname(os.path.abspath(__file__))


def parse_args():
    p = argparse.ArgumentParser(description="Rotasi proxy untuk auto-test voting")
    p.add_argument("--name", default=None,
                   help="nama pemilih; default: acak via Faker per attempt")
    p.add_argument("--max-proxies", type=int, default=20,
                   help="jumlah proxy hidup maksimal dicoba")
    p.add_argument("--precheck-limit", type=int, default=0,
                   help="0 = scan semua proxy")
    p.add_argument("--attempt-timeout", type=int, default=150,
                   help="timeout per attempt (detik)")
    p.add_argument("--headless", action="store_true")
    return p.parse_args()


def main():
    args = parse_args()
    used = auto_test.load_used_proxies()
    entries = auto_test.fetch_all_proxies("ID", exclude=used)
    print(f"Proxy baru tersedia: {len(entries)} "
          f"(sudah buang {len(used)} yang dicek/dipakai)")
    working, checked = auto_test.precheck_proxies(entries,
                                                  args.precheck_limit)
    for p in checked:
        auto_test.mark_used(p, "checked")
    if not working:
        print("Tidak ada proxy hidup baru. Coba lagi nanti.")
        return 1

    working = working[: args.max_proxies]
    print(f"\n[*] {len(working)} proxy hidup akan dicoba satu per satu...")

    t0 = time.time()
    for i, proxy in enumerate(working, 1):
        print(f"\n=== Attempt {i}/{len(working)}: {proxy} ===")
        cmd = [sys.executable, os.path.join(HERE, "auto_test.py"),
               "--proxy", proxy]
        if args.name:
            cmd += ["--name", args.name]
        if args.headless:
            cmd.append("--headless")
        try:
            r = subprocess.run(cmd, capture_output=True, text=True,
                               timeout=args.attempt_timeout, cwd=HERE)
            out = r.stdout + r.stderr
        except subprocess.TimeoutExpired:
            print("    [timeout] attempt melebihi batas waktu")
            continue

        # Parsing verdict
        m = re.search(r"verdict: (\w+)", out)
        verdict = m.group(1) if m else "unknown"
        print(f"    -> verdict: {verdict} [{time.time()-t0:.0f}s total]")

        if r.returncode == 0 or "BERHASIL" in out:
            print("\n=== BERHASIL! ===")
            print(proxy)
            for line in out.splitlines():
                if "BERHASIL" in line or "/api/votes" in line:
                    print("   ", line.strip())
            return 0

    print(f"\n=== GAGAL: {len(working)} proxy dicoba, tidak ada vote sukses. ===")
    return 1


if __name__ == "__main__":
    sys.exit(main())
