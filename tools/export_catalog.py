#!/usr/bin/env python3
"""Ekspor katalog Onimeku ke cermin repo adiytharpansa/onimeku-data.

Merakit katalog penuh dari API aplikasi (daftar + detail per judul), lalu
menulis web/catalog-full.json + web/catalog-manifest.json dengan penjaga
monotonik: menolak mendorong cermin yang lebih kecil dari cermin terbit.

Sifat kerja:
- Checkpoint JSONL per judul: run yang terputus bisa dilanjutkan; judul yang
  sudah terakit tidak diambil ulang.
- Detail yang gagal diambil sesudah coba-ulang jatuh ke rekaman cermin
  sebelumnya (fallback) dan dihitung jujur di manifest.
- Fase terpisah: fetch -> assemble -> push (atau `all`).

Pemakaian:
  python3 export_catalog.py [fetch|assemble|push|all]
Direktori kerja (checkpoint + hasil rakitan) dari env EXPORT_WORKDIR,
bawaan: ~/workspace/goals/my-anime-catalog-app/hidden_files/rescue-export
"""
import base64
import json
import os
import subprocess
import sys
import threading
import time
import urllib.request
import urllib.error
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

BASE = "https://my-anime-app.adiyth-arpansa.workers.dev"
UA = ("Mozilla/5.0 (Linux; Android 14; SM-A155F) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/130.0 Mobile Safari/537.36")
REPO = "adiytharpansa/onimeku-data"
MIRROR = "https://cdn.jsdelivr.net/gh/" + REPO + "@main"
WORKDIR = os.environ.get(
    "EXPORT_WORKDIR",
    os.path.expanduser(
        "~/workspace/goals/my-anime-catalog-app/hidden_files/rescue-export"))
CKPT = os.path.join(WORKDIR, "checkpoint.jsonl")
IDS_FILE = os.path.join(WORKDIR, "source_ids.json")
PREV_FULL = os.path.join(WORKDIR, "prev-full.json")
PREV_MANIFEST = os.path.join(WORKDIR, "prev-manifest.json")
OUT_FULL = os.path.join(WORKDIR, "catalog-full.json")
OUT_MANIFEST = os.path.join(WORKDIR, "catalog-manifest.json")
SUMMARY = os.path.join(WORKDIR, "run-summary.json")


def log(msg):
    print("[%s] %s" % (datetime.now().strftime("%H:%M:%S"), msg), flush=True)


def curl_json(url, timeout=60):
    """GET JSON via curl (UA peramban; urllib diblokir Cloudflare 1010)."""
    out = subprocess.run(
        ["curl", "-s", "-A", UA, "--max-time", str(timeout),
         "-w", "\n%{http_code}", url],
        capture_output=True, text=True, timeout=timeout + 15)
    body, _, code = out.stdout.rpartition("\n")
    if code.strip() != "200":
        raise RuntimeError("http %s dari %s" % (code.strip(), url[:90]))
    return json.loads(body)


def curl_text(url, timeout=120):
    out = subprocess.run(
        ["curl", "-s", "-A", UA, "--max-time", str(timeout),
         "-w", "\n%{http_code}", url],
        capture_output=True, text=True, timeout=timeout + 15)
    body, _, code = out.stdout.rpartition("\n")
    if code.strip() != "200":
        raise RuntimeError("http %s dari %s" % (code.strip(), url[:90]))
    return body


# ---------------------------------------------------------------- fase fetch
def get_source_ids():
    """Daftar id sumber: fresh -> biasa -> lite; ditambah id cermin lama
    yang hilang dari daftar supaya cermin tidak pernah menyusut."""
    ids, variant = None, None
    for suffix in ("/api/anime?fresh=1", "/api/anime", "/api/anime?lite=1"):
        try:
            d = curl_json(BASE + suffix, timeout=120)
            items = d.get("data") or []
            got = [it["id"] for it in items if it.get("id")]
            log("daftar %s -> %d judul" % (suffix, len(got)))
            if got:
                ids, variant = got, suffix
                break
        except Exception as e:
            log("daftar %s gagal: %s" % (suffix, e))
    if not ids:
        raise RuntimeError("semua jalur daftar gagal")
    prev = load_prev_full()
    prev_ids = [r["id"] for r in prev]
    topped = [i for i in prev_ids if i not in set(ids)]
    if topped:
        log("daftar ditambah %d id dari cermin lama (anti-menyusut)"
            % len(topped))
        ids = ids + topped
    json.dump({"variant": variant, "ids": ids, "toppedUp": len(topped)},
              open(IDS_FILE, "w"))
    return ids


def load_prev_full():
    if not os.path.exists(PREV_FULL):
        log("mengunduh catalog-full cermin terbit...")
        body = curl_text(MIRROR + "/web/catalog-full.json", timeout=180)
        open(PREV_FULL, "w").write(body)
    return json.load(open(PREV_FULL))


def load_checkpoint():
    done = {}
    if os.path.exists(CKPT):
        for line in open(CKPT):
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
                done[rec["id"]] = rec
            except Exception:
                pass
    return done


def fetch_one(tid, prev_by_id):
    url = "%s/api/anime?id=%s" % (BASE, tid)
    last = None
    for attempt, wait in enumerate((0, 2, 5, 10)):
        if wait:
            time.sleep(wait)
        try:
            d = curl_json(url, timeout=45)
            rec = d.get("data") or d
            if not isinstance(rec, dict) or rec.get("id") != tid:
                raise RuntimeError("rekaman tidak cocok")
            eps = rec.get("episodes")
            if not isinstance(eps, list):
                raise RuntimeError("episodes bukan daftar")
            prev = prev_by_id.get(tid)
            if prev is not None and len(eps) == 0 \
                    and len(prev.get("episodes") or []) > 0:
                raise RuntimeError("episode kosong vs cermin")
            return rec
        except Exception as e:
            last = e
    raise RuntimeError("gagal sesudah coba-ulang: %s" % last)


def phase_fetch():
    os.makedirs(WORKDIR, exist_ok=True)
    ids = get_source_ids()
    prev = load_prev_full()
    prev_by_id = {r["id"]: r for r in prev}
    done = load_checkpoint()
    preexisting = set(done)
    todo = [i for i in ids if i not in done]
    log("sumber %d judul; checkpoint %d; perlu ambil %d"
        % (len(ids), len(done), len(todo)))
    lock = threading.Lock()
    failed = []
    counter = {"n": 0}

    def work(tid):
        try:
            rec = fetch_one(tid, prev_by_id)
            with lock:
                with open(CKPT, "a") as f:
                    f.write(json.dumps(rec, ensure_ascii=True) + "\n")
                counter["n"] += 1
                if counter["n"] % 50 == 0:
                    log("terambil %d/%d" % (counter["n"], len(todo)))
        except Exception as e:
            with lock:
                failed.append(tid)
            log("GAGAL %s: %s" % (tid, e))

    with ThreadPoolExecutor(max_workers=5) as ex:
        list(ex.map(work, todo))
    done = load_checkpoint()
    summary = {"phase": "fetch", "sourceTitles": len(ids),
               "inCheckpoint": len(done), "preexisting": len(preexisting),
               "failedIds": failed}
    json.dump(summary, open(SUMMARY, "w"), indent=2)
    log("fetch selesai: checkpoint=%d gagal=%d" % (len(done), len(failed)))


# ------------------------------------------------------------- fase assemble
def phase_assemble():
    ids = json.load(open(IDS_FILE))["ids"]
    prev = load_prev_full()
    prev_by_id = {r["id"]: r for r in prev}
    prev_manifest = json.load(open(PREV_MANIFEST)) \
        if os.path.exists(PREV_MANIFEST) else {}
    fetch_summary = json.load(open(SUMMARY)) \
        if os.path.exists(SUMMARY) else {}
    preexisting = fetch_summary.get("preexisting", 0)
    done = load_checkpoint()
    records, fallback_ids, skipped_ids = [], [], []
    resumed = 0
    for tid in ids:
        rec = done.get(tid)
        if rec is not None:
            records.append(rec)
            continue
        prev = prev_by_id.get(tid)
        if prev is not None:
            records.append(prev)
            fallback_ids.append(tid)
        else:
            skipped_ids.append(tid)
    resumed = min(preexisting, len(records))
    total_eps = sum(len(r.get("episodes") or []) for r in records)
    manifest = {
        "exportedAt": datetime.now(timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%S.%fZ"),
        "sourceTitles": len(ids),
        "exportedTitles": len(records),
        "totalEpisodes": total_eps,
        "skippedTitles": len(skipped_ids),
        "skippedIds": skipped_ids,
        "fallbackTitles": len(fallback_ids),
        "fallbackIds": fallback_ids,
        "resumedTitles": resumed,
        "previousMirrorTitles": prev_manifest.get("exportedTitles", 0),
        "format": "catalog-full v1 (array of detail records)",
    }
    prev_titles = prev_manifest.get("exportedTitles", 0)
    prev_eps = prev_manifest.get("totalEpisodes", 0)
    problems = []
    if skipped_ids:
        problems.append("ada judul terlewat: %d" % len(skipped_ids))
    if len(records) < prev_titles:
        problems.append("judul menyusut: %d < %d" % (len(records), prev_titles))
    if total_eps < prev_eps:
        problems.append("episode menyusut: %d < %d" % (total_eps, prev_eps))
    json.dump(records, open(OUT_FULL, "w"), ensure_ascii=True)
    json.dump(manifest, open(OUT_MANIFEST, "w"), indent=2, ensure_ascii=False)
    summary = dict(fetch_summary)
    summary.update({"phase": "assemble", "manifest": manifest,
                    "problems": problems})
    json.dump(summary, open(SUMMARY, "w"), indent=2)
    log("assemble: %d judul, %d episode, fallback %d, terlewat %d"
        % (len(records), total_eps, len(fallback_ids), len(skipped_ids)))
    if problems:
        log("PENJAGA MENOLAK: %s" % "; ".join(problems))
        sys.exit(3)
    log("penjaga monotonik LOLOS (sebelumnya %d judul/%d episode)"
        % (prev_titles, prev_eps))


# ----------------------------------------------------------------- fase push
def gh_call(method, path, payload=None, timeout=600):
    sys.path.insert(0, "/opt/muse/skills/custom-connector-setup/bin")
    from dynamic_credentials import add_surrogate_to_request, \
        read_json_response
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request("https://api.github.com" + path,
                                 data=data, method=method)
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("X-GitHub-Api-Version", "2022-11-28")
    if data:
        req.add_header("Content-Type", "application/json")
    add_surrogate_to_request(req, "custom.github-pat",
                             allowed_hosts=["api.github.com"])
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            return resp.status, json.loads(raw.decode()) if raw else {}
    except urllib.error.HTTPError as e:
        try:
            return e.code, read_json_response(e)
        except Exception:
            return e.code, {"error": str(e)}


def make_blob(content_bytes):
    payload = {"content": base64.b64encode(content_bytes).decode(),
               "encoding": "base64"}
    st, body = gh_call("POST", "/repos/%s/git/blobs" % REPO, payload)
    if st not in (200, 201):
        raise RuntimeError("blob gagal %s: %s" % (st, str(body)[:200]))
    return body["sha"]


def phase_push():
    summary = json.load(open(SUMMARY))
    if summary.get("problems"):
        log("push dibatalkan: penjaga menolak di fase assemble")
        sys.exit(3)
    st, ref = gh_call("GET", "/repos/%s/git/ref/heads/main" % REPO)
    head_sha = ref["object"]["sha"]
    st, commit = gh_call("GET", "/repos/%s/git/commits/%s" % (REPO, head_sha))
    base_tree = commit["tree"]["sha"]
    log("HEAD main %s, tree %s" % (head_sha[:8], base_tree[:8]))
    entries = []
    for path, local in (("web/catalog-full.json", OUT_FULL),
                        ("web/catalog-manifest.json", OUT_MANIFEST),
                        ("tools/export_catalog.py",
                         os.path.abspath(__file__))):
        blob = make_blob(open(local, "rb").read())
        entries.append({"path": path, "mode": "100644",
                        "type": "blob", "sha": blob})
        log("blob %s -> %s" % (path, blob[:8]))
    st, tree = gh_call("POST", "/repos/%s/git/trees" % REPO,
                       {"base_tree": base_tree, "tree": entries})
    if st not in (200, 201):
        raise RuntimeError("tree gagal %s" % st)
    manifest = json.load(open(OUT_MANIFEST))
    msg = ("Ekspor katalog penyelamat: %d judul, %d episode "
           "(fallback %d)" % (manifest["exportedTitles"],
                              manifest["totalEpisodes"],
                              manifest["fallbackTitles"]))
    st, new_commit = gh_call("POST", "/repos/%s/git/commits" % REPO,
                             {"message": msg, "tree": tree["sha"],
                              "parents": [head_sha]})
    if st not in (200, 201):
        raise RuntimeError("commit gagal %s" % st)
    st, updated = gh_call("PATCH", "/repos/%s/git/refs/heads/main" % REPO,
                          {"sha": new_commit["sha"]})
    if st not in (200,):
        raise RuntimeError("update ref gagal %s: %s" % (st, str(updated)[:200]))
    log("commit terbit: %s" % new_commit["sha"])
    for p in ("web/catalog-full.json", "web/catalog-manifest.json"):
        try:
            curl_text("https://purge.jsdelivr.net/gh/%s@main/%s"
                      % (REPO, p), timeout=30)
            log("purge jsDelivr: %s" % p)
        except Exception as e:
            log("purge %s gagal (diabaikan): %s" % (p, e))
    summary["commit"] = new_commit["sha"]
    json.dump(summary, open(SUMMARY, "w"), indent=2)


if __name__ == "__main__":
    phase = sys.argv[1] if len(sys.argv) > 1 else "all"
    if phase in ("fetch", "all"):
        phase_fetch()
    if phase in ("assemble", "all"):
        phase_assemble()
    if phase in ("push", "all"):
        phase_push()
    log("fase %s selesai" % phase)
