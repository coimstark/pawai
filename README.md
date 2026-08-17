# Auto-test Voting pawainusantara.vercel.app

Auto-test flow voting dengan Camoufox (browser stealth Firefox + Playwright)
dan rotasi proxy gratis dari [proxifly/free-proxy-list](https://github.com/proxifly/free-proxy-list)
(daftar dirotasi ~setiap 5 menit — tiap fetch dapat IP baru).

## Arsitektur (fast path)

**Browser hanya dipakai untuk mendapatkan token Turnstile** — submit-nya
murni HTTP biasa:

```
1. HTTP    GET  /api/voting/config      -> turnstileSiteKey
2. HTTP    POST /api/voting/device      -> cookie voting_device
3. Browser      buka halaman -> render widget Turnstile -> ambil token
               (tanpa isi form / klik; ditutup setelah token)
4. HTTP    POST /api/votes              -> submit vote (cookie + token)
```

Token Turnstile **hanya** bisa didapat dari widget JS Cloudflare (sekali
pakai, berlaku 300 detik) — itulah satu-satunya alasan browser tetap
dipakai. Semua yang lain (device, submit) cukup request biasa.

## Script

| File | Fungsi |
|------|--------|
| `fast_vote.py` | Satu attempt cepat: token via browser + submit HTTP biasa |
| `mass_vote.py` | **Mass voting paralel & kontinu** — N worker subproses `fast_vote.py`, loop tak terbatas, re-fetch daftar proxy tiap siklus |
| `auto_test.py` | Flow GUI penuh (fallback/debug): isi form, klik, dll. |
| `run_rotation.py` | Rotasi sekuensial satu-per-satu (mode lama) |

## Cara pakai

```bash
# Setup
python3 -m venv .venv
.venv/bin/pip install "camoufox[geoip]" faker
.venv/bin/camoufox fetch

# Mass voting paralel & kontinu (default: headless, loop tak terbatas)
.venv/bin/python mass_vote.py --workers 6 --max-proxies 40

# Satu attempt cepat
.venv/bin/python fast_vote.py --proxy http://IP:PORT
```

Opsi penting `mass_vote.py`:
- `--workers N` — jumlah browser paralel
- `--cycles N` — batas siklus (0 = tak terbatas)
- `--cycle-gap N` — jeda antar siklus (detik), biarkan daftar proxy refresh
- `--precheck-target N` — hentikan pre-filter setelah N proxy hidup
  (efisien; mis. `--workers` × 2)
- `--headless` / `--name X` / `--phone Y` (default: Faker acak)

## Penyimpanan status (persisten)

- `used_proxies.json` — **proxy yang sudah dicek/dipakai**; di-skip pada
  fetch berikutnya. Karena daftar proxifly berotasi tiap 5 menit, proxy
  baru selalu diproses tanpa mengulang yang lama.
- `results.json` — vote sukses (timestamp, proxy, nama, HP) untuk
  identifikasi & pembersihan record test.
- `working_proxies.json` — cache proxy hidup hasil pre-filter (TTL 3 menit).

## Verdict

- `success` — `201 {"success":true}` (vote tercatat)
- `duplicate_ip` / `duplicate_phone` — `409` (proteksi anti-duplikat; IP/HP sudah vote)
- `turnstile` — token ditolak / challenge interaktif tidak bisa diselesaikan
  (nasib mayoritas IP datacenter — lotere reputasi Cloudflare)
- `connect_error` — proxy mati
- `timeout` — attempt melebihi batas waktu

## Temuan menarik (server side)

- `409` memakai kode `ip_already_voted` (IP) dan `duplicate_vote` (HP).
- Ada check anti-bot `400 "Form dikirim terlalu cepat"` — `startedAt` di
  payload harus mundur beberapa detik dari waktu submit (di-handle otomatis
  via `--started-ago`, default 45 detik).
- Halaman punya plumbing test mode (`?mode=test` → `testModeRequested: true`)
  tapi server tetap `testMode: false` di deployment ini.

## Catatan

- Vote yang berhasil adalah vote sungguhan — hapus dari database bila
  hanya untuk uji coba (lihat `results.json`).
- Proxy gratis cepat mati; pre-filter curl (cek situs + host Turnstile)
  dijalankan tiap siklus, dan hanya proxy yang benar-benar di-scan yang
  ditandai "checked".
