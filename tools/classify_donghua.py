#!/usr/bin/env python3
"""Klasifikasi negara asal katalog Onimeku dari AniList.

Menghasilkan web/donghua.json: peta id katalog -> kode negara
asal AniList (JP/KR/CN/TW) + daftar donghuaIds (negara CN).
Situs web memakai peta ini untuk memisahkan donghua ke baris
"Donghua" sendiri di beranda.

Sumber kebenaran: AniList `countryOfOrigin` — hanya bisa
dijangkau dari runner GitHub Actions (IP pusat data VM/Worker
diblokir AniList). Judul ber-anilistId ditanyakan massal per
50; judul tanpa anilistId dicari per judul dengan penjaga
kemiripan judul + tahun agar tidak salah tempel.

Hasil run sebelumnya TIDAK pernah menyusut: peta lama dibaca
dulu dan hanya ditimpa data AniList yang lebih segar.

Env:
  OLD_API  alamat daftar katalog (bawaan API situs lama)
  OUT      berkas keluaran (bawaan web/donghua.json)
  MIRROR   cermin penuh lokal untuk gabungan id (bawaan
           web/catalog-full.json bila ada)
"""

import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

OLD_API = os.environ.get(
    "OLD_API", "https://my-anime-app.adiyth-arpansa.workers.dev/api/anime"
)
OUT = os.environ.get("OUT", "web/donghua.json")
MIRROR = os.environ.get("MIRROR", "web/catalog-full.json")
ANILIST = "https://graphql.anilist.co"
UA = {"User-Agent": "OnimekuClassify/1.0 (+klasifikasi katalog)"}


def http_json(url, payload=None, timeout=40, retries=4):
    data = json.dumps(payload).encode() if payload is not None else None
    headers = dict(UA)
    if data is not None:
        headers["Content-Type"] = "application/json"
    last = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, data=data, headers=headers, method="POST" if data else "GET")
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            last = e
            if e.code == 429:
                wait = 5
                try:
                    wait = int(e.headers.get("Retry-After") or 5)
                except Exception:
                    pass
                print(f"  AniList 429, tunggu {wait} dtk...", flush=True)
                time.sleep(min(wait, 60))
                continue
            if e.code in (500, 502, 503, 504) and attempt < retries - 1:
                time.sleep(3 * (attempt + 1))
                continue
            raise
        except Exception as e:  # jaringan sesaat
            last = e
            if attempt < retries - 1:
                time.sleep(3 * (attempt + 1))
                continue
            raise
    raise RuntimeError(f"gagal memanggil {url}: {last}")


def norm_title(t):
    t = (t or "").lower()
    t = re.sub(r"[^a-z0-9]+", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def similarity(a, b):
    """Kemiripan himpunan token judul (0..1)."""
    sa, sb = set(norm_title(a).split()), set(norm_title(b).split())
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / max(len(sa), len(sb))


def load_catalog():
    items = {}
    try:
        data = http_json(OLD_API)
        rows = data if isinstance(data, list) else data.get("data") or []
        for r in rows:
            if r.get("id"):
                items[r["id"]] = r
        print(f"API katalog: {len(rows)} judul", flush=True)
    except Exception as e:
        print(f"API katalog gagal ({e}); lanjut dari cermin saja", flush=True)
    if os.path.exists(MIRROR):
        try:
            rows = json.load(open(MIRROR, encoding="utf-8"))
            n0 = len(items)
            for r in rows:
                rid = r.get("id")
                if not rid:
                    continue
                if rid not in items:
                    items[rid] = r
                elif not items[rid].get("anilistId") and r.get("anilistId"):
                    items[rid]["anilistId"] = r["anilistId"]
            print(f"Cermin penuh: gabungan {len(items)} judul (dari {n0})", flush=True)
        except Exception as e:
            print(f"Cermin penuh gagal dibaca ({e})", flush=True)
    return items


def anilist_batch(ids):
    q = """
    query($ids:[Int]){ Page(page:1, perPage:50){
      media(id_in:$ids, type:ANIME){ id countryOfOrigin } } }
    """
    out = {}
    for i in range(0, len(ids), 50):
        chunk = ids[i : i + 50]
        data = http_json(ANILIST, {"query": q, "variables": {"ids": chunk}})
        media = (((data or {}).get("data") or {}).get("Page") or {}).get("media") or []
        for m in media:
            if m.get("id") and m.get("countryOfOrigin"):
                out[int(m["id"])] = m["countryOfOrigin"]
        print(f"  batch id {i//50+1}: total terklasifikasi {len(out)}", flush=True)
        time.sleep(1.0)
    return out


def anilist_search(title):
    q = """
    query($s:String){ Page(page:1, perPage:5){
      media(search:$s, type:ANIME, sort:SEARCH_MATCH){
        id countryOfOrigin startDate{ year }
        title{ romaji english native } } } }
    """
    data = http_json(ANILIST, {"query": q, "variables": {"s": title}})
    return (((data or {}).get("data") or {}).get("Page") or {}).get("media") or []


def best_match(entry, candidates):
    """Pilih kandidat AniList paling meyakinkan untuk satu judul katalog."""
    titles = [entry.get("title") or ""]
    for alt in entry.get("altTitles") or []:
        if alt:
            titles.append(alt)
    year = entry.get("year")
    scored = []
    for c in candidates:
        ct = c.get("title") or {}
        names = [ct.get("romaji") or "", ct.get("english") or "", ct.get("native") or ""]
        score = 0.0
        for t in titles:
            for n in names:
                if not n:
                    continue
                if norm_title(t) == norm_title(n):
                    score = max(score, 1.0)
                else:
                    score = max(score, similarity(t, n))
        cy = ((c.get("startDate") or {}).get("year"))
        if year and cy and abs(int(year) - int(cy)) > 1:
            continue  # tahun jauh: hampir pasti judul lain
        scored.append((score, c))
    if not scored:
        return None
    scored.sort(key=lambda x: -x[0])
    top_score, top = scored[0]
    if top_score < 0.75:
        return None
    # Ambigu: dua kandidat nyaris sama kuat tetapi beda negara.
    if len(scored) > 1 and scored[1][0] >= top_score - 0.05:
        if (scored[1][1].get("countryOfOrigin") or "") != (top.get("countryOfOrigin") or ""):
            return None
    return top


def main():
    items = load_catalog()
    if not items:
        print("Katalog kosong — tidak menulis apa pun.")
        return 1

    prev = {}
    if os.path.exists(OUT):
        try:
            old = json.load(open(OUT, encoding="utf-8"))
            prev = old.get("countries") or {}
            print(f"Peta sebelumnya: {len(prev)} judul terklasifikasi", flush=True)
        except Exception:
            prev = {}

    countries = {}

    # 1) Judul ber-anilistId: tanya massal.
    by_anilist = {}
    for rid, r in items.items():
        aid = r.get("anilistId")
        if aid:
            by_anilist[int(aid)] = rid
    if by_anilist:
        print(f"Menanyakan {len(by_anilist)} anilistId ke AniList...", flush=True)
        found = anilist_batch(sorted(by_anilist))
        for aid, country in found.items():
            rid = by_anilist.get(aid)
            if rid:
                countries[rid] = country

    # 2) Judul tanpa anilistId: cari per judul (hanya yang
    #    belum terklasifikasi dari peta lama, agar run cepat).
    todo = [
        r for rid, r in items.items()
        if not r.get("anilistId") and rid not in countries and rid not in prev
    ]
    print(f"Pencarian per judul untuk {len(todo)} judul tanpa anilistId...", flush=True)
    resolved = 0
    for idx, r in enumerate(todo, 1):
        try:
            cands = anilist_search(r.get("title") or "")
            m = best_match(r, cands)
            if m and m.get("countryOfOrigin"):
                countries[r["id"]] = m["countryOfOrigin"]
                resolved += 1
        except Exception as e:
            print(f"  cari gagal '{r.get('title')}': {e}", flush=True)
        if idx % 25 == 0:
            print(f"  pencarian {idx}/{len(todo)} — terpecahkan {resolved}", flush=True)
        time.sleep(1.1)
    print(f"Pencarian selesai: {resolved} judul terpecahkan", flush=True)

    # 3) Gabung peta lama (yang masih ada di katalog) — anti menyusut.
    for rid, c in prev.items():
        if rid in items and rid not in countries:
            countries[rid] = c

    donghua = sorted(rid for rid, c in countries.items() if c == "CN")
    payload = {
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "totalCatalog": len(items),
        "classified": len(countries),
        "countries": dict(sorted(countries.items())),
        "donghuaIds": donghua,
    }
    os.makedirs(os.path.dirname(OUT) or ".", exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=1, sort_keys=True)
    print(f"SELESAI: katalog {len(items)}, terklasifikasi {len(countries)}, donghua {len(donghua)}")
    for rid in donghua[:60]:
        print(f"  DONGHUA: {items.get(rid, {}).get('title', rid)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
