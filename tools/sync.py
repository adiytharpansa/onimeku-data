#!/usr/bin/env python3
"""Sinkron katalog Onimeku (proyek baru) — otomatis & selengkap mungkin.

Alur: API Onimeku LAMA (katalog yang sudah teresolusi penuh oleh
mesin sinkron lama: poster, episode, stream, subtitle sumber)
-> GERBANG KELENGKAPAN -> API Onimeku BARU (POST /api/sync/upsert)
-> cermin jsDelivr (repo onimeku-data: catalog.json + titles/*.json).

Gerbang (aturan user, sama seperti proyek lama): judul hanya
masuk bila poster ada DAN episode berurutan dari 1 tanpa lubang;
judul TAMAT wajib lengkap sampai total episode; yang masih
tayang cukup sampai episode terbaru. Tidak ada data dummy.

Anti-menyusut: catalog.json tidak pernah ditimpa hasil yang
lebih kecil dari yang sudah terbit.

Env:
  OLD_API    alamat API lama   (bawaan: workers.dev my-anime-app)
  NEW_API    alamat API baru   (bawaan: workers.dev onimeku-api)
  SYNC_TOKEN token upsert API baru (wajib, dari secret/file)
  MIRROR_DIR folder cermin untuk catalog.json + titles/
             (bawaan: direktori kerja saat ini)
  FULL_MIRROR=1  sekalian terbitkan cermin PENUH situs lama di
             web/ (SEMUA judul tanpa gerbang: web/catalog.json,
             web/catalog-full.json, web/titles/<id>.json) —
             situs my-anime-app membacanya sebagai lapis
             cadangan di antara D1 dan snapshot KV.
  DRY_RUN=1  hanya laporkan, tanpa POST & tanpa tulis berkas

Dipakai oleh: GitHub Actions (.github/workflows/sync.yml di repo
onimeku-data) tiap 30 menit, dan bisa dijalankan manual dari VM.
"""

import hashlib
import json
import os
import sys
import time
from datetime import datetime, timezone

import requests

OLD_API = os.environ.get("OLD_API", "https://my-anime-app.adiyth-arpansa.workers.dev/api/anime")
NEW_API = os.environ.get("NEW_API", "https://onimeku-api.adiyth-arpansa.workers.dev")
TOKEN = os.environ.get("SYNC_TOKEN", "")
MIRROR_DIR = os.environ.get("MIRROR_DIR", ".")
FULL_MIRROR = os.environ.get("FULL_MIRROR", "") == "1"
DRY_RUN = os.environ.get("DRY_RUN", "") == "1"
UA = {"User-Agent": "OnimekuSync/1.0 (+proyek-baru)"}


def get_json(url, tries=2):
    last = None
    for _ in range(tries):
        try:
            r = requests.get(url, headers=UA, timeout=45)
            if r.ok:
                return r.json()
            last = f"HTTP {r.status_code}"
        except Exception as e:  # jaringan sesaat
            last = str(e)
        time.sleep(2)
    raise RuntimeError(last or "gagal ambil")


def gate(detail):
    """Kembalikan (lolos: bool, alasan: str)."""
    if not (detail.get("poster") or "").strip():
        return False, "tanpa-poster"
    eps = [e for e in detail.get("episodes", []) if e.get("streamUrl")]
    if not eps:
        return False, "tanpa-episode"
    nums = sorted(int(e["number"]) for e in eps)
    if nums != list(range(1, len(nums) + 1)):
        return False, "episode-lubang"
    total = int(detail.get("totalEpisodes") or 0)
    status = (detail.get("status") or "").upper()
    if status == "FINISHED" and total > 0 and len(eps) != total:
        return False, "belum-lengkap"
    return True, "ok"


def payload_of(detail):
    eps = [e for e in detail.get("episodes", []) if e.get("streamUrl")]
    eps.sort(key=lambda e: int(e["number"]))
    return {
        "anime": {
            "id": detail["id"],
            "title": detail["title"],
            "rawTitle": detail.get("rawTitle", ""),
            "poster": detail.get("poster", ""),
            "banner": detail.get("banner", ""),
            "synopsis": detail.get("synopsis", ""),
            "score": detail.get("score"),
            "year": detail.get("year"),
            "season": detail.get("season", ""),
            "status": detail.get("status", ""),
            "type": detail.get("type", ""),
            "studio": detail.get("studio", ""),
            "genres": detail.get("genres", []),
            "altTitles": detail.get("altTitles", []),
            "anilistId": detail.get("anilistId"),
            "totalEpisodes": detail.get("totalEpisodes", len(eps)),
        },
        "episodes": [
            {
                "number": int(e["number"]),
                "title": e.get("title", ""),
                "streamUrl": e.get("streamUrl", ""),
                "downloadUrl": e.get("downloadUrl", ""),
                "referer": e.get("referer", ""),
                "thumbnail": e.get("thumbnail", ""),
                "subtitles": e.get("subtitles", []),
            }
            for e in eps
        ],
    }


def summary_of(detail):
    p = payload_of(detail)
    a = p["anime"]
    return {
        "id": a["id"],
        "title": a["title"],
        "poster": a["poster"],
        "banner": a["banner"],
        "score": a["score"],
        "year": a["year"],
        "season": a["season"],
        "status": a["status"],
        "type": a["type"],
        "genres": a["genres"],
        "episodeCount": len(p["episodes"]),
        "totalEpisodes": a["totalEpisodes"],
    }


def digest(obj):
    raw = json.dumps(obj, sort_keys=True, ensure_ascii=False).encode()
    return hashlib.sha256(raw).hexdigest()[:16]


def main():
    if not TOKEN and not DRY_RUN:
        print("SYNC_TOKEN kosong — berhenti.")
        return 1
    print(f"Ambil daftar hulu dari {OLD_API}")
    catalog = get_json(OLD_API).get("data") or []
    print(f"Hulu: {len(catalog)} judul")

    titles_dir = os.path.join(MIRROR_DIR, "titles")
    os.makedirs(titles_dir, exist_ok=True)

    imported = updated_skip = gated = failed = 0
    reasons = {}
    summaries = []
    full_details = []
    full_written = 0
    for item in catalog:
        cid = item["id"]
        try:
            detail = get_json(f"{OLD_API}?id={cid}").get("data")
        except Exception as e:
            print(f"GAGAL detail {cid}: {e}")
            failed += 1
            continue
        if not detail:
            gated += 1
            reasons["detail-kosong"] = reasons.get("detail-kosong", 0) + 1
            continue
        if FULL_MIRROR and not DRY_RUN:
            # Cermin penuh situs lama: semua judul, tanpa gerbang.
            full_details.append(detail)
            fh = digest(detail)
            wdir = os.path.join(MIRROR_DIR, "web", "titles")
            os.makedirs(wdir, exist_ok=True)
            wpath = os.path.join(wdir, f"{cid}.json")
            old_fh = None
            if os.path.exists(wpath):
                try:
                    old_fh = json.load(open(wpath)).get("_syncHash")
                except Exception:
                    old_fh = None
            if old_fh != fh:
                doc = dict(detail)
                doc["_syncHash"] = fh
                with open(wpath, "w") as f:
                    json.dump(doc, f, ensure_ascii=False, indent=1)
                full_written += 1
        ok, reason = gate(detail)
        if not ok:
            gated += 1
            reasons[reason] = reasons.get(reason, 0) + 1
            continue
        summaries.append(summary_of(detail))
        payload = payload_of(detail)
        h = digest(payload)
        tpath = os.path.join(titles_dir, f"{cid}.json")
        old_hash = None
        if os.path.exists(tpath):
            try:
                old_hash = json.load(open(tpath)).get("_syncHash")
            except Exception:
                old_hash = None
        if old_hash == h:
            updated_skip += 1
            continue
        if not DRY_RUN:
            try:
                r = requests.post(
                    f"{NEW_API}/api/sync/upsert",
                    json=payload,
                    headers={**UA, "x-sync-token": TOKEN, "Content-Type": "application/json"},
                    timeout=45,
                )
                if r.status_code not in (200, 201):
                    print(f"GAGAL upsert {cid}: HTTP {r.status_code} {r.text[:120]}")
                    failed += 1
                    continue
            except Exception as e:
                print(f"GAGAL upsert {cid}: {e}")
                failed += 1
                continue
            mirror_doc = dict(detail)
            mirror_doc["_syncHash"] = h
            with open(tpath, "w") as f:
                json.dump(mirror_doc, f, ensure_ascii=False, indent=1)
        imported += 1
        time.sleep(0.15)

    # Susun catalog.json + penjaga anti-menyusut
    cat_path = os.path.join(MIRROR_DIR, "catalog.json")
    old_count = -1
    if os.path.exists(cat_path):
        try:
            old_count = int(json.load(open(cat_path)).get("count", -1))
        except Exception:
            old_count = -1
    if not DRY_RUN:
        if len(summaries) >= old_count:
            doc = {
                "generatedAt": datetime.now(timezone.utc).isoformat(),
                "count": len(summaries),
                "data": summaries,
            }
            with open(cat_path, "w") as f:
                json.dump(doc, f, ensure_ascii=False, indent=1)
        else:
            print(f"ANTI-MENYUSUT: hasil baru {len(summaries)} < terbit {old_count} — catalog.json dipertahankan")

    if FULL_MIRROR and not DRY_RUN:
        web_dir = os.path.join(MIRROR_DIR, "web")
        os.makedirs(web_dir, exist_ok=True)
        stamp = datetime.now(timezone.utc).isoformat()
        wcat = os.path.join(web_dir, "catalog.json")
        old_wc = -1
        if os.path.exists(wcat):
            try:
                old_wc = int(json.load(open(wcat)).get("count", -1))
            except Exception:
                old_wc = -1
        if len(catalog) >= old_wc:
            with open(wcat, "w") as f:
                json.dump({"generatedAt": stamp, "count": len(catalog), "data": catalog}, f, ensure_ascii=False, indent=1)
        else:
            print(f"ANTI-MENYUSUT web/catalog: {len(catalog)} < {old_wc} — dipertahankan")
        wfull = os.path.join(web_dir, "catalog-full.json")
        old_wf = -1
        if os.path.exists(wfull):
            try:
                old_wf = len(json.load(open(wfull)))
            except Exception:
                old_wf = -1
        if len(full_details) >= old_wf:
            with open(wfull, "w") as f:
                json.dump(full_details, f, ensure_ascii=False)
        else:
            print(f"ANTI-MENYUSUT web/full: {len(full_details)} < {old_wf} — dipertahankan")
        print(f"cermin penuh web/: {len(full_details)} rekaman detail, {full_written} berkas judul ditulis")

    print(
        "--- sinkron selesai: "
        f"{imported} diimpor/diperbarui, {updated_skip} tidak berubah, "
        f"{gated} tertahan gerbang {reasons}, {failed} gagal ---"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
