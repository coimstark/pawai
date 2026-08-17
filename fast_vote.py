#!/usr/bin/env python3
"""Vote cepat: browser HANYA untuk token Turnstile, sisanya HTTP biasa.

Alur per attempt (satu proxy):
  1. HTTP  : GET  /api/voting/config      -> ambil turnstileSiteKey
  2. HTTP  : POST /api/voting/device      -> ambil cookie voting_device
  3. Browser: buka halaman -> render widget Turnstile -> ambil token
             (tanpa isi form / klik tombol; browser ditutup setelah token)
  4. HTTP  : POST /api/votes              -> submit vote (cookie + token)

Kenapa browser tetap diperlukan? Token Turnstile hanya bisa didapat dari
widget JS Cloudflare yang berjalan di lingkungan browser — tidak bisa
dipalsukan lewat request biasa. Sisanya murni HTTP.

Usage:
  .venv/bin/python fast_vote.py --proxy http://IP:PORT
  .venv/bin/python fast_vote.py --proxy http://IP:PORT --headless
  .venv/bin/python fast_vote.py --no-proxy
"""
import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
import uuid

from camoufox.sync_api import Camoufox

import auto_test  # faker, mark_used, _record_success, dll.

BASE = "https://pawainusantara.vercel.app"
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
      "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36")


def parse_args():
    p = argparse.ArgumentParser(description="Vote cepat (token Turnstile via browser)")
    p.add_argument("--proxy", default=None, help="mis. http://IP:PORT")
    p.add_argument("--no-proxy", action="store_true", help="tanpa proxy (IP lokal)")
    p.add_argument("--url", default=BASE + "/mobil-06")
    p.add_argument("--slug", default="mobil-06")
    p.add_argument("--participant-id", type=int, default=None,
                   help="default: diparse dari halaman")
    p.add_argument("--name", default=None, help="default: acak via Faker")
    p.add_argument("--phone", default=None, help="default: acak via Faker")
    p.add_argument("--headless", action="store_true",
                   help="browser headless (default: False)")
    p.add_argument("--token-timeout", type=int, default=40,
                   help="timeout tunggu token (detik)")
    p.add_argument("--started-ago", type=int, default=45,
                   help="startedAt mundur N detik (server tolak form terlalu cepat)")
    p.add_argument("--no-record", action="store_true")
    p.add_argument("--no-used", action="store_true")
    return p.parse_args()


def http(proxy, url, method="GET", headers=None, data=None, timeout=30):
    """Request HTTP biasa (lewat proxy bila ada); return (status, headers, body)."""
    h = dict(headers or {})
    h.setdefault("User-Agent", UA)
    h.setdefault("Accept", "*/*")
    req = urllib.request.Request(url, data=data, headers=h, method=method)
    if proxy:
        opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
    else:
        opener = urllib.request.build_opener()
    try:
        with opener.open(req, timeout=timeout) as r:
            return r.status, dict(r.headers), r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read().decode("utf-8", "replace")
    except Exception as e:
        return 0, {}, f"{type(e).__name__}: {e}"


def get_sitekey(proxy):
    status, _, body = http(proxy, BASE + "/api/voting/config")
    if status != 200:
        return None, f"config HTTP {status}: {body[:120]}"
    try:
        return json.loads(body)["turnstileSiteKey"], None
    except Exception as e:
        return None, f"config parse gagal: {e}"


def register_device(proxy):
    """POST /api/voting/device -> cookie 'voting_device=...'"""
    status, headers, body = http(
        proxy, BASE + "/api/voting/device", method="POST",
        headers={"Origin": BASE, "Referer": BASE + "/", "Content-Length": "0"})
    if status != 200:
        return None, f"device HTTP {status}: {body[:120]}"
    set_cookie = headers.get("Set-Cookie", "")
    m = re.search(r"voting_device=[^;]+", set_cookie)
    if not m:
        return None, f"device tanpa Set-Cookie: {set_cookie[:120]}"
    return m.group(0), None


def get_turnstile_token(proxy, sitekey, args):
    """Browser seminimal mungkin: render widget Turnstile, ambil token."""
    launch = dict(disable_coop=True, headless=args.headless,
                  humanize=True, i_know_what_im_doing=True)
    if proxy:
        launch["proxy"] = {"server": proxy}
        launch["geoip"] = True
    with Camoufox(**launch) as browser:
        page = browser.new_page()
        page.set_default_timeout(15000)
        page.goto(args.url, wait_until="domcontentloaded", timeout=40000)

        # Tunggu api.js Turnstile termuat (dimuat halaman setelah hydration)
        for _ in range(int(args.token_timeout * 2)):
            if page.evaluate("typeof window.turnstile !== 'undefined'"):
                break
            page.wait_for_timeout(500)

        # Parse participantId dari halaman bila belum diketahui
        participant_id = args.participant_id
        if participant_id is None:
            m = re.search(r'"id":(\d+),"slug":"%s"' % re.escape(args.slug),
                          page.content())
            if m:
                participant_id = int(m.group(1))

        # Inject widget Turnstile sendiri (hostname halaman = domain sitekey)
        page.evaluate("""
            window.__token = null;
            (function(){
                var d = document.createElement('div');
                d.id = 'fast-tst';
                d.style.width = '300px';
                d.style.height = '65px';
                document.body.appendChild(d);
                turnstile.render(d, {
                    sitekey: %s,
                    callback: function(t){ window.__token = t; }
                });
            })();
        """ % json.dumps(sitekey))

        # Klik checkbox bila widget interaktif (disable_coop memungkinkan)
        widget = page.locator("#fast-tst iframe").first
        token = None
        for _ in range(int(args.token_timeout * 2)):
            token = page.evaluate("window.__token")
            if token:
                break
            try:
                if widget.count() and widget.is_visible():
                    frame = widget.content_frame
                    if frame is not None:
                        cb = frame.locator(
                            '.ctp-checkbox-label, input[type="checkbox"], '
                            '[role="checkbox"]').first
                        if cb.count() and cb.is_visible():
                            cb.click(timeout=3000)
            except Exception:
                pass
            page.wait_for_timeout(500)

        if not token:
            return None, None, "token tidak keluar (challenge interaktif / diblokir)"
        return token, participant_id, None


def submit_vote(proxy, cookie, token, participant_id, name, phone, args):
    payload = {
        "requestId": str(uuid.uuid4()),
        "participantId": participant_id,
        "name": name,
        "phone": phone,
        "source": args.slug,
        "website": "",
        "startedAt": int((time.time() - args.started_ago) * 1000),
        "turnstileToken": token,
        "testMode": False,
    }
    status, _, body = http(
        proxy, BASE + "/api/votes", method="POST",
        headers={"Content-Type": "application/json", "Cookie": cookie,
                 "Origin": BASE, "Referer": args.url},
        data=json.dumps(payload).encode(), timeout=30)
    return status, body


def classify(status, body):
    try:
        j = json.loads(body) if body.startswith("{") else {}
    except Exception:
        j = {}
    if status in (200, 201):
        return "success", body
    if status == 409:
        msg = j.get("error", body)
        return ("duplicate_ip" if "IP" in msg else "duplicate_phone"), msg
    if status == 403:
        return "turnstile", j.get("error", body)
    return "error", f"HTTP {status}: {body[:150]}"


def main():
    args = parse_args()
    proxy = None if args.no_proxy else args.proxy
    name = args.name or auto_test.random_name()
    phone = args.phone or auto_test.random_phone()
    t0 = time.time()
    try:
        return _main(args, proxy, name, phone, t0)
    except Exception as e:
        print(f"RESULT:error|{type(e).__name__}: {str(e)[:200]}")
        if proxy:
            auto_test.mark_used(proxy, "error")
        return 1


def _main(args, proxy, name, phone, t0):
    """Badan utama; dibungkus try/except agar RESULT selalu terprint."""

    # 1. Config -> sitekey
    sitekey, err = get_sitekey(proxy)
    if err:
        print(f"RESULT:connect_error|{err}")
        return 1

    # 2. Device -> cookie
    cookie, err = register_device(proxy)
    if err:
        print(f"RESULT:connect_error|{err}")
        return 1

    # 3. Browser -> token
    token, participant_id, err = get_turnstile_token(proxy, sitekey, args)
    if err or not token:
        verdict, detail = "turnstile", err or "token kosong"
        print(f"RESULT:{verdict}|{detail}")
        if proxy:
            auto_test.mark_used(proxy, verdict)
        return 1
    if participant_id is None:
        participant_id = args.participant_id or 6

    # 4. Submit vote (HTTP biasa)
    status, body = submit_vote(proxy, cookie, token, participant_id,
                               name, phone, args)
    verdict, detail = classify(status, body)

    print(f"RESULT:{verdict}|{detail}")
    print(f"  proxy={proxy} name={name} phone={phone} "
          f"participantId={participant_id} [{time.time()-t0:.0f}s]")
    if proxy:
        auto_test.mark_used(proxy, verdict)
    if verdict == "success" and not args.no_record:
        auto_test._record_success(proxy, name, phone)
    return 0 if verdict == "success" else 1


if __name__ == "__main__":
    sys.exit(main())
