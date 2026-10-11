"""Approve archived snapshots for publication, or list the ones awaiting review.

Usage:
  uv run python src/ingest/approve.py --pending          # archived but not yet published
  uv run python src/ingest/approve.py S02-2026-10-05     # add to data/manifests/published.json
"""

from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MANIFEST = ROOT / "data" / "manifests" / "snapshots.csv"
PUBLISHED = ROOT / "data" / "manifests" / "published.json"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("snapshot_ids", nargs="*")
    ap.add_argument("--pending", action="store_true", help="print archived snapshots not yet approved")
    args = ap.parse_args()

    archived = [r["snapshot_id"] for r in csv.DictReader(MANIFEST.open(encoding="utf-8"))]
    published = json.loads(PUBLISHED.read_text(encoding="utf-8"))
    approved = set(published["approved"])

    if args.pending:
        print("\n".join(s for s in archived if s not in approved))
        return
    unknown = [s for s in args.snapshot_ids if s not in archived]
    if unknown:
        raise SystemExit(f"not in the archive manifest: {unknown}")
    published["approved"] = sorted(approved | set(args.snapshot_ids))
    PUBLISHED.write_text(json.dumps(published, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    print("approved:", ", ".join(args.snapshot_ids))


if __name__ == "__main__":
    main()
