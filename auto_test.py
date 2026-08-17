#!/usr/bin/env python3
"""Auto-test flow voting pawainusantara.vercel.app menggunakan Camoufox.

Mendukung rotasi proxy gratis dari proxifly/free-proxy-list agar tiap
percobaan memakai IP yang berbeda (menghindari proteksi duplicate IP).

Flow per percobaan:
  1. GET  /api/voting/config      -> voting open?
  2. POST /api/voting/device      -> registrasi device (cookie voting_device)
  3. Turnstile (dirender setelah klik submit)
  4. POST /api/votes              -> hasil vote

Usage:
  python auto_test.py                        # rotasi proxy otomatis
  python auto_test.py --proxy http://IP:PORT # satu proxy tertentu
  python auto_test.py --no-proxy             # tanpa proxy (IP lokal)
  python auto_test.py --phone 081234567890 --name "Budi" --max-proxies 10
"""
import argparse
import fcntl
import json
import os
import random
import re
import subprocess
import sys
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor

from camoufox.sync_api import Camoufox
from faker import Faker

API_RE = re.compile(r"/api/(voting/config|voting/device|votes|voting/status)")
PROXY_LIST_URL = (
    "https://cdn.jsdelivr.net/gh/proxifly/free-proxy-list@main/"
    "proxies/protocols/http/data.json"
)
# Sumber proxy tambahan (semua via jsdelivr CDN agar bebas rate limit)
PROXY_SOURCES = {
    "proxifly": {"url": PROXY_LIST_URL, "format": "json"},
    "monosans": {
        "url": "https://cdn.jsdelivr.net/gh/monosans/proxy-list@main/"
               "proxies/http.txt",
        "format": "txt",
    },
    "hideip": {
        "url": "https://cdn.jsdelivr.net/gh/zloi-user/hideip.me@main/http.txt",
        "format": "txt_country",
    },
}
CACHE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                          "working_proxies.json")
RESULTS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "results.json")
USED_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                         "used_proxies.json")
CACHE_TTL = 3 * 60  # detik (proxy gratis cepat mati)


def parse_args():
    p = argparse.ArgumentParser(description="Auto-test voting flow pawainusantara")
    p.add_argument("--url", default="https://pawainusantara.vercel.app/mobil-06")
    p.add_argument("--name", default=None,
                   help="nama pemilih; default: acak via Faker per percobaan")
    p.add_argument("--phone", default=None,
                   help="nomor HP; default: acak via Faker per percobaan")
    p.add_argument("--headless", action="store_true")
    p.add_argument("--no-proxy", action="store_true",
                   help="pakai IP lokal (tanpa proxy)")
    p.add_argument("--proxy", default=None,
                   help="pakai satu proxy, mis. http://IP:PORT")
    p.add_argument("--proxy-list", default=None,
                   help="URL daftar proxy JSON khusus (override semua sumber)")
    p.add_argument("--sources", default=None,
                   help="sumber proxy: proxifly,monosans,hideip "
                        "(default: semua)")
    p.add_argument("--max-proxies", type=int, default=15,
                   help="jumlah proxy maksimal dicoba saat rotasi")
    p.add_argument("--precheck-limit", type=int, default=0,
                   help="jumlah proxy yang di-scan saat pre-filter (0=semua)")
    p.add_argument("--precheck-target", type=int, default=0,
                   help="berhenti pre-filter setelah N proxy hidup (0=semua)")
    p.add_argument("--no-precheck", action="store_true",
                   help="lewati pre-filter curl (langsung coba browser)")
    p.add_argument("--no-used", action="store_true",
                   help="abaikan catatan proxy yang sudah dicek/dipakai")
    p.add_argument("--prefer-country", default="ID",
                   help="negara proxy yang diprioritaskan")
    p.add_argument("--attempt-timeout", type=int, default=75,
                   help="timeout per percobaan (detik)")
    p.add_argument("--shots", default="shots")
    p.add_argument("--no-shots", action="store_true",
                   help="lewati screenshot (lebih cepat)")
    p.add_argument("--no-record", action="store_true",
                   help="jangan catat hasil sukses ke results.json")
    return p.parse_args()


_fake = Faker("id_ID")


def random_name():
    """Nama orang Indonesia acak via Faker."""
    return _fake.name()


def random_phone():
    """Nomor HP Indonesia acak via Faker: 08 + 10 digit."""
    return _fake.numerify("08##########")


def fetch_proxies(url, prefer_country, exclude=None):
    """Ambil daftar proxy dari proxifly (JSON), urutkan: negara favorit dulu.
    Proksi yang ada di `exclude` (set "ip:port") dibuang — sudah dicek/dipakai."""
    req = urllib.request.Request(url, headers={"User-Agent": "auto-test"})
    with urllib.request.urlopen(req, timeout=30) as r:
        data = json.loads(r.read().decode("utf-8"))
    if not isinstance(data, list) or not data:
        raise RuntimeError("Daftar proxy kosong atau format tidak dikenal")

    data = _sort_shuffle(data, prefer_country)
    if exclude:
        data = [p for p in data if p["proxy"] not in exclude]
    return data


def _sort_shuffle(data, prefer_country):
    def sort_key(p):
        country = p.get("geolocation", {}).get("country", "")
        return (
            0 if country == prefer_country else 1,
            0 if p.get("anonymity") == "elite" else 1,
        )

    data.sort(key=sort_key)
    random.shuffle(data)  # variasi dalam kelompok yang setara
    return data


def _parse_txt(text, exclude, protocol, country_col=False, source=""):
    """Parse daftar teks `ip:port[:Country]` menjadi format entry proxifly."""
    entries = []
    seen = set()
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split(":")
        if len(parts) < 2:
            continue
        ip, port = parts[0], parts[1]
        if not re.match(r"^\d{1,3}(\.\d{1,3}){3}$", ip) or not port.isdigit():
            continue
        country = parts[2] if country_col and len(parts) > 2 else "unknown"
        proxy = f"{protocol}://{ip}:{port}"
        if proxy in exclude or proxy in seen:
            continue
        seen.add(proxy)
        entries.append({
            "proxy": proxy, "ip": ip, "port": int(port),
            "protocol": protocol, "anonymity": "unknown", "https": True,
            "geolocation": {"country": country, "city": "Unknown"},
            "source": source,
        })
    return entries


def fetch_all_proxies(prefer_country, exclude=None, sources=None):
    """Gabungkan semua sumber proxy (proxifly, monosans, hideip), dedup.
    Satu sumber gagal tidak menggagalkan yang lain."""
    exclude = exclude or set()
    all_entries = []
    seen = set()
    for name in (sources or list(PROXY_SOURCES)):
        spec = PROXY_SOURCES[name]
        try:
            req = urllib.request.Request(spec["url"],
                                         headers={"User-Agent": "auto-test"})
            with urllib.request.urlopen(req, timeout=30) as r:
                text = r.read().decode("utf-8", "replace")
            if spec["format"] == "json":
                data = json.loads(text)
                new = []
                for p in data:
                    if p["proxy"] in exclude or p["proxy"] in seen:
                        continue
                    p.setdefault("source", name)
                    new.append(p)
            else:
                new = _parse_txt(text, exclude | seen, "http",
                                 country_col=spec["format"] == "txt_country",
                                 source=name)
            for p in new:
                seen.add(p["proxy"])
            all_entries.extend(new)
            print(f"    + {name}: {len(new)} proxy baru "
                  f"(total {len(all_entries)})")
        except Exception as e:
            print(f"    ! {name}: gagal ({type(e).__name__}: {e})")
    return _sort_shuffle(all_entries, prefer_country)


def load_used_proxies():
    """Set proxy yang sudah dicek/dipakai (persisten di used_proxies.json)."""
    try:
        with open(USED_FILE) as f:
            return set(json.load(f))
    except Exception:
        return set()


def mark_used(proxy, status):
    """Tandai proxy sebagai sudah dicek/dipakai (aman untuk akses paralel)."""
    try:
        lock_path = USED_FILE + ".lock"
        with open(lock_path, "w") as lf:
            fcntl.flock(lf, fcntl.LOCK_EX)
            try:
                used = load_used_proxies()
                used.add(proxy)
                tmp = USED_FILE + ".tmp"
                with open(tmp, "w") as f:
                    json.dump(sorted(used), f)
                os.replace(tmp, USED_FILE)
            finally:
                fcntl.flock(lf, fcntl.LOCK_UN)
    except Exception as e:
        print(f"    (gagal tandai proxy: {e})")


PRECHECK_TARGETS = [
    "https://pawainusantara.vercel.app/api/voting/config",
    "https://challenges.cloudflare.com/turnstile/v0/api.js",
]


def _curl_check(proxy_url, timeout=8):
    """Cek proxy: harus bisa akses situs DAN host Turnstile (hindari stuck)."""
    for url in PRECHECK_TARGETS:
        try:
            r = subprocess.run(
                ["curl", "-sSL", "-o", "/dev/null", "-w", "%{http_code}",
                 "-x", proxy_url, "--connect-timeout", "3",
                 "--max-time", str(timeout), url],
                capture_output=True, text=True, timeout=timeout + 3)
            code = r.stdout.strip()
            if not code.startswith(("2", "3")):
                return False
        except Exception:
            return False
    return True


def precheck_proxies(entries, limit, workers=25, target=0):
    """Pre-filter proxy dengan curl paralel; kembalikan yang hidup.
    Berhenti lebih awal begitu `target` proxy hidup terkumpul (0 = semua).
    Return: (working, checked) — `checked` = set proxy yang benar-benar
    di-scan (penting untuk penandaan "sudah dicek" yang akurat)."""
    if limit:
        entries = entries[:limit]
    print(f"[*] Pre-filter {len(entries)} proxy dengan curl "
          f"({workers} paralel)...")
    working = []
    checked = set()
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futures = {ex.submit(_curl_check, p["proxy"]): p for p in entries}
        done = 0
        for f in futures:
            if f.result():
                working.append(futures[f]["proxy"])
                if target and len(working) >= target:
                    for other in futures:
                        if not other.done():
                            other.cancel()
                    checked.add(futures[f]["proxy"])
                    break
            checked.add(futures[f]["proxy"])
            done += 1
            if done % 50 == 0:
                print(f"    ...{done}/{len(entries)} dicek, "
                      f"{len(working)} hidup")
    print(f"    -> {len(working)}/{len(entries)} proxy hidup "
          f"({len(checked)} di-scan)")
    return working, checked


def load_cache():
    try:
        with open(CACHE_FILE) as f:
            c = json.load(f)
        if time.time() - c.get("ts", 0) < CACHE_TTL and c.get("proxies"):
            return c["proxies"]
    except Exception:
        pass
    return None


def save_cache(proxies):
    try:
        with open(CACHE_FILE, "w") as f:
            json.dump({"ts": time.time(), "proxies": proxies}, f)
    except Exception:
        pass


def _record_success(proxy, name, phone):
    """Catat vote sukses (untuk identifikasi & pembersihan record test).
    Aman untuk akses paralel (file lock + tulis atomic)."""
    try:
        lock_path = RESULTS_FILE + ".lock"
        with open(lock_path, "w") as lf:
            fcntl.flock(lf, fcntl.LOCK_EX)
            try:
                records = []
                if os.path.exists(RESULTS_FILE):
                    with open(RESULTS_FILE) as f:
                        records = json.load(f)
                records.append({"ts": time.strftime("%Y-%m-%d %H:%M:%S"),
                                "proxy": proxy, "name": name, "phone": phone})
                tmp = RESULTS_FILE + ".tmp"
                with open(tmp, "w") as f:
                    json.dump(records, f, ensure_ascii=False, indent=2)
                os.replace(tmp, RESULTS_FILE)
            finally:
                fcntl.flock(lf, fcntl.LOCK_UN)
        print(f"    tercatat di {RESULTS_FILE}")
    except Exception as e:
        print(f"    (gagal catat hasil: {e})")


def classify(api_log):
    """Klasifikasi hasil percobaan dari log API."""
    votes = [e for e in api_log if e["url"].endswith("/api/votes")]
    if not votes:
        return ("no_vote", "tidak ada request /api/votes")
    last = votes[-1]
    status, body = last["status"], last.get("body", "")
    try:
        j = json.loads(body) if body.startswith("{") else {}
    except Exception:
        j = {}
    code = j.get("code", "")
    if status in (200, 201):
        return ("success", body)
    if status == 409:
        msg = j.get("error", body)
        if "IP" in msg:
            return ("duplicate_ip", msg)
        return ("duplicate_phone", msg)
    if status == 403:
        return ("turnstile", j.get("error", body))
    return ("error", f"HTTP {status} {body}")


def run_attempt(launch_kwargs, args, name, phone, shot_tag):
    """Satu percobaan penuh; return (verdict, api_log)."""
    api_log = []
    with Camoufox(**launch_kwargs) as browser:
        page = browser.new_page()
        page.set_default_timeout(15000)

        def on_console(msg):
            if msg.type == "error" and msg.text.strip() not in ("0", ""):
                print(f"      [console.error] {msg.text[:200]}")

        page.on("console", on_console)
        page.on("pageerror", lambda err: print(f"      [pageerror] {err}"))

        def on_response(resp):
            if API_RE.search(resp.url):
                body = ""
                try:
                    body = resp.text()
                except Exception:
                    body = "<no body>"
                api_log.append({"method": resp.request.method,
                                "url": resp.url, "status": resp.status,
                                "body": body[:500]})
                print(f"      [api] {resp.request.method} {resp.url} -> "
                      f"{resp.status} {body[:160]}")

        page.on("response", on_response)

        page.goto(args.url, wait_until="domcontentloaded", timeout=30000)
        page.wait_for_timeout(1500)

        # Form — isi dengan verifikasi & retry (React bisa reset input jika
        # diisi sebelum hydration selesai, terutama lewat proxy lambat)
        name_input = page.locator('input[name="voter_name"]')
        phone_input = page.locator('input[name="voter_phone"]')
        name_input.wait_for(state="visible", timeout=30000)
        for _ in range(5):
            name_input.fill(name)
            phone_input.fill(phone)
            page.evaluate(
                "document.querySelector('input[name=contact_url_confirm]').value=''")
            page.wait_for_timeout(700)
            if (name_input.input_value() == name
                    and phone_input.input_value() == phone):
                break

        btn = page.locator("form button.primary")
        btn.wait_for(state="visible", timeout=30000)
        # Tombol baru aktif setelah inisialisasi device + Turnstile selesai
        enabled = False
        for _ in range(50):
            if not btn.is_disabled():
                enabled = True
                break
            page.wait_for_timeout(500)
        # Pastikan input tetap terisi tepat sebelum klik (React re-render
        # bisa mengosongkan jika hydration terlambat)
        if name_input.input_value() != name or phone_input.input_value() != phone:
            name_input.fill(name)
            phone_input.fill(phone)
            page.wait_for_timeout(800)
        if not args.no_shots:
            page.screenshot(path=f"{args.shots}/{shot_tag}-filled.png")
        if not enabled:
            return ("stuck",
                    f"tombol tidak aktif: {(btn.inner_text() or '')[:80]}"), api_log
        try:
            btn.click(timeout=20000)
        except Exception:
            # Fallback: klik via JS (hindari actionability check yang bisa
            # gagal di proxy lambat); event tetap memicu onSubmit React
            print("      fallback: klik via JS")
            page.evaluate(
                "document.querySelector('form button.primary').click()")

        # Tunggu Turnstile iframe (dirender setelah klik submit)
        ts = page.locator('iframe[src*="challenges.cloudflare.com"]')
        for _ in range(16):
            if ts.count() > 0:
                break
            page.wait_for_timeout(500)
        if ts.count() > 0:
            for i in range(ts.count()):
                frame = ts.nth(i).content_frame
                if frame is None:
                    continue
                cb = frame.locator(
                    '.ctp-checkbox-label, input[type="checkbox"], '
                    '[role="checkbox"]').first
                if cb.count() and cb.is_visible():
                    print("      klik checkbox Turnstile")
                    cb.click(timeout=10000)
                else:
                    box = ts.nth(i).bounding_box()
                    if box:
                        page.mouse.click(box["x"] + box["width"] / 2,
                                         box["y"] + box["height"] / 2)
                break

        # Tunggu hasil
        for _ in range(60):
            if api_log and api_log[-1]["url"].endswith("/api/votes") and \
                    api_log[-1]["status"] not in (0,):
                break
            page.wait_for_timeout(500)
        page.wait_for_timeout(500)
        if not args.no_shots:
            page.screenshot(path=f"{args.shots}/{shot_tag}-final.png",
                            full_page=True)

        return classify(api_log), api_log


def main():
    args = parse_args()
    os.makedirs(args.shots, exist_ok=True)

    if args.no_proxy:
        proxies = [None]
    elif args.proxy:
        proxies = [args.proxy]
    else:
        used = set() if args.no_used else load_used_proxies()
        src_list = (args.sources.split(",") if args.sources else None)
        if args.proxy_list:
            print(f"[*] Ambil daftar proxy dari {args.proxy_list} "
                  f"(exclude {len(used)} sudah dicek/dipakai)...")
            entries = fetch_proxies(args.proxy_list, args.prefer_country,
                                    exclude=used)
        else:
            print(f"[*] Ambil daftar proxy dari semua sumber "
                  f"(exclude {len(used)} sudah dicek/dipakai)...")
            entries = fetch_all_proxies(args.prefer_country, exclude=used,
                                        sources=src_list)
        cached = None if args.no_precheck else load_cache()
        if cached:
            proxies = [p for p in cached if p not in used]
            print(f"    pakai cache {len(proxies)} proxy hidup "
                  f"(dari <3 menit lalu, sudah buang yang terpakai)")
        else:
            proxies, checked = precheck_proxies(
                entries, args.precheck_limit, target=args.precheck_target)
            # Tandai hanya yang benar-benar di-scan agar tidak dicek ulang
            for p in checked:
                mark_used(p, "checked")
            if proxies:
                save_cache(proxies)
        if not proxies:
            print("    (tidak ada proxy hidup; fallback tanpa proxy)")
            proxies = [None]
        else:
            proxies = proxies[:args.max_proxies]
            print(f"    {len(proxies)} proxy hidup siap dicoba "
                  f"(prioritas negara {args.prefer_country})")

    attempts = 0
    for idx, proxy in enumerate(proxies, 1):
        name = args.name or random_name()
        phone = args.phone or random_phone()
        print(f"\n=== Percobaan {idx}/{len(proxies) or 1}: "
              f"proxy={proxy or '(tanpa proxy)'} name={name} "
              f"phone={phone} ===")
        launch = dict(disable_coop=True, headless=args.headless,
                      humanize=True, i_know_what_im_doing=True)
        if proxy:
            launch["proxy"] = {"server": proxy}
            launch["geoip"] = True  # cocokkan fingerprint dgn lokasi proxy
        attempts += 1
        t0 = time.time()
        try:
            (verdict, detail), api_log = run_attempt(
                launch, args, name, phone, f"a{idx:02d}")
        except Exception as e:
            print(f"    [gagal] {type(e).__name__}: {str(e)[:200]}")
            verdict, detail = "connect_error", str(e)[:200]
        elapsed = time.time() - t0
        print(f"    -> verdict: {verdict} ({detail[:160]}) [{elapsed:.0f}s]")
        print(f"RESULT:{verdict}|{detail[:200]}")
        if proxy:
            mark_used(proxy, verdict)

        if verdict == "success":
            print(f"\n=== BERHASIL! proxy={proxy} name={name} phone={phone} ===")
            if not args.no_record:
                _record_success(proxy, name, phone)
            return 0
        notes = {
            "turnstile": "Turnstile menolak proxy ini",
            "duplicate_ip": "IP sudah vote / proxy bocorkan IP asli",
            "stuck": "form tidak siap (Turnstile gagal dimuat)",
            "no_vote": "proxy mati sebelum vote terkirim",
            "connect_error": "koneksi gagal",
            "error": "error lain",
        }
        print(f"    ({notes.get(verdict, verdict)})")
        if not proxies:
            break

    print(f"\n=== GAGAL setelah {attempts} percobaan; tidak ada vote sukses. ===")
    return 1


if __name__ == "__main__":
    sys.exit(main())
