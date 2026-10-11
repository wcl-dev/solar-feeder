"""Check sources and archive new versions.

Taipower overwrites the same URL on every update (S01 national, S02 Yunlin), so
each distinct file is kept under data/raw/<source_id>/<file_date>/ and recorded
in data/manifests/snapshots.csv. A missed version is lost for good, so these are
checked on every run with conditional requests (ETag / Last-Modified): an
unchanged file costs one 304 response and no download.

Archiving is not publishing: a new snapshot only reaches the site once it is
listed in data/manifests/published.json (see src/ingest/approve.py).

The CSVs carry no content date. `file_date` is the newest modification time of
the members inside the ZIP — the best available proxy, labelled as such.

Usage:
  uv run python src/ingest/fetch_snapshots.py            # scheduled check
  uv run python src/ingest/fetch_snapshots.py --force    # ignore schedules and conditional requests
  uv run python src/ingest/fetch_snapshots.py --no-wayback-save
  uv run python src/ingest/fetch_snapshots.py --archive-manual   # also archive manual sources not yet in the archive
  uv run python src/ingest/fetch_snapshots.py --via-wayback      # test the Wayback relay used when Taipower is unreachable

Taipower sometimes does not answer GitHub's cloud runners (TLS handshake timeouts,
seen 2026-10-11). A direct check is retried once; if it still fails, the file is
checked through a fresh Wayback Machine capture instead ("via=wayback").
New Taipower snapshots are printed as "NEW SNAPSHOT <id>" and, under GitHub
Actions, written to $GITHUB_OUTPUT as new_snapshots=<id,id>.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import os
import re
import ssl
import time
import sys
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
RAW = ROOT / "data" / "raw"
MANIFESTS = ROOT / "data" / "manifests"
MANIFEST = MANIFESTS / "snapshots.csv"
CHECKS = MANIFESTS / "checks.csv"  # one row per check, changed or not
HTTP_STATE = MANIFESTS / "http_state.json"  # last ETag / Last-Modified per source
WAYBACK_SAVES = MANIFESTS / "wayback_saves.csv"
USER_AGENT = "solar-grid-observatory/0.1 (research archive)"

BASE = "https://service.taipower.com.tw/data/opendata/apply/file/d077009/"
SOURCES = {
    # mode "archive": fetch the fixed URL on every run and archive new content.
    "S01": {"mode": "archive", "url": BASE + "001.zip", "file": "001.zip", "dataset": "https://data.gov.tw/dataset/161874"},
    "S02": {"mode": "archive", "url": BASE + "YUNLIN.zip", "file": "YUNLIN.zip", "dataset": "https://data.gov.tw/dataset/44208"},
    # mode "manual": a new release is a new report at a new URL; curated by hand (data/curated/).
    "S04": {
        "mode": "manual",
        "url": "https://www.ly.gov.tw/Pages/ashx/File.ashx?FilePath=~/File/Attach/255076/File_19860273.pdf",
        "file": "ly115_budget_eval_s12.pdf",
        "dataset": "https://www.ly.gov.tw/Pages/Detail.aspx?nodeid=56112&pid=255076",
        # Not a ZIP: date is the PDF's /CreationDate (2025-09-16 10:28:57 +08:00).
        "file_date": "2025-09-16",
        "file_date_basis": "pdf_creation_date",
    },
    "S14": {
        "mode": "manual",
        # LY Budget Center, FY114 overall evaluation, section 101: 表4 lists each Taipower district's service area.
        "url": "https://www.ly.gov.tw/Pages/ashx/File.ashx?FilePath=~/File/Attach/243293/File_19817703.pdf",
        "file": "ly114_budget_eval_s101.pdf",
        "dataset": "https://www.ly.gov.tw/Pages/Detail.aspx?nodeid=55648&pid=243293",
        "file_date": "2024-09-16",  # PDF /CreationDate 2024-09-16 09:40:58 +08:00
        "file_date_basis": "pdf_creation_date",
    },
    # mode "watch": the download URL carries the release date, so a new release is a new URL.
    # Every `every_days`, compare the URL listed on data.gov.tw and report a change for manual handling.
    "S12": {
        "mode": "watch",
        "every_days": 90,
        "api": "https://data.gov.tw/api/v2/rest/dataset/7441",
        "url": "https://www.tgos.tw/tgos/VirtualDir/Product/3fe61d4a-ca23-4f45-8aca-4a536f40f290/"
        + urllib.parse.quote("鄉(鎮、市、區)界線1140318.zip"),
        "file": "township_1140318.zip",
        "dataset": "https://data.gov.tw/dataset/7441",
    },
}
# Wayback captures with distinct digests (CDX query, collapse=digest); fetched once.
WAYBACK = {
    "S01": ["20250424125909"],
    "S02": ["20241116125219", "20250424135946"],
}

FIELDS = [
    "snapshot_id", "source_id", "origin", "source_url", "fetch_url", "dataset_page",
    "retrieved_at", "http_last_modified", "wayback_timestamp", "file_date",
    "file_date_basis", "members", "sha256", "bytes", "archive_path",
]
CHECK_FIELDS = ["checked_at", "source_id", "result", "sha256", "snapshot_id", "via"]
# result: not_modified (304) | unchanged (downloaded, same hash) | new | url_same | url_changed | error


def now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def http_get(url: str, headers: dict | None = None, timeout: int = 120) -> tuple[int, bytes, dict]:
    # Taipower's certificate lacks a Subject Key Identifier, which Python 3.13+
    # rejects under VERIFY_X509_STRICT. Chain and hostname are still verified.
    ctx = ssl.create_default_context()
    ctx.verify_flags &= ~ssl.VERIFY_X509_STRICT
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as r:
            return r.status, r.read(), dict(r.headers)
    except urllib.error.HTTPError as e:
        if e.code == 304:
            return 304, b"", dict(e.headers)
        raise


def http_get_retry(url: str, headers: dict | None = None, attempts: int = 2, timeout: int = 45) -> tuple[int, bytes, dict]:
    for i in range(attempts):
        try:
            return http_get(url, headers, timeout=timeout)
        except (urllib.error.URLError, TimeoutError, OSError):
            if i == attempts - 1:
                raise
            time.sleep(10)
    raise AssertionError("unreachable")


def via_wayback(url: str) -> tuple[bytes, dict, str, dict]:
    """Have Wayback capture url now, then download that capture. Returns (blob, headers, timestamp, save record)."""
    save = wayback_save(url)
    m = re.search(r"/web/(\d{14})", save["capture"])
    if not m:
        raise RuntimeError(f"Wayback relay: no capture ({save['status']})")
    ts = m.group(1)
    age = datetime.now(timezone.utc) - datetime.strptime(ts, "%Y%m%d%H%M%S").replace(tzinfo=timezone.utc)
    if age > timedelta(days=1):
        raise RuntimeError(f"Wayback relay: newest capture {ts} is not fresh")
    _, blob, headers = http_get(f"https://web.archive.org/web/{ts}id_/{url}", timeout=180)
    headers = {**headers, "Last-Modified": headers.get("x-archive-orig-last-modified", "")}
    return blob, headers, ts, save


def zip_members(blob: bytes) -> list[tuple[str, str]]:
    with zipfile.ZipFile(io.BytesIO(blob)) as z:
        return [(i.filename, datetime(*i.date_time).strftime("%Y-%m-%d")) for i in z.infolist()]


def read_csv(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with path.open(encoding="utf-8") as f:
        return list(csv.DictReader(f))


def write_csv(path: Path, fields: list[str], rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)


def store(source_id: str, origin: str, fetch_url: str, blob: bytes, headers: dict, wayback_ts: str = "") -> dict:
    """Write blob to data/raw (read-only, never overwritten) and return its manifest row."""
    src = SOURCES[source_id]
    sha = hashlib.sha256(blob).hexdigest()
    if "file_date" in src:
        members, file_date = [], src["file_date"]
    else:
        members = zip_members(blob)
        data_members = [d for n, d in members if n.lower().endswith(".csv") and not n.startswith(("manifest", "schema"))]
        file_date = max(data_members or [d for _, d in members])

    out_dir = RAW / source_id / file_date
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / src["file"]
    if path.exists():
        if hashlib.sha256(path.read_bytes()).hexdigest() != sha:
            sys.exit(f"refusing to overwrite {path}: same file_date, different content")
    else:
        path.write_bytes(blob)
        path.chmod(0o444)

    last_mod = headers.get("Last-Modified", "")
    return {
        "snapshot_id": f"{source_id}-{file_date}",
        "source_id": source_id,
        "origin": origin,
        "source_url": src["url"],
        "fetch_url": fetch_url,
        "dataset_page": src["dataset"],
        "retrieved_at": now(),
        "http_last_modified": parsedate_to_datetime(last_mod).strftime("%Y-%m-%dT%H:%M:%SZ") if last_mod else "",
        "wayback_timestamp": wayback_ts,
        "file_date": file_date,
        "file_date_basis": src.get("file_date_basis", "zip_member_mtime"),
        "members": ";".join(f"{n}@{d}" for n, d in members),
        "sha256": sha,
        "bytes": len(blob),
        "archive_path": str(path.relative_to(ROOT)),
    }


def wayback_save(url: str) -> dict:
    """Ask the Internet Archive to capture url (an independent copy). Best effort."""
    rec = {"requested_at": now(), "url": url, "status": "", "capture": ""}
    try:
        status, _, headers = http_get("https://web.archive.org/save/" + url, timeout=180)
        rec["status"] = str(status)
        rec["capture"] = headers.get("Content-Location", "")
        if not rec["capture"]:  # look the new capture up in the CDX index instead
            _, body, _ = http_get("https://web.archive.org/cdx/search/cdx?output=json&fl=timestamp&limit=-1&url="
                                  + urllib.parse.quote(url, safe=""), timeout=60)
            found = json.loads(body or b"[]")
            rec["capture"] = f"/web/{found[-1][0]}/{url}" if len(found) > 1 else ""
    except Exception as e:  # never fail the archive run because of Wayback
        rec["status"] = f"error: {type(e).__name__}: {e}"[:200]
    return rec


def last_check(checks: list[dict], source_id: str) -> datetime | None:
    times = [r["checked_at"] for r in checks if r["source_id"] == source_id and r.get("result") != "error"]
    return datetime.strptime(max(times), "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc) if times else None


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true", help="ignore schedules and conditional requests")
    ap.add_argument("--no-wayback-save", action="store_true", help="do not ask Wayback to capture new versions")
    ap.add_argument("--archive-manual", action="store_true", help="archive manual sources that are not archived yet")
    ap.add_argument("--via-wayback", action="store_true", help="skip the direct request and check through Wayback (test the relay)")
    args = ap.parse_args()

    manifest = {r["sha256"]: r for r in read_csv(MANIFEST)}
    checks = read_csv(CHECKS)
    for r in checks:  # rows written before the `result` column existed
        if "result" not in r or not r["result"]:
            r["result"] = "new" if r.pop("changed", "") == "True" else "unchanged"
    state = json.loads(HTTP_STATE.read_text()) if HTTP_STATE.exists() else {}
    saves = read_csv(WAYBACK_SAVES)
    new_snapshots, notices = [], []

    for sid, src in SOURCES.items():
        if src["mode"] == "manual":
            if args.archive_manual and not any(r["source_id"] == sid for r in manifest.values()):
                status, blob, headers = http_get(src["url"])
                rec = store(sid, "publisher_live", src["url"], blob, headers)
                manifest[rec["sha256"]] = rec
                print(f"archived  {rec['snapshot_id']} (manual source) sha256={rec['sha256'][:12]}")
            continue
        if src["mode"] == "watch":
            last = last_check(checks, sid)
            if not args.force and last and datetime.now(timezone.utc) - last < timedelta(days=src["every_days"]):
                print(f"skip      {sid} (checked {last:%Y-%m-%d}; every {src['every_days']} days)")
                continue
            try:
                _, body, _ = http_get(src["api"])
                meta = json.loads(body)["result"]
                listed = [d.get("resourceDownloadUrl", "") for d in meta.get("distribution", [])]
                same = any(urllib.parse.unquote(u) == urllib.parse.unquote(src["url"]) for u in listed)
                result = "url_same" if same else "url_changed"
                if not same:
                    notices.append(f"NEW RELEASE {sid}: data.gov.tw now lists {listed} — archive manually")
            except Exception as e:
                result = f"error"
                notices.append(f"CHECK FAILED {sid}: {type(e).__name__}: {e}")
            checks.append({"checked_at": now(), "source_id": sid, "result": result, "sha256": "", "snapshot_id": "", "via": "direct"})
            print(f"{result:9s} {sid}")
            continue

        # mode "archive"
        cond = {} if args.force else {
            k: v for k, v in (("If-None-Match", state.get(sid, {}).get("etag")),
                              ("If-Modified-Since", state.get(sid, {}).get("last_modified"))) if v
        }
        via, relay_ts, relay_save = "direct", "", None
        try:
            if args.via_wayback:
                raise RuntimeError("--via-wayback")
            status, blob, headers = http_get_retry(src["url"], cond)
        except Exception as e:
            if not args.via_wayback:
                notices.append(f"DIRECT FAILED {sid}: {type(e).__name__}: {e} — trying Wayback relay")
            try:
                blob, headers, relay_ts, relay_save = via_wayback(src["url"])
                status, via = 200, "wayback"
            except Exception as e2:
                checks.append({"checked_at": now(), "source_id": sid, "result": "error", "sha256": "", "snapshot_id": "", "via": ""})
                notices.append(f"CHECK FAILED {sid}: {type(e2).__name__}: {e2}")
                continue
        if status == 304:
            prev = state[sid]
            checks.append({"checked_at": now(), "source_id": sid, "result": "not_modified",
                           "sha256": prev["sha256"], "snapshot_id": prev["snapshot_id"], "via": via})
            print(f"not modified {prev['snapshot_id']} (304)")
            continue
        origin = "publisher_live" if via == "direct" else "wayback_relay"
        fetch_url = src["url"] if via == "direct" else f"https://web.archive.org/web/{relay_ts}id_/{src['url']}"
        rec = store(sid, origin, fetch_url, blob, headers, relay_ts)
        is_new = rec["sha256"] not in manifest
        if is_new:
            manifest[rec["sha256"]] = rec
            new_snapshots.append(rec["snapshot_id"])
            print(f"NEW SNAPSHOT {rec['snapshot_id']} {rec['bytes']:,} B sha256={rec['sha256'][:12]} (via {via})")
            if relay_save:
                saves.append({**relay_save, "source_id": sid, "snapshot_id": rec["snapshot_id"]})
            elif not args.no_wayback_save:
                saves.append({**wayback_save(src["url"]), "source_id": sid, "snapshot_id": rec["snapshot_id"]})
        else:
            print(f"unchanged {manifest[rec['sha256']]['snapshot_id']} (via {via})")
        checks.append({"checked_at": rec["retrieved_at"], "source_id": sid, "result": "new" if is_new else "unchanged",
                       "sha256": rec["sha256"], "snapshot_id": manifest[rec["sha256"]]["snapshot_id"], "via": via})
        if via == "direct":  # Wayback responses carry no Taipower ETag
            state[sid] = {"etag": headers.get("ETag", ""), "last_modified": headers.get("Last-Modified", ""),
                          "sha256": rec["sha256"], "snapshot_id": manifest[rec["sha256"]]["snapshot_id"]}

    # Backfill from Wayback, once per capture.
    have = {(r["source_id"], r["wayback_timestamp"]) for r in manifest.values()}
    for sid, stamps in WAYBACK.items():
        for ts in stamps:
            if (sid, ts) in have and not args.force:
                continue
            url = f"https://web.archive.org/web/{ts}id_/{SOURCES[sid]['url']}"
            _, blob, headers = http_get(url)
            rec = store(sid, "wayback", url, blob, headers, ts)
            if rec["sha256"] not in manifest:
                manifest[rec["sha256"]] = rec
                print(f"archived  {rec['snapshot_id']} (wayback {ts})")

    write_csv(MANIFEST, FIELDS, sorted(manifest.values(), key=lambda r: (r["source_id"], r["file_date"])))
    write_csv(CHECKS, CHECK_FIELDS, checks)
    HTTP_STATE.write_text(json.dumps(state, ensure_ascii=False, indent=1) + "\n")
    if saves:
        write_csv(WAYBACK_SAVES, ["requested_at", "source_id", "snapshot_id", "url", "status", "capture"], saves)

    for n in notices:
        print(n)
    if os.environ.get("GITHUB_OUTPUT"):
        with open(os.environ["GITHUB_OUTPUT"], "a") as f:
            f.write(f"new_snapshots={','.join(new_snapshots)}\n")


if __name__ == "__main__":
    main()
