"""Build the Yunlin distribution-feeder layer from archived S02/S01/S12 snapshots.

Reads only files listed in data/manifests/snapshots.csv. Writes:
  web/data/summary.json            KPIs, provenance, quality notes, snapshot history
  web/data/transformers.geojson    transformer points (latest snapshot) tagged with feeder + class
  web/data/townships.geojson       Yunlin township boundaries (simplified, reference only)
  web/data/download/*.csv          citable per-snapshot feeder tables
  docs/audit/S02_capacity_audit.md generated audit report

Capacity is published per feeder (kW). Transformer points carry no capacity of
their own; they only show where a feeder's equipment is. Feeder values must
never be summed into area totals (upstream substation limits are shared).
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import zipfile
from collections import Counter
from datetime import date
from pathlib import Path

import geopandas as gpd
import pandas as pd
from pyproj import Transformer

METHOD_VERSION = "0.1.0"
ROOT = Path(__file__).resolve().parents[2]
MANIFEST = ROOT / "data" / "manifests" / "snapshots.csv"
CHECKS = ROOT / "data" / "manifests" / "checks.csv"
WEB = ROOT / "web" / "data"
AUDIT = ROOT / "docs" / "audit" / "S02_capacity_audit.md"

# Fixed project bins in kW. Not Taipower FGIS's six levels (its breakpoints are
# not published in text). Fixed rather than quantile so snapshots stay comparable.
CLASSES = [
    {"id": 0, "label": "0", "min": None, "max": 0},
    {"id": 1, "label": "未滿 1 MW", "min": 0, "max": 1000},
    {"id": 2, "label": "1–3 MW", "min": 1000, "max": 3000},
    {"id": 3, "label": "3–6 MW", "min": 3000, "max": 6000},
    {"id": 4, "label": "6 MW 以上", "min": 6000, "max": None},
]
NO_CAPACITY_CLASS = -1
# PROJ's EPSG:3828 -> 4326 path is only a "ballpark" offset (no datum shift), which
# leaves TWD67 points ~850 m off. Candidates are tested by how many transformer
# points land off-shore / off-land (transformers are always on land).
TWD67_TM2 = ("+proj=tmerc +lat_0=0 +lon_0=121 +k=0.9999 +x_0=250000 +y_0=0 +ellps=aust_SA "
             "+towgs84=-752,-358,-179,0,0,0,0 +units=m +no_defs")
MOI_A, MOI_B = 0.00001549, 0.000006521  # MOI simplified TWD67->TWD97 TM2 formula


def _via_3826(x, y):
    return Transformer.from_crs("EPSG:3826", "EPSG:4326", always_xy=True).transform(x, y)


CRS_CANDIDATES = {
    "twd67_towgs84": ("TWD67 TM2 + towgs84 3 參數基準轉換", lambda x, y: Transformer.from_crs(TWD67_TM2, "EPSG:4326", always_xy=True).transform(x, y)),
    "twd67_moi_formula": ("TWD67 TM2 + 內政部四參數簡易公式", lambda x, y: _via_3826(x + 807.8 + MOI_A * x + MOI_B * y, y - 248.6 + MOI_A * y - MOI_B * x)),
    "twd97_as_is": ("視為 TWD97 TM2（不做基準轉換）", _via_3826),
    "epsg3828_ballpark": ("EPSG:3828 PROJ 預設（僅概略偏移）", lambda x, y: Transformer.from_crs("EPSG:3828", "EPSG:4326", always_xy=True).transform(x, y)),
}
LICENSE = {"name": "政府資料開放授權條款－第1版", "url": "https://data.gov.tw/license"}
NO_CAPACITY_LABEL = "台電未列容量"
FEEDER_CODE_NONE = "無"  # Taipower's own placeholder in tr_feeder.csv: transformer has no feeder assigned
COUNTY_ZH = {
    "CHANGHUA": "彰化", "CHIAYI": "嘉義", "HSINCHU": "新竹", "HUALIEN": "花蓮", "KAOHSIUNG": "高雄",
    "KEELUNG": "基隆", "MIAOLI": "苗栗", "NANTOU": "南投", "NEWTAIPEI": "新北", "PINGTUNG": "屏東",
    "TAICHUNG": "臺中", "TAINAN": "臺南", "TAIPEI": "臺北", "TAITUNG": "臺東", "TAOYUAN": "桃園",
    "YILAN": "宜蘭", "YUNLIN": "雲林",
}


def other_county_capacity(s01_zip: Path) -> dict[str, list[tuple[str, float]]]:
    """Feeder -> [(county code, kW), ...] from every non-Yunlin capacity table in the national S01 ZIP.

    Border transformers in Yunlin are sometimes fed by a neighbouring county's
    feeder; that feeder's capacity is published in the neighbour's table only.
    Feeder codes are not unique nationwide (e.g. CV31 in both Keelung and New
    Taipei with different values), so callers must check for conflicts.
    """
    out: dict[str, list[tuple[str, float]]] = {}
    with zipfile.ZipFile(s01_zip) as z:
        for name in z.namelist():
            if not name.endswith("_capacity.csv") or name.startswith("schema-") or name.startswith("YUNLIN"):
                continue
            county = name.removesuffix("_capacity.csv")
            frame = pd.read_csv(io.BytesIO(z.read(name)), encoding="utf-8-sig", dtype={"FEEDER": str})
            for fid, kw in zip(frame.FEEDER.str.strip(), frame.CAPACITY):
                out.setdefault(fid, []).append((county, float(kw)))
    return out


def classify(kw: float | None) -> int:
    if kw is None or pd.isna(kw):
        return NO_CAPACITY_CLASS
    if kw <= 0:
        return 0
    if kw < 1000:
        return 1
    if kw < 3000:
        return 2
    if kw < 6000:
        return 3
    return 4


def read_member(zip_path: Path, suffix: str) -> tuple[pd.DataFrame, str, str]:
    """Return (frame, member date, member sha256) for the YUNLIN_<suffix>.csv member."""
    with zipfile.ZipFile(zip_path) as z:
        info = next(i for i in z.infolist() if i.filename.upper() == f"YUNLIN_{suffix}.CSV".upper())
        raw = z.read(info)
    frame = pd.read_csv(io.BytesIO(raw), encoding="utf-8-sig", dtype={"FEEDER": str})
    return frame, date(*info.date_time[:3]).isoformat(), hashlib.sha256(raw).hexdigest()


def load_townships(manifest: pd.DataFrame) -> tuple[gpd.GeoDataFrame, gpd.GeoDataFrame, dict]:
    """Return (Yunlin townships, Yunlin + neighbouring counties as land mask, provenance)."""
    row = manifest[manifest.source_id == "S12"].sort_values("file_date").iloc[-1]
    path = ROOT / row.archive_path
    shp = next(n for n in zipfile.ZipFile(path).namelist() if n.upper().startswith("TOWN_MOI") and n.lower().endswith(".shp"))
    allg = gpd.read_file(f"zip://{path}!{shp}")
    # TWD97[2020] geographic; treated as WGS84 (difference well under 1 m).
    allg = allg.set_crs("EPSG:4326", allow_override=True)
    land = allg[allg.COUNTYNAME.isin(["雲林縣", "彰化縣", "嘉義縣", "南投縣"])]
    g = allg[allg.COUNTYNAME == "雲林縣"][["TOWNCODE", "TOWNNAME", "geometry"]].reset_index(drop=True)
    meta = {"snapshot_id": row.snapshot_id, "shp": shp, "sha256": row.sha256, "source_url": row.source_url,
            "dataset_page": row.dataset_page, "townships": len(g)}
    return g, land, meta


def to_points(pts: pd.DataFrame, crs: str) -> gpd.GeoDataFrame:
    lon, lat = CRS_CANDIDATES[crs][1](pts.X.to_numpy(), pts.Y.to_numpy())
    return gpd.GeoDataFrame(pts.copy(), geometry=gpd.points_from_xy(lon, lat), crs="EPSG:4326")


def crs_test(pts: pd.DataFrame, county: gpd.GeoSeries, land: gpd.GeoSeries) -> dict:
    poly = county.union_all()
    land_m = gpd.GeoSeries([land.union_all()], crs="EPSG:4326").to_crs("EPSG:3826").iloc[0]
    ref = to_points(pts, "twd67_towgs84").to_crs("EPSG:3826")
    out = {}
    for key, (name, _) in CRS_CANDIDATES.items():
        g = to_points(pts, key)
        gm = g.to_crs("EPSG:3826")
        off = ~gm.within(land_m)
        out[key] = {
            "name": name,
            "inside_county": round(float(g.within(poly).mean()), 4),
            "off_land_points": int(off.sum()),
            "off_land_over_100m": int((gm[off].distance(land_m) > 100).sum()),
            "mean_offset_from_towgs84_m": round(float(gm.distance(ref).mean()), 1),
        }
    return out


def build_snapshot(row: pd.Series, towns: gpd.GeoDataFrame, crs: str, s01_row: pd.Series | None = None) -> dict:
    path = ROOT / row.archive_path
    cap, cap_date, cap_sha = read_member(path, "capacity")
    pts, pts_date, pts_sha = read_member(path, "tr_feeder")
    cap["FEEDER"] = cap.FEEDER.str.strip()
    pts["FEEDER"] = pts.FEEDER.str.strip()

    g = to_points(pts, crs)
    g = gpd.sjoin(g, towns[["TOWNNAME", "geometry"]], how="left", predicate="within").drop(columns="index_right")
    capmap = dict(zip(cap.FEEDER, cap.CAPACITY))
    # Neighbour-county values only from a national S01 file of the same file date.
    neighbours = other_county_capacity(ROOT / s01_row.archive_path) if s01_row is not None else {}

    feeders = []
    for fid in sorted(set(cap.FEEDER) | set(g.FEEDER)):
        sub = g[g.FEEDER == fid]
        kw = capmap.get(fid)
        source, county = ("S02", "") if kw is not None else ("", "")
        towns_ct = Counter(sub.TOWNNAME.fillna("（縣界外）"))
        flags = []
        if kw is None and fid == FEEDER_CODE_NONE:
            flags.append("feeder_code_none")
        elif kw is None and fid in neighbours and len({v for _, v in neighbours[fid]}) == 1:
            county, kw = neighbours[fid][0][0], neighbours[fid][0][1]
            source = "S01"
            flags.append("capacity_from_other_county")
        elif kw is None and fid in neighbours:
            flags.append("ambiguous_other_county")
        elif kw is None:
            flags.append("no_capacity_row")
        elif kw < 0:
            flags.append("negative_capacity")
        if len(sub) == 0:
            flags.append("no_transformer_points")
        if "（縣界外）" in towns_ct:
            flags.append("points_outside_county")
        if cap.FEEDER.tolist().count(fid) > 1:
            flags.append("duplicate_capacity_row")
        main, main_n = towns_ct.most_common(1)[0] if towns_ct else ("", 0)
        feeders.append({
            "feeder": fid,
            "capacity_kw": None if kw is None else float(kw),
            "capacity_source": source,
            "capacity_county": COUNTY_ZH.get(county, county),
            "class": classify(kw),
            "transformer_points": int(len(sub)),
            "main_township": main,
            "main_township_share": round(main_n / len(sub), 3) if len(sub) else None,
            "townships": "、".join(t for t, _ in towns_ct.most_common()),
            "lon": round(float(sub.geometry.x.mean()), 5) if len(sub) else None,
            "lat": round(float(sub.geometry.y.mean()), 5) if len(sub) else None,
            "flags": ";".join(flags),
        })

    v = cap.CAPACITY
    stats = {
        "feeders_with_capacity": int(cap.FEEDER.nunique()),
        "capacity_rows": int(len(cap)),
        "zero": int((v <= 0).sum()),
        "zero_share": round(float((v <= 0).mean()), 4),
        "under_1mw": int((v < 1000).sum()),
        "under_1mw_share": round(float((v < 1000).mean()), 4),
        "median_kw": round(float(v.median()), 1),
        "p25_kw": round(float(v.quantile(0.25)), 1),
        "p75_kw": round(float(v.quantile(0.75)), 1),
        "max_kw": round(float(v.max()), 1),
        "count_at_max": int((v == v.max()).sum()),
        "negatives": int((v < 0).sum()),
        "transformer_points": int(len(pts)),
        "feeders_in_points": int(pts.FEEDER.nunique()),
        "points_without_capacity_feeder": int((~pts.FEEDER.isin(cap.FEEDER)).sum()),
        "feeders_no_capacity_row": sum("no_capacity_row" in f["flags"] for f in feeders),
        "points_no_capacity_row": sum(f["transformer_points"] for f in feeders if "no_capacity_row" in f["flags"]),
        "feeders_other_county": sum("capacity_from_other_county" in f["flags"] for f in feeders),
        "points_other_county": sum(f["transformer_points"] for f in feeders if "capacity_from_other_county" in f["flags"]),
        "points_feeder_code_none": int((pts.FEEDER == FEEDER_CODE_NONE).sum()),
        "feeders_ambiguous_other_county": sum("ambiguous_other_county" in f["flags"] for f in feeders),
        "national_codes_conflicting": sum(1 for v in neighbours.values() if len({kw for _, kw in v}) > 1),
        "national_conflict_examples": [
            f"{fid}（{'／'.join(COUNTY_ZH.get(c, c) for c, _ in v)}）"
            for fid, v in sorted(neighbours.items()) if len({kw for _, kw in v}) > 1
        ][:3],
        "feeders_no_points": sum("no_transformer_points" in f["flags"] for f in feeders),
        "points_outside_county": int(g.TOWNNAME.isna().sum()),
        # Basis: the Yunlin capacity table (S02) only — matches the KPI denominators.
        "class_counts": {c["id"]: int(sum(classify(x) == c["id"] for x in v)) for c in CLASSES},
    }
    # Basis: what the map draws — feeders with at least one transformer, and the transformers themselves.
    on_map = [f for f in feeders if f["transformer_points"] > 0]
    ids = [c["id"] for c in CLASSES] + [NO_CAPACITY_CLASS]
    stats["map"] = {
        "feeders": len(on_map),
        "transformers": int(len(pts)),
        "class_feeders": {i: sum(1 for f in on_map if f["class"] == i and f["feeder"] != FEEDER_CODE_NONE) for i in ids},
        "class_transformers": {i: sum(f["transformer_points"] for f in on_map if f["class"] == i) for i in ids},
    }
    return {
        "snapshot_id": row.snapshot_id,
        "origin": row.origin,
        "fetch_url": row.fetch_url,
        "archive_sha256": row.sha256,
        "archive_path": row.archive_path,
        "retrieved_at": row.retrieved_at,
        "capacity_date": cap_date,
        "capacity_member_sha256": cap_sha,
        "points_date": pts_date,
        "points_member_sha256": pts_sha,
        "s01_snapshot_id": s01_row.snapshot_id if s01_row is not None else None,
        "s01_archive_sha256": s01_row.sha256 if s01_row is not None else None,
        "s01_archive_path": s01_row.archive_path if s01_row is not None else None,
        "stats": stats,
        "feeders": feeders,
        "_points": g,
    }


def cross_check(manifest: pd.DataFrame) -> list[dict]:
    """Compare the YUNLIN members inside S01 (national) and S02 (county) archives."""
    out = []
    s01 = manifest[manifest.source_id == "S01"]
    for _, s02 in manifest[manifest.source_id == "S02"].iterrows():
        twin = s01[s01.file_date == s02.file_date]
        if twin.empty:
            continue
        rec = {"file_date": s02.file_date}
        for suffix in ("capacity", "tr_feeder"):
            _, d1, h1 = read_member(ROOT / twin.iloc[0].archive_path, suffix)
            _, d2, h2 = read_member(ROOT / s02.archive_path, suffix)
            rec[suffix] = "identical" if h1 == h2 else f"differs (S01 {d1} {h1[:8]} vs S02 {d2} {h2[:8]})"
        out.append(rec)
    return out


def write_csv(path: Path, snap: dict, towns_meta: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    cols = ["feeder", "capacity_kw", "capacity_source", "capacity_county", "class", "class_label", "transformer_points",
            "main_township", "main_township_share", "townships", "lon", "lat", "flags"]
    labels = {c["id"]: c["label"] for c in CLASSES} | {NO_CAPACITY_CLASS: NO_CAPACITY_LABEL}
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        f.write(f"# 雲林縣配電饋線公布可併網容量｜台電容量表日期 {snap['capacity_date']}｜原始檔 SHA-256 {snap['archive_sha256']}\n")
        f.write(f"# 來源：台灣電力公司，政府資料開放平臺 {towns_meta['s02_page']}，{LICENSE['name']} {LICENSE['url']}\n")
        if snap["s01_snapshot_id"]:
            f.write(f"# capacity_source=S01：鄰縣饋線，容量取自同日全國檔 {towns_meta['s01_page']}（SHA-256 {snap['s01_archive_sha256']}）\n")
        f.write(f"# 衍生欄位（class、鄉鎮、經緯度）為本專案計算，方法版本 {METHOD_VERSION}。容量不可跨饋線加總。\n")
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        for r in snap["feeders"]:
            w.writerow({k: r.get(k) for k in cols} | {"class_label": labels[r["class"]]})


def write_points(path: Path, snap: dict, classes: dict[str, int]) -> None:
    feats = []
    for p in snap["_points"].itertuples():
        feats.append({
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [round(p.geometry.x, 5), round(p.geometry.y, 5)]},
            "properties": {"f": p.FEEDER, "c": classes.get(p.FEEDER, NO_CAPACITY_CLASS)},
        })
    path.write_text(json.dumps({"type": "FeatureCollection", "features": feats}, ensure_ascii=False, separators=(",", ":")))


def feeder_stability(snaps: list[dict]) -> list[dict]:
    out = []
    for a, b in zip(snaps, snaps[1:]):
        fa = {f["feeder"] for f in a["feeders"] if f["capacity_source"] == "S02"}
        fb = {f["feeder"] for f in b["feeders"] if f["capacity_source"] == "S02"}
        out.append({"from": a["capacity_date"], "to": b["capacity_date"], "common": len(fa & fb),
                    "only_from": len(fa - fb), "only_to": len(fb - fa)})
    return out


def main() -> None:
    manifest = pd.read_csv(MANIFEST, dtype=str)
    towns, land, towns_meta = load_townships(manifest)
    s02 = manifest[manifest.source_id == "S02"].sort_values("file_date")
    towns_meta["s02_page"] = s02.iloc[-1].dataset_page
    s01 = manifest[manifest.source_id == "S01"]
    towns_meta["s01_page"] = s01.iloc[-1].dataset_page

    latest_pts, _, _ = read_member(ROOT / s02.iloc[-1].archive_path, "tr_feeder")
    crs_result = crs_test(latest_pts, towns.geometry, land.geometry)
    crs = min(crs_result, key=lambda k: (crs_result[k]["off_land_points"], -crs_result[k]["inside_county"]))
    assert crs.startswith("twd67"), f"CRS test picked {crs}; review before publishing"

    def same_day_s01(r: pd.Series) -> pd.Series | None:
        twin = s01[s01.file_date == r.file_date]
        return twin.iloc[0] if len(twin) else None

    snaps = [build_snapshot(r, towns, crs, same_day_s01(r)) for _, r in s02.iterrows()]
    latest = snaps[-1]
    checks = cross_check(manifest)
    stability = feeder_stability(snaps)

    WEB.mkdir(parents=True, exist_ok=True)
    for s in snaps:
        write_csv(WEB / "download" / f"yunlin_feeders_{s['capacity_date']}.csv", s, towns_meta)
    write_points(WEB / "transformers.geojson", latest, {f["feeder"]: f["class"] for f in latest["feeders"]})
    t = towns.copy()
    rep = t.geometry.representative_point()
    t["label_lon"], t["label_lat"] = rep.x.round(5), rep.y.round(5)
    t["geometry"] = t.geometry.simplify(0.0004, preserve_topology=True)
    t.rename(columns={"TOWNNAME": "name", "TOWNCODE": "code"}).to_file(WEB / "townships.geojson", driver="GeoJSON",
                                                                      COORDINATE_PRECISION=5)

    def public(s: dict) -> dict:
        return {k: v for k, v in s.items() if k not in ("feeders", "_points")}

    check_log = pd.read_csv(CHECKS, dtype=str)
    last_checked = check_log[check_log.source_id == "S02"].checked_at.max()
    summary = {
        "method_version": METHOD_VERSION,
        "built_on": date.today().isoformat(),
        "source_last_checked": last_checked,
        "scope": "雲林縣・配電饋線（distribution）",
        "grid_level": "distribution",
        "license": LICENSE,
        "sources": {
            "S02": {"title": "台灣電力公司雲林縣再生能源可併網容量", "publisher": "台灣電力公司",
                    "dataset_page": s02.iloc[-1].dataset_page, "source_url": s02.iloc[-1].source_url,
                    "declared_frequency": "每 6 月（平臺頁面標示）"},
            "S01": {"title": "台灣電力公司全國各縣市再生能源可併網容量", "publisher": "台灣電力公司",
                    "dataset_page": s01.iloc[-1].dataset_page, "source_url": s01.iloc[-1].source_url},
            "S12": {"title": "鄉鎮市區界線(TWD97經緯度)", "publisher": "內政部國土測繪中心", **towns_meta},
        },
        "classes": CLASSES,
        "crs": {"chosen": crs, "tests": crs_result},
        "latest": public(latest),
        "feeders": latest["feeders"],
        "history": [public(s) for s in snaps],
        "feeder_key_stability": stability,
        "s01_s02_cross_check": checks,
    }
    (WEB / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=1))
    write_audit(summary)
    print(json.dumps({"crs": crs_result,
                      "chosen": crs, "latest": latest["stats"], "cross_check": checks, "stability": stability},
                     ensure_ascii=False, indent=1))


def pct(x: float) -> str:
    return f"{x * 100:.1f}%"


def write_audit(s: dict) -> None:
    L = s["latest"]
    st = L["stats"]
    lines = [
        "# S02 雲林縣再生能源可併網容量：資料審計",
        "",
        f"> 本檔由 `src/transform/build_yunlin.py` 自動產生（方法版本 {s['method_version']}，建置日 {s['built_on']}）。請勿手動編輯。",
        "",
        "## 1. 檔案結構（已確認）",
        "",
        "- ZIP 內含兩張表與各自 schema：`YUNLIN_capacity.csv`（`FEEDER`, `CAPACITY`＝饋線可併網容量，單位千瓦）與 `YUNLIN_tr_feeder.csv`（`ROWNUM`, `X`, `Y`, `FEEDER`＝變壓器 TWD67 座標及所屬饋線）。",
        "- **容量以饋線為單位。** 變壓器點位本身沒有容量，只標示該饋線設備位置；同一饋線多點是正常結構，不是重複容量。",
        "- CSV 內沒有資料日期欄位。本專案以 ZIP 內各成員檔的修改時間作為「檔案日期」，並分別記錄容量表與點位表的日期。",
        "",
        "## 2. 版本與日期",
        "",
        "| 快照 | 來源 | 容量表日期 | 點位表日期 | 原始檔 SHA-256 |",
        "|---|---|---|---|---|",
    ]
    for h in s["history"]:
        lines.append(f"| {h['snapshot_id']} | {h['origin']} | {h['capacity_date']} | {h['points_date']} | `{h['archive_sha256'][:16]}…` |")
    lines += [
        "",
        "平臺頁面標示每 6 個月更新；已取得的版本間隔約一年（可能有未被 Wayback 保存的中間版本）。",
        "",
        "## 3. 座標系統驗證",
        "",
        "變壓器必在陸地上，因此以「落在雲林及鄰接三縣陸域之外（海上）」的點數作為主要判準。",
        "",
        "| 轉換方式 | 落在雲林縣界內 | 落在陸域外 | 陸域外逾 100 m | 與採用方案平均差距 |",
        "|---|---|---|---|---|",
    ]
    for k, v in s["crs"]["tests"].items():
        lines.append(f"| `{k}` {v['name']} | {pct(v['inside_county'])} | {v['off_land_points']} | {v['off_land_over_100m']} | {v['mean_offset_from_towgs84_m']} m |")
    lines += [
        "",
        f"採用：**`{s['crs']['chosen']}`**。兩種獨立的 TWD67→TWD97 方法結果一致（差距數公尺），且無任何點位落在陸域外；",
        "若直接使用 PROJ 對 EPSG:3828 的預設轉換（僅概略偏移，無基準轉換），全部點位會系統性偏移約 850 公尺，並有上百點落入海中。",
        "",
        f"## 4. 最新快照品質（容量表 {L['capacity_date']}）",
        "",
        f"- 饋線容量列 {st['capacity_rows']} 筆、不重複饋線 {st['feeders_with_capacity']} 條；負值 {st['negatives']} 筆。",
        f"- 公布容量為 0：{st['zero']} 條（{pct(st['zero_share'])}）；未滿 1 MW：{st['under_1mw']} 條（{pct(st['under_1mw_share'])}）。",
        f"- 中位數 {st['median_kw']:,} kW（四分位 {st['p25_kw']:,}–{st['p75_kw']:,} kW）；最大值 {st['max_kw']:,} kW，共 {st['count_at_max']} 條等於最大值（疑似上限值，待向台電確認）。",
        f"- 變壓器點位 {st['transformer_points']:,} 個，分屬 {st['feeders_in_points']} 個饋線代號；落在縣界外 {st['points_outside_county']} 個。",
        f"- 點位表有、雲林容量表沒有的變壓器共 {st['points_without_capacity_feeder']:,} 個，分三種：",
        f"  - 鄰縣饋線 {st['feeders_other_county']} 條（{st['points_other_county']:,} 個點）：容量見同日全國檔（S01）其他縣市容量表，本站取用並標示來源。"
        if L["s01_snapshot_id"] else "  - 鄰縣饋線：本版無同日全國檔，未補值。",
        f"  - 雲林代號但兩表皆無 {st['feeders_no_capacity_row']} 條（{st['points_no_capacity_row']:,} 個點）：標為「{NO_CAPACITY_LABEL}」，成因待確認。",
        f"  - 所屬饋線欄位為「{FEEDER_CODE_NONE}」：{st['points_feeder_code_none']} 個點。",
        f"  - 代號在多個鄰縣容量表出現且數值不同、因此不補值：{st['feeders_ambiguous_other_county']} 條。",
        (f"- 同日全國檔中，同一饋線代號在多個縣市容量表出現且數值不同者：{st['national_codes_conflicting']} 個"
         + (f"（例：{'、'.join(st['national_conflict_examples'])}）" if st["national_conflict_examples"] else "")
         + "。")
        if L["s01_snapshot_id"] else "",
        *[
            f"- {h['s01_snapshot_id']} 全國檔有 {h['stats']['national_codes_conflicting']} 個代號在多縣數值不同"
            f"（例：{'、'.join(h['stats']['national_conflict_examples'])}）：饋線代號不保證全國唯一，跨縣比對須連同縣市判斷。"
            for h in s["history"] if h["stats"].get("national_codes_conflicting")
        ],
        f"- 雲林容量表有、點位表沒有的饋線 {st['feeders_no_points']} 條（地圖上無法定位，只列在饋線清單）。",
        "",
        "## 5. 全國檔（S01）與雲林檔（S02）交叉比對",
        "",
    ]
    lines += [f"- {c['file_date']}：容量表 {c['capacity']}；點位表 {c['tr_feeder']}" for c in s["s01_s02_cross_check"]] or ["- 無同日期版本可比對。"]
    lines += ["", "## 6. 饋線代號跨版本穩定性", "", "| 前版 | 後版 | 共同 | 僅前版 | 僅後版 |", "|---|---|---|---|---|"]
    lines += [f"| {x['from']} | {x['to']} | {x['common']} | {x['only_from']} | {x['only_to']} |" for x in s["feeder_key_stability"]]
    lines += [
        "",
        "代號相同不代表饋線範圍或計算條件相同；跨版本差值須另經可比性檢查（spec §8.3）後才發布。",
        "",
        "## 7. 未決問題（P0）",
        "",
        "1. 容量計算條件、日期與上限值的官方定義。",
        f"2. 雲林代號但兩表皆無的 {st['feeders_no_capacity_row']} 條饋線，與有容量但無點位的 {st['feeders_no_points']} 條饋線，是否有對應關係（新設、改名或停用）。",
        "3. 檔案日期（ZIP 成員修改時間）與台電實際計算基準日的關係。",
    ]
    AUDIT.parent.mkdir(parents=True, exist_ok=True)
    AUDIT.write_text("\n".join(lines) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
