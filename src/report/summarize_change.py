"""Review summary for new S01/S02 snapshots, compared with the newest published ones, per county on the site.

Written as Markdown for the review PR body (or the local weekly check). It is a
reviewer's aid, not a published comparison: the site does not show deltas until
cross-version comparability is confirmed (spec §8.3).

Usage:
  uv run python src/report/summarize_change.py                  # newest pending vs newest approved, S01 and S02
  uv run python src/report/summarize_change.py OLD_ID NEW_ID    # one pair from the same source
"""

from __future__ import annotations

import csv
import io
import json
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src" / "transform"))
from counties import COUNTIES  # noqa: E402

MANIFEST = ROOT / "data" / "manifests" / "snapshots.csv"
PUBLISHED = ROOT / "data" / "manifests" / "published.json"
BINS = [("0", lambda v: v <= 0), ("<1 MW", lambda v: 0 < v < 1000), ("1–3 MW", lambda v: 1000 <= v < 3000),
        ("3–6 MW", lambda v: 3000 <= v < 6000), ("≥6 MW", lambda v: v >= 6000)]
EXPECTED_COLUMNS = {"capacity": ["FEEDER", "CAPACITY"], "tr_feeder": ["ROWNUM", "X", "Y", "FEEDER"]}


def load(row: dict, code: str) -> dict:
    with zipfile.ZipFile(ROOT / row["archive_path"]) as z:
        def member(suffix: str) -> tuple[list[str], list[dict]]:
            name = next(n for n in z.namelist() if n.upper() == f"{code}_{suffix}.CSV".upper())
            reader = csv.DictReader(io.TextIOWrapper(z.open(name), encoding="utf-8-sig"))
            rows = list(reader)
            return reader.fieldnames or [], rows
        cap_cols, cap = member("capacity")
        pts_cols, pts = member("tr_feeder")
    return {
        "id": row["snapshot_id"], "origin": row["origin"], "sha256": row["sha256"],
        "columns": {"capacity": cap_cols, "tr_feeder": pts_cols},
        "cap": {r["FEEDER"].strip(): float(r["CAPACITY"]) for r in cap},
        "points": len(pts),
    }


def bin_of(v: float) -> str:
    return next(name for name, test in BINS if test(v))


def share0(d: dict) -> float:
    return sum(v <= 0 for v in d["cap"].values()) / len(d["cap"])


def county_section(cfg: dict, old_row: dict, new_row: dict) -> tuple[list[str], list[str]]:
    """Markdown lines and warnings for one county's tables in an old/new snapshot pair."""
    old, new = load(old_row, cfg["code"]), load(new_row, cfg["code"])
    warnings = []
    for part, cols in EXPECTED_COLUMNS.items():
        if new["columns"][part] != cols:
            warnings.append(f"欄位改變：{part} 為 {new['columns'][part]}（預期 {cols}）")
    if any(v < 0 for v in new["cap"].values()):
        warnings.append(f"出現負值容量 {sum(v < 0 for v in new['cap'].values())} 筆")
    if abs(share0(new) - share0(old)) > 0.15:
        warnings.append(f"公布為 0 的比例變動超過 15 個百分點（{share0(old):.1%} → {share0(new):.1%}）")
    if abs(len(new["cap"]) - len(old["cap"])) > 0.1 * len(old["cap"]):
        warnings.append(f"饋線數變動超過 10%（{len(old['cap'])} → {len(new['cap'])}）")

    added = sorted(set(new["cap"]) - set(old["cap"]))
    removed = sorted(set(old["cap"]) - set(new["cap"]))
    common = set(old["cap"]) & set(new["cap"])
    moved = {}
    for f in common:
        a, b = bin_of(old["cap"][f]), bin_of(new["cap"][f])
        if a != b:
            moved[(a, b)] = moved.get((a, b), 0) + 1

    lines = [
        f"### {cfg['name']}（{cfg['code']}，來自 {new_row['source_id']}）",
        "",
        "| | 目前上線 | 待審新版 |",
        "|---|---|---|",
        f"| 存檔版本 | {old['id']} | {new['id']} |",
        f"| 饋線數（容量表） | {len(old['cap'])} | {len(new['cap'])} |",
        f"| 公布為 0 | {sum(v <= 0 for v in old['cap'].values())}（{share0(old):.1%}） | {sum(v <= 0 for v in new['cap'].values())}（{share0(new):.1%}） |",
        f"| 最大值（kW） | {max(old['cap'].values()):,.0f} | {max(new['cap'].values()):,.0f} |",
        f"| 變壓器（位置表） | {old['points']:,} | {new['points']:,} |",
        "",
        f"- 饋線代號兩版皆有 {len(common)} 條；新增 {len(added)} 條：{', '.join(added) if added else '無'}；"
        f"消失 {len(removed)} 條：{', '.join(removed) if removed else '無'}",
        "- 兩版皆有的饋線，級距改變："
        + ("；".join(f"{a} → {b} {n} 條" for (a, b), n in sorted(moved.items(), key=lambda x: -x[1])) or "無"),
        "",
    ]
    return lines, [f"{cfg['name']}：{w}" for w in warnings]


def main() -> None:
    rows = {r["snapshot_id"]: r for r in csv.DictReader(MANIFEST.open(encoding="utf-8"))}
    if len(sys.argv) == 3:
        pairs = [tuple(sys.argv[1:])]
    else:
        approved = set(json.loads(PUBLISHED.read_text(encoding="utf-8"))["approved"])
        pairs = []
        for src in ("S02", "S01"):
            ids = sorted(s for s in rows if s.startswith(f"{src}-"))
            pending = [s for s in ids if s not in approved]
            if pending:
                pairs.append((max(s for s in ids if s in approved), pending[-1]))
        if not pairs:
            print("No pending S01/S02 snapshot.")
            return

    body, warnings = [], []
    for old_id, new_id in pairs:
        src = rows[new_id]["source_id"]
        for cfg in (c for c in COUNTIES if c["source"] == src):
            lines, warns = county_section(cfg, rows[old_id], rows[new_id])
            body += lines
            warnings += warns
    s01_new = any(rows[n]["source_id"] == "S01" for _, n in pairs)

    lines = [
        f"## 新版資料待審：{'、'.join(n for _, n in pairs)}",
        "",
        "> 審核用摘要，不對外發布。跨版可比性確認前，網站不顯示增減。",
        "",
        "### 需要注意" if warnings else "### 自動檢查",
        *([f"- ⚠️ {w}" for w in warnings] or ["- 未發現欄位改變、負值或異常幅度的變動。"]),
        *(["- 全國檔（S01）新版也會更新首頁「在全國的位置」與各縣的鄰縣補值。"] if s01_new else []),
        "",
        *body,
        "### 審核步驟",
        "",
        "1. 看上方警示與數字是否合理；必要時抽查幾條饋線與台電 FGIS 比對。",
        "2. 檢查 `docs/audit/` 下各縣審計報告的差異（座標驗證、全國檔交叉比對、縣界外的點）。",
        "3. 合併此 PR 即核准上線；若有疑慮，留言後不合併，原始檔已保存在 main。",
    ]
    print("\n".join(lines))


if __name__ == "__main__":
    main()
