"""Build web/data/queue.json from the curated LY Budget Center table (S04).

The table is a manual extraction (data/curated/), verified against the archived
PDF. It is a Taipower district-office (區營業處) aggregate and must never be
pushed down to townships or feeders.
"""

from __future__ import annotations

import csv
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CURATED = ROOT / "data" / "curated" / "S04_queue_by_district_2025-06-30.csv"
MANIFEST = ROOT / "data" / "manifests" / "snapshots.csv"
OUT = ROOT / "web" / "data" / "queue.json"

DISTRICT = "雲林"
BUCKETS = ["半年以內", "超過半年1年以內", "超過1年2年以內", "超過2年以上"]


def main() -> None:
    rows = list(csv.DictReader(CURATED.open(encoding="utf-8")))
    table = {(r["district"], r["wait_bucket"]): (int(r["case_count"].replace(",", "")), float(r["capacity_mw"])) for r in rows}
    districts = sorted({r["district"] for r in rows} - {"合計(A)"})

    yl = {b: table[(DISTRICT, b)] for b in BUCKETS + ["total"]}
    assert sum(yl[b][0] for b in BUCKETS) == yl["total"][0], "Yunlin bucket case counts do not sum to total"
    assert abs(sum(yl[b][1] for b in BUCKETS) - yl["total"][1]) < 0.015, "Yunlin bucket MW do not sum to total"

    listed_total = table[("合計(A)", "total")]
    ranked = sorted(districts, key=lambda d: table[(d, "total")][1], reverse=True)
    snap = next(r for r in csv.DictReader(MANIFEST.open(encoding="utf-8")) if r["source_id"] == "S04")

    queue = {
        "source_id": "S04",
        "district": "台電雲林區營業處",
        "as_of": "2025-06-30",
        "cases": yl["total"][0],
        "capacity_mw": yl["total"][1],
        "over_2y_cases": yl["超過2年以上"][0],
        "over_2y_mw": yl["超過2年以上"][1],
        "buckets": [{"bucket": b, "cases": yl[b][0], "capacity_mw": yl[b][1]} for b in BUCKETS],
        "listed_districts": len(districts),
        "listed_total_cases": listed_total[0],
        "listed_total_mw": listed_total[1],
        "share_of_listed_mw": round(yl["total"][1] / listed_total[1], 4),
        "rank_by_mw_among_listed": ranked.index(DISTRICT) + 1,
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
        "archive_sha256": snap["sha256"],
        "archive_path": snap["archive_path"],
        "curated_table": str(CURATED.relative_to(ROOT)),
    }
    OUT.write_text(json.dumps(queue, ensure_ascii=False, indent=1))
    print(json.dumps({k: queue[k] for k in ("cases", "capacity_mw", "over_2y_cases", "over_2y_mw", "share_of_listed_mw", "rank_by_mw_among_listed")}, ensure_ascii=False))


if __name__ == "__main__":
    main()
