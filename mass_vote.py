#!/usr/bin/env python3
"""Mass voting paralel & kontinu untuk auto-test pawainusantara.

- Menggunakan Camoufox + proxy gratis proxifly/free-proxy-list.
- Daftar proxy proxifly dirotasi ~setiap 5 menit; tool ini berjalan terus
  menerus dan mengambil daftar terbaru setiap siklus.
- Tiap worker = 1 subproses auto_test.py dengan 1 proxy (isolasi penuh,
  menghindari korupsi event loop Playwright antar percobaan).

Alur tiap siklus:
  1. Fetch daftar proxy proxifly (prioritas negara ID)
  2. Pre-filter curl paralel -> proxy hidup
  3. Jalankan N worker browser paralel, satu vote per proxy
  4. Ulangi siklus dengan daftar proxy segar

Usage:
  .venv/bin/python mass_vote.py --workers 6 --max-proxies 40
  .venv/bin/python mass_vote.py --cycles 2 --workers 8 --headless
  .venv/bin/python mass_vote.py --max-proxies 20 --cycle-gap 60
"""
import argparse
import json
import os
import re
import signal
import subprocess
import sys
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed

import auto_test

HERE = os.path.dirname(os.path.abspath(__file__))


def parse_args():
    p = argparse.ArgumentParser(
        description="Mass voting paralel & kontinu (Camoufox + proxifly)")
    p.add_argument("--workers", type=int, default=5,
                   help="jumlah worker browser paralel")
    p.add_argument("--cycles", type=int, default=0,
                   help="jumlah siklus (0 = tak terbatas)")
    p.add_argument("--cycle-gap", type=int, default=10,
                   help="jeda antar siklus (detik), biarkan daftar proxy "
                        "refresh")
    p.add_argument("--max-proxies", type=int, default=40,
                   help="jumlah proxy dicoba per siklus")
    p.add_argument("--precheck-limit", type=int, default=0,
                   help="0 = scan semua proxy saat pre-filter")
    p.add_argument("--precheck-target", type=int, default=0,
                   help="berhenti pre-filter setelah N proxy hidup "
                        "(0 = semua; efisien: workers*2)")
    p.add_argument("--precheck-workers", type=int, default=40,
                   help="paralelisme pre-filter curl")
    p.add_argument("--no-used", action="store_true",
                   help="abaikan catatan proxy yang sudah dicek/dipakai")
    p.add_argument("--attempt-timeout", type=int, default=90,
                   help="timeout satu attempt (detik)")
    p.add_argument("--name", default=None,
                   help="nama pemilih; default: acak via Faker")
    p.add_argument("--phone", default=None,
                   help="nomor HP; default: acak via Faker")
    p.add_argument("--headless", action="store_true",
                   help="jalankan browser headless (lebih cepat)")
    p.add_argument("--no-record", action="store_true",
                   help="jangan catat hasil sukses ke results.json")
    p.add_argument("--prefer-country", default="ID")
    p.add_argument("--sources", default=None,
                   help="sumber proxy: proxifly,monosans,hideip "
                        "(default: semua)")
    return p.parse_args()


def run_one(proxy, args):
    """Satu vote via satu proxy, di subproses terpisah.
    Pakai fast_vote.py: browser hanya untuk token Turnstile, submit via
    HTTP biasa (jauh lebih cepat dari flow GUI penuh)."""
    cmd = [sys.executable, os.path.join(HERE, "fast_vote.py"),
           "--proxy", proxy]
    if args.headless:
        cmd.append("--headless")
    if args.name:
        cmd += ["--name", args.name]
    if args.phone:
        cmd += ["--phone", args.phone]
    if args.no_record:
        cmd.append("--no-record")

    p = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                         stderr=subprocess.STDOUT, text=True,
                         cwd=HERE, start_new_session=True)
    try:
        out, _ = p.communicate(timeout=args.attempt_timeout)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(p.pid, signal.SIGKILL)
        except Exception:
            pass
        p.communicate()
        return {"proxy": proxy, "verdict": "timeout",
                "detail": "attempt melebihi batas waktu"}
    m = re.search(r"RESULT:(\w+)\|(.*)", out)
    if m:
        return {"proxy": proxy, "verdict": m.group(1),
                "detail": m.group(2)[:200]}
    return {"proxy": proxy, "verdict": "unknown",
            "detail": "RESULT line tidak ditemukan"}


def main():
    args = parse_args()
    total = Counter()
    t_start = time.time()
    cycle = 0

    print("Mass voting dimulai. Ctrl+C untuk berhenti.\n")
    try:
        while args.cycles == 0 or cycle < args.cycles:
            cycle += 1
            print(f"{'=' * 64}\nSiklus {cycle} — {time.strftime('%H:%M:%S')}")

            # 1. Fetch daftar proxy terbaru (proxifly rotasi ~5 menit);
            #    buang proxy yang sudah dicek/dipakai (used_proxies.json)
            used = set() if args.no_used else auto_test.load_used_proxies()
            src_list = (args.sources.split(",") if args.sources else None)
            entries = auto_test.fetch_all_proxies(
                args.prefer_country, exclude=used, sources=src_list)
            print(f"  Proxy baru tersedia: {len(entries)} "
                  f"(sudah buang {len(used)} yang dicek/dipakai)")

            # 2. Pre-filter
            working, checked = auto_test.precheck_proxies(
                entries, args.precheck_limit, args.precheck_workers,
                target=args.precheck_target)
            # Tandai hanya yang benar-benar di-scan agar tidak dicek ulang
            for p in checked:
                auto_test.mark_used(p, "checked")
            if not working:
                print(f"  Tidak ada proxy hidup baru; tunggu {args.cycle_gap}s...")
                time.sleep(args.cycle_gap)
                continue
            working = working[:args.max_proxies]
            print(f"  {len(working)} proxy hidup -> dispatch ke "
                  f"{args.workers} worker paralel")

            # 3. Submit paralel
            cycle_stats = Counter()
            with ThreadPoolExecutor(max_workers=args.workers) as ex:
                futures = {ex.submit(run_one, proxy, args): proxy
                           for proxy in working}
                for fut in as_completed(futures):
                    res = fut.result()
                    cycle_stats[res["verdict"]] += 1
                    total[res["verdict"]] += 1
                    elapsed_min = (time.time() - t_start) / 60
                    rate = total["success"] / elapsed_min if elapsed_min else 0
                    mark = "✓" if res["verdict"] == "success" else " "
                    print(f"  [{mark}] {time.strftime('%H:%M:%S')} "
                          f"{res['proxy']} -> {res['verdict']} | "
                          f"sukses: {total['success']} "
                          f"({rate:.1f}/menit)")
                    if res["verdict"] == "success":
                        print(f"       {res['detail'][:120]}")

            print(f"  Siklus {cycle} selesai: {dict(cycle_stats)}")
            print(f"  Total sementara: {dict(total)}")
            time.sleep(args.cycle_gap)

    except KeyboardInterrupt:
        print("\n[!] Dihentikan pengguna.")

    elapsed_min = (time.time() - t_start) / 60
    print(f"\n{'=' * 64}\n=== STATISTIK AKHIR "
          f"({elapsed_min:.1f} menit) ===")
    for k, v in total.most_common():
        print(f"  {k}: {v}")
    if total["success"]:
        print(f"  Rata-rata: {total['success'] / elapsed_min:.1f} vote/menit")
    return 0 if total["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
