"""Archive source ZIPs: Taipower hosting capacity (S01 national, S02 Yunlin)
and MOI township boundaries (S12).

Taipower overwrites the same URL on every update, so each distinct file is
kept under data/raw/<source_id>/<file_date>/ and recorded in
data/manifests/snapshots.csv. Wayback Machine captures backfill older versions.

The CSVs carry no content date. `file_date` is the newest modification time of
the members inside the ZIP — the best available proxy, labelled as such.
"""

from __future__ import annotations

import csv
import hashlib
import io
import ssl
import sys
import urllib.parse
import urllib.request
import zipfile
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
RAW = ROOT / "data" / "raw"
MANIFEST = ROOT / "data" / "manifests" / "snapshots.csv"
CHECKS = ROOT / "data" / "manifests" / "checks.csv"  # one row per live check, changed or not

BASE = "https://service.taipower.com.tw/data/opendata/apply/file/d077009/"
SOURCES = {
    "S01": {"url": BASE + "001.zip", "file": "001.zip", "dataset": "https://data.gov.tw/dataset/161874"},
    "S02": {"url": BASE + "YUNLIN.zip", "file": "YUNLIN.zip", "dataset": "https://data.gov.tw/dataset/44208"},
    "S04": {
        "url": "https://www.ly.gov.tw/Pages/ashx/File.ashx?FilePath=~/File/Attach/255076/File_19860273.pdf",
        "file": "ly115_budget_eval_s12.pdf",
        "dataset": "https://www.ly.gov.tw/Pages/Detail.aspx?nodeid=56112&pid=255076",
        # Not a ZIP: date is the PDF's /CreationDate (2025-09-16 10:28:57 +08:00).
        "file_date": "2025-09-16",
        "file_date_basis": "pdf_creation_date",
    },
    "S12": {
        "url": "https://www.tgos.tw/tgos/VirtualDir/Product/3fe61d4a-ca23-4f45-8aca-4a536f40f290/"
        + urllib.parse.quote("鄉(鎮、市、區)界線1140318.zip"),
        "file": "township_1140318.zip",
        "dataset": "https://data.gov.tw/dataset/7441",
    },
}
# Wayback captures with distinct digests (CDX query, collapse=digest).
WAYBACK = {
    "S01": ["20250424125909"],
    "S02": ["20241116125219", "20250424135946"],
}

FIELDS = [
    "snapshot_id", "source_id", "origin", "source_url", "fetch_url", "dataset_page",
    "retrieved_at", "http_last_modified", "wayback_timestamp", "file_date",
    "file_date_basis", "members", "sha256", "bytes", "archive_path",
]


def fetch(url: str) -> tuple[bytes, str]:
    # Taipower's certificate lacks a Subject Key Identifier, which Python 3.13+
    # rejects under VERIFY_X509_STRICT. Chain and hostname are still verified.
    ctx = ssl.create_default_context()
    ctx.verify_flags &= ~ssl.VERIFY_X509_STRICT
    req = urllib.request.Request(url, headers={"User-Agent": "solar-grid-observatory/0.1 (research archive)"})
    with urllib.request.urlopen(req, timeout=120, context=ctx) as r:
        return r.read(), r.headers.get("Last-Modified", "")


def zip_members(blob: bytes) -> list[tuple[str, str]]:
    with zipfile.ZipFile(io.BytesIO(blob)) as z:
        return [(i.filename, datetime(*i.date_time).strftime("%Y-%m-%d")) for i in z.infolist()]


def load_manifest() -> list[dict]:
    if not MANIFEST.exists():
        return []
    with MANIFEST.open(encoding="utf-8") as f:
        return list(csv.DictReader(f))


def archive(source_id: str, origin: str, fetch_url: str, wayback_ts: str = "") -> dict | None:
    src = SOURCES[source_id]
    blob, last_mod = fetch(fetch_url)
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

    return {
        "snapshot_id": f"{source_id}-{file_date}",
        "source_id": source_id,
        "origin": origin,
        "source_url": src["url"],
        "fetch_url": fetch_url,
        "dataset_page": src["dataset"],
        "retrieved_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "http_last_modified": parsedate_to_datetime(last_mod).strftime("%Y-%m-%dT%H:%M:%SZ") if last_mod else "",
        "wayback_timestamp": wayback_ts,
        "file_date": file_date,
        "file_date_basis": src.get("file_date_basis", "zip_member_mtime"),
        "members": ";".join(f"{n}@{d}" for n, d in members),
        "sha256": sha,
        "bytes": len(blob),
        "archive_path": str(path.relative_to(ROOT)),
    }


def main() -> None:
    rows = {r["sha256"]: r for r in load_manifest()}
    jobs = [(sid, "publisher_live", s["url"], "") for sid, s in SOURCES.items()]
    jobs += [
        (sid, "wayback", f"https://web.archive.org/web/{ts}id_/{SOURCES[sid]['url']}", ts)
        for sid, stamps in WAYBACK.items() for ts in stamps
    ]
    checks = []
    for sid, origin, url, ts in jobs:
        rec = archive(sid, origin, url, ts)
        if origin == "publisher_live":
            checks.append({"checked_at": rec["retrieved_at"], "source_id": sid, "sha256": rec["sha256"],
                           "changed": rec["sha256"] not in rows, "snapshot_id": rec["snapshot_id"]})
        if rec["sha256"] in rows:
            print(f"unchanged {rec['snapshot_id']} ({origin})")
            continue
        rows[rec["sha256"]] = rec
        print(f"archived  {rec['snapshot_id']} ({origin}) {rec['bytes']:,} B sha256={rec['sha256'][:12]}")

    MANIFEST.parent.mkdir(parents=True, exist_ok=True)
    with MANIFEST.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        w.writerows(sorted(rows.values(), key=lambda r: (r["source_id"], r["file_date"])))

    new_log = not CHECKS.exists()
    with CHECKS.open("a", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["checked_at", "source_id", "sha256", "changed", "snapshot_id"])
        if new_log:
            w.writeheader()
        w.writerows(checks)


if __name__ == "__main__":
    main()
