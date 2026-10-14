"""Build web/data/queue.json from the curated LY Budget Center table (S04).

The table is a manual extraction (data/curated/), verified against the archived
PDF. It is a Taipower district-office (區營業處) aggregate and must never be
pushed down to townships or feeders. The site shows the district of the county
being viewed (counties.py) and compares all listed districts.
"""

from __future__ import annotations

import csv
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CURATED = ROOT / "data" / "curated" / "S04_queue_by_district_2025-06-30.csv"
COVERAGE = ROOT / "data" / "curated" / "S14_district_service_area.csv"
MANIFEST = ROOT / "data" / "manifests" / "snapshots.csv"
OUT = ROOT / "web" / "data" / "queue.json"

BUCKETS = ["半年以內", "超過半年1年以內", "超過1年2年以內", "超過2年以上"]


def main() -> None:
    rows = list(csv.DictReader(CURATED.open(encoding="utf-8")))
    table = {(r["district"], r["wait_bucket"]): (int(r["case_count"].replace(",", "")), float(r["capacity_mw"])) for r in rows}
    districts = sorted({r["district"] for r in rows} - {"合計(A)"})

    for d in districts:
        assert sum(table[(d, b)][0] for b in BUCKETS) == table[(d, "total")][0], f"{d}: bucket case counts do not sum to total"
        assert abs(sum(table[(d, b)][1] for b in BUCKETS) - table[(d, "total")][1]) < 0.015, f"{d}: bucket MW do not sum to total"

    listed_total = table[("合計(A)", "total")]
    ranked = sorted(districts, key=lambda d: table[(d, "total")][1], reverse=True)
    manifest = list(csv.DictReader(MANIFEST.open(encoding="utf-8")))
    snap = next(r for r in manifest if r["source_id"] == "S04")
    cov_snap = next(r for r in manifest if r["source_id"] == "S14")
    coverage = {r["district"]: r["service_area"] for r in csv.DictReader(COVERAGE.open(encoding="utf-8"))}
    missing = [d for d in districts if d not in coverage]
    assert not missing, f"no service area for {missing}"

    queue = {
        "source_id": "S04",
        "as_of": "2025-06-30",
        "listed_districts": len(districts),
        "listed_total_cases": listed_total[0],
        "listed_total_mw": listed_total[1],
        "citation": (
            "來源：立法院預算中心〈115年度中央政府總預算案整體評估報告〉一二、表2"
            "〈截至114年6月底止太陽光電裝置等候併網件數及裝置容量統計表〉（PDF 第 3 頁），"
            "資料來源為台電公司 114 年 8 月提供。"
        ),
        "note": (
            f"表2只列 {len(districts)} 個區處（合計 {listed_total[0]:,} 件、{listed_total[1]:,.2f} MW），"
            "未說明其他區處為零或未列入，因此不稱為全臺總數。"
        ),
        "source_url": snap["source_url"],
        "page_url": snap["dataset_page"],
        "snapshot_id": snap["snapshot_id"],
        "archive_sha256": snap["sha256"],
        "archive_path": snap["archive_path"],
        "curated_table": str(CURATED.relative_to(ROOT)),
        # All listed districts, largest first. Service areas come from a different
        # report (FY114, 表4) because the FY115 table does not list them.
        "districts": [
            {"district": d, "office": f"台電{d}區營業處", "service_area": coverage[d],
             "cases": table[(d, "total")][0], "capacity_mw": table[(d, "total")][1],
             "share_of_listed_mw": round(table[(d, "total")][1] / listed_total[1], 4),
             "rank_by_mw_among_listed": ranked.index(d) + 1,
             "over_2y_cases": table[(d, "超過2年以上")][0], "over_2y_mw": table[(d, "超過2年以上")][1],
             "buckets": [{"bucket": b, "cases": table[(d, b)][0], "capacity_mw": table[(d, b)][1]} for b in BUCKETS]}
            for d in ranked],
        "coverage_source": {
            "snapshot_id": cov_snap["snapshot_id"], "page_url": cov_snap["dataset_page"], "source_url": cov_snap["source_url"],
            "citation": "轄區取自立法院預算中心〈114年度中央政府總預算案整體評估報告〉一○一、表4（PDF 第 6 頁）。",
        },
    }
    OUT.write_text(json.dumps(queue, ensure_ascii=False, indent=1, allow_nan=False))
    print(json.dumps([{k: d[k] for k in ("district", "cases", "capacity_mw", "over_2y_cases", "rank_by_mw_among_listed")}
                      for d in queue["districts"]], ensure_ascii=False))


if __name__ == "__main__":
    main()
