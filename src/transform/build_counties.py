"""Build the distribution-feeder layer of every county in counties.py from archived S01/S02/S12 snapshots.

Reads only approved files listed in data/manifests/snapshots.csv. Writes:
  web/data/summary.json                        site-wide provenance, archive list, national comparison, per-county stats
  web/data/<slug>/feeders.json                 feeder table (latest snapshot)
  web/data/<slug>/transformers.geojson         transformer points (latest snapshot) tagged with feeder + class
  web/data/<slug>/townships.geojson            township shapes (simplified; names, labels, map extent)
  web/data/<slug>/town_lines.geojson           boundaries between townships, drawn on the map
  web/data/download/<slug>_feeders_<date>.csv  citable per-snapshot feeder tables
  docs/audit/<slug>_capacity_audit.md          generated audit report per county

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
import shapely

from counties import COUNTIES, DEFAULT_COUNTY

METHOD_VERSION = "0.3.0"  # 0.3.0: 彰化、嘉義 added from S01; 0.2.0: neighbour-county feeders filled from same-day S01; 台電未列容量 split out
ROOT = Path(__file__).resolve().parents[2]
MANIFEST = ROOT / "data" / "manifests" / "snapshots.csv"
CHECKS = ROOT / "data" / "manifests" / "checks.csv"
PUBLISHED = ROOT / "data" / "manifests" / "published.json"
WEB = ROOT / "web" / "data"
AUDIT_DIR = ROOT / "docs" / "audit"

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
FAR_OUTSIDE_M = 5_000  # a point this far outside its county is misfiled, not a projection error


def _via_3826(x, y):
    return Transformer.from_crs("EPSG:3826", "EPSG:4326", always_xy=True).transform(x, y)


CRS_CANDIDATES = {
    "twd67_towgs84": ("TWD67 TM2 + towgs84 3 參數基準轉換", lambda x, y: Transformer.from_crs(TWD67_TM2, "EPSG:4326", always_xy=True).transform(x, y)),
    "twd67_moi_formula": ("TWD67 TM2 + 內政部四參數簡易公式", lambda x, y: _via_3826(x + 807.8 + MOI_A * x + MOI_B * y, y - 248.6 + MOI_A * y - MOI_B * x)),
    "twd97_as_is": ("視為 TWD97 TM2（不做基準轉換）", _via_3826),
    "epsg3828_ballpark": ("EPSG:3828 PROJ 預設（僅概略偏移）", lambda x, y: Transformer.from_crs("EPSG:3828", "EPSG:4326", always_xy=True).transform(x, y)),
}
LICENSE = {"name": "政府資料開放授權條款－第1版", "url": "https://data.gov.tw/license"}
SOURCES = {
    "S01": {"title": "台灣電力公司全國各縣市再生能源可併網容量", "short": "台電全國各縣市再生能源可併網容量"},
    "S02": {"title": "台灣電力公司雲林縣再生能源可併網容量", "short": "台電雲林縣再生能源可併網容量"},
}
NO_CAPACITY_LABEL = "台電未列容量"
FEEDER_CODE_NONE = "無"  # Taipower's own placeholder in tr_feeder.csv: transformer has no feeder assigned
OUTSIDE = "（縣界外）"
COUNTY_ZH = {
    "CHANGHUA": "彰化", "CHIAYI": "嘉義", "HSINCHU": "新竹", "HUALIEN": "花蓮", "KAOHSIUNG": "高雄",
    "KEELUNG": "基隆", "MIAOLI": "苗栗", "NANTOU": "南投", "NEWTAIPEI": "新北", "PINGTUNG": "屏東",
    "TAICHUNG": "臺中", "TAINAN": "臺南", "TAIPEI": "臺北", "TAITUNG": "臺東", "TAOYUAN": "桃園",
    "YILAN": "宜蘭", "YUNLIN": "雲林",
}


# Administrative coverage of each national file, checked by placing every file's
# transformers on MOI county boundaries (2026-10-12): CHIAYI = 嘉義縣 89.6% + 嘉義市 10.3%,
# HSINCHU = 新竹縣 76.1% + 新竹市 23.7%, all others ≥ 99.8% one county.
# Penghu, Kinmen and Lienchiang (separate island grids) are not in the files.
COUNTY_DISPLAY = {
    "CHANGHUA": "彰化縣", "CHIAYI": "嘉義縣市", "HSINCHU": "新竹縣市", "HUALIEN": "花蓮縣", "KAOHSIUNG": "高雄市",
    "KEELUNG": "基隆市", "MIAOLI": "苗栗縣", "NANTOU": "南投縣", "NEWTAIPEI": "新北市", "PINGTUNG": "屏東縣",
    "TAICHUNG": "臺中市", "TAINAN": "臺南市", "TAIPEI": "臺北市", "TAITUNG": "臺東縣", "TAOYUAN": "桃園市",
    "YILAN": "宜蘭縣", "YUNLIN": "雲林縣",
}
NOT_IN_NATIONAL_FILE = ["澎湖縣", "金門縣", "連江縣"]


def national_stats(s01_row: pd.Series) -> dict:
    """Per-county feeder statistics from one national file: same date and definition for every county."""
    counties, dates = [], set()
    with zipfile.ZipFile(ROOT / s01_row.archive_path) as z:
        for info in z.infolist():
            name = info.filename
            if not name.endswith("_capacity.csv") or name.startswith("schema-"):
                continue
            code = name.removesuffix("_capacity.csv")
            dates.add(date(*info.date_time[:3]).isoformat())
            v = pd.read_csv(io.BytesIO(z.read(name)), encoding="utf-8-sig", dtype={"FEEDER": str}).CAPACITY
            transformers = sum(1 for _ in z.open(f"{code}_tr_feeder.csv")) - 1
            counties.append({
                "code": code, "name": COUNTY_DISPLAY.get(code, code), "feeders": int(len(v)),
                "zero": int((v <= 0).sum()), "zero_share": round(float((v <= 0).mean()), 4),
                "under_1mw_share": round(float((v < 1000).mean()), 4), "median_kw": round(float(v.median()), 1),
                "transformers": transformers,
            })
    return {
        "snapshot_id": s01_row.snapshot_id,
        "capacity_dates": sorted(dates),
        "counties": sorted(counties, key=lambda c: (-c["zero_share"], c["name"])),
        "not_included": NOT_IN_NATIONAL_FILE,
    }


def other_county_capacity(s01_zip: Path, exclude: str) -> dict[str, list[tuple[str, float]]]:
    """Feeder -> [(county code, kW), ...] from every capacity table in the national S01 ZIP except `exclude`'s.

    Border transformers are sometimes fed by a neighbouring county's feeder; that
    feeder's capacity is published in the neighbour's table only. Feeder codes are
    not unique nationwide (e.g. CV31 in both Keelung and New Taipei with different
    values), so callers must check for conflicts.
    """
    out: dict[str, list[tuple[str, float]]] = {}
    with zipfile.ZipFile(s01_zip) as z:
        for name in z.namelist():
            if not name.endswith("_capacity.csv") or name.startswith("schema-") or name.startswith(f"{exclude}_"):
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


def read_member(zip_path: Path, code: str, suffix: str) -> tuple[pd.DataFrame, str, str]:
    """Return (frame, member date, member sha256) for the <code>_<suffix>.csv member."""
    with zipfile.ZipFile(zip_path) as z:
        info = next(i for i in z.infolist() if i.filename.upper() == f"{code}_{suffix}.CSV".upper())
        raw = z.read(info)
    frame = pd.read_csv(io.BytesIO(raw), encoding="utf-8-sig", dtype={"FEEDER": str})
    return frame, date(*info.date_time[:3]).isoformat(), hashlib.sha256(raw).hexdigest()


def load_townships(manifest: pd.DataFrame) -> tuple[gpd.GeoDataFrame, dict]:
    """Return (all MOI townships, provenance)."""
    row = manifest[manifest.source_id == "S12"].sort_values("file_date").iloc[-1]
    path = ROOT / row.archive_path
    shp = next(n for n in zipfile.ZipFile(path).namelist() if n.upper().startswith("TOWN_MOI") and n.lower().endswith(".shp"))
    allg = gpd.read_file(f"zip://{path}!{shp}")
    # TWD97[2020] geographic; treated as WGS84 (difference well under 1 m).
    allg = allg.set_crs("EPSG:4326", allow_override=True)
    meta = {"snapshot_id": row.snapshot_id, "shp": shp, "sha256": row.sha256, "source_url": row.source_url,
            "dataset_page": row.dataset_page}
    return allg, meta


def county_towns(allg: gpd.GeoDataFrame, cfg: dict) -> gpd.GeoDataFrame:
    g = allg[allg.COUNTYNAME.isin(cfg["moi"])].copy()
    # A file covering county and city (CHIAYI): prefix the city's districts (嘉義市東區) so names stay unambiguous.
    if len(cfg["moi"]) > 1:
        g["TOWNNAME"] = [t if c == cfg["moi"][0] else c + t for c, t in zip(g.COUNTYNAME, g.TOWNNAME)]
    return g[["TOWNCODE", "TOWNNAME", "geometry"]].reset_index(drop=True)


def all_land(allg: gpd.GeoDataFrame):
    """Union of every MOI township in EPSG:3826, prepared for fast point-in-polygon tests."""
    land = allg.to_crs("EPSG:3826").union_all()
    shapely.prepare(land)
    return land


def display_towns(towns: gpd.GeoDataFrame, points: gpd.GeoDataFrame) -> tuple[gpd.GeoDataFrame, int]:
    """Township shapes for the map: drop detached parts with no transformers (each township keeps its largest part).

    MOI boundaries include offshore sandbars whose legal outline no longer matches where
    the sand is (外傘頂洲 off 口湖, 20 km² in the 2025 file); drawn on a current basemap
    they look misplaced. Township assignment still uses the full boundaries.
    """
    shown, dropped = towns.copy(), 0
    for i, geom in towns.geometry.items():
        parts = list(getattr(geom, "geoms", [geom]))
        if len(parts) == 1:
            continue
        largest = max(parts, key=lambda g: g.area)
        keep = [g for g in parts if g is largest or len(points.sindex.query(g, predicate="intersects"))]
        dropped += len(parts) - len(keep)
        shown.loc[i, "geometry"] = keep[0] if len(keep) == 1 else shapely.MultiPolygon(keep)
    return shown, dropped


def to_points(pts: pd.DataFrame, crs: str) -> gpd.GeoDataFrame:
    lon, lat = CRS_CANDIDATES[crs][1](pts.X.to_numpy(), pts.Y.to_numpy())
    return gpd.GeoDataFrame(pts.copy(), geometry=gpd.points_from_xy(lon, lat), crs="EPSG:4326")


def crs_test(pts: pd.DataFrame, county: gpd.GeoSeries, land_m) -> dict:
    poly = county.union_all()
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


def own_listed(f: dict) -> bool:
    """Listed in the county's own capacity table (not filled from a neighbour, not missing)."""
    return f["capacity_kw"] is not None and not f["capacity_county"]


def build_snapshot(row: pd.Series, cfg: dict, towns: gpd.GeoDataFrame, crs: str, s01_row: pd.Series | None = None) -> dict:
    path = ROOT / row.archive_path
    cap, cap_date, cap_sha = read_member(path, cfg["code"], "capacity")
    pts, pts_date, pts_sha = read_member(path, cfg["code"], "tr_feeder")
    cap["FEEDER"] = cap.FEEDER.str.strip()
    pts["FEEDER"] = pts.FEEDER.str.strip()

    g = to_points(pts, crs)
    county_m = towns.to_crs("EPSG:3826").union_all()
    far = g.to_crs("EPSG:3826").distance(county_m) > FAR_OUTSIDE_M
    g = gpd.sjoin(g, towns[["TOWNNAME", "geometry"]], how="left", predicate="within").drop(columns="index_right")
    capmap = dict(zip(cap.FEEDER, cap.CAPACITY))
    # Neighbour-county values only from a national S01 file of the same file date.
    neighbours = other_county_capacity(ROOT / s01_row.archive_path, cfg["code"]) if s01_row is not None else {}

    feeders = []
    for fid in sorted(set(cap.FEEDER) | set(g.FEEDER)):
        sub = g[g.FEEDER == fid]
        kw = capmap.get(fid)
        source, county = (cfg["source"], "") if kw is not None else ("", "")
        towns_ct = Counter(sub.TOWNNAME.fillna(OUTSIDE))
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
        if OUTSIDE in towns_ct:
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
        "points_far_outside_county": int(far.sum()),
        "far_outside_feeders": sorted(set(g.FEEDER[far.to_numpy()])),
        # Basis: the county's own capacity table only — matches the KPI denominators.
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


def cross_check(manifest: pd.DataFrame, code: str) -> list[dict]:
    """Compare a county's members inside S01 (national) and S02 (county) archives of the same date."""
    out = []
    s01 = manifest[manifest.source_id == "S01"]
    for _, s02 in manifest[manifest.source_id == "S02"].iterrows():
        twin = s01[s01.file_date == s02.file_date]
        if twin.empty:
            continue
        rec = {"file_date": s02.file_date}
        for suffix in ("capacity", "tr_feeder"):
            _, d1, h1 = read_member(ROOT / twin.iloc[0].archive_path, code, suffix)
            _, d2, h2 = read_member(ROOT / s02.archive_path, code, suffix)
            rec[suffix] = "identical" if h1 == h2 else f"differs (S01 {d1} {h1[:8]} vs S02 {d2} {h2[:8]})"
        out.append(rec)
    return out


def write_csv(path: Path, snap: dict, cfg: dict, pages: dict[str, str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    cols = ["feeder", "capacity_kw", "capacity_source", "capacity_county", "class", "class_label", "transformer_points",
            "main_township", "main_township_share", "townships", "lon", "lat", "flags"]
    labels = {c["id"]: c["label"] for c in CLASSES} | {NO_CAPACITY_CLASS: NO_CAPACITY_LABEL}
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        f.write(f"# {cfg['name']}配電饋線公布可併網容量｜台電容量表日期 {snap['capacity_date']}｜原始檔 SHA-256 {snap['archive_sha256']}\n")
        f.write(f"# 來源：台灣電力公司，政府資料開放平臺 {pages[cfg['source']]}，{LICENSE['name']} {LICENSE['url']}\n")
        if cfg["source"] == "S01":
            f.write(f"# 容量取自全國檔中的 {cfg['code']}_capacity.csv；capacity_county 有值者為鄰縣饋線，容量取自同一全國檔中該縣市的容量表\n")
        elif snap["s01_snapshot_id"]:
            f.write(f"# capacity_source=S01：鄰縣饋線，容量取自同日全國檔 {pages['S01']}（SHA-256 {snap['s01_archive_sha256']}）\n")
        f.write(f"# 衍生欄位（class、鄉鎮、經緯度）為本專案計算，方法版本 {METHOD_VERSION}。容量不可跨饋線加總。\n")
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        for r in snap["feeders"]:
            w.writerow({k: r.get(k) for k in cols} | {"class_label": labels[r["class"]]})


def write_points(path: Path, snap: dict) -> None:
    classes = {f["feeder"]: f["class"] for f in snap["feeders"]}
    feats = []
    for p in snap["_points"].itertuples():
        feats.append({
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [round(p.geometry.x, 5), round(p.geometry.y, 5)]},
            "properties": {"f": p.FEEDER, "c": classes.get(p.FEEDER, NO_CAPACITY_CLASS)},
        })
    path.write_text(json.dumps({"type": "FeatureCollection", "features": feats}, ensure_ascii=False, separators=(",", ":")))


def write_towns(path: Path, towns: gpd.GeoDataFrame) -> None:
    t = towns.copy()
    rep = t.geometry.representative_point()
    t["label_lon"], t["label_lat"] = rep.x.round(5), rep.y.round(5)
    t["geometry"] = t.geometry.simplify(0.0004, preserve_topology=True)
    t.rename(columns={"TOWNNAME": "name", "TOWNCODE": "code"}).to_file(path, driver="GeoJSON", COORDINATE_PRECISION=5)


def write_town_lines(path: Path, towns: gpd.GeoDataFrame, points: gpd.GeoDataFrame) -> None:
    """Boundaries between townships, only where the county's transformers are; merged at full resolution, then simplified.

    Along the coast, MOI boundaries take in tidal flats and run well offshore, so drawn on
    the basemap they look like a misplaced coastline. The county's outer edge is left out,
    and the lines between coastal townships are cut at a concave hull of the transformers
    (+300 m), which stands in for the land: transformers are always on land.
    """
    county = towns.union_all()
    inside = points[points.TOWNNAME.notna()].to_crs("EPSG:3826")  # misfiled far-away points would stretch the hull
    hull = shapely.concave_hull(shapely.multipoints(shapely.get_coordinates(inside.geometry)), ratio=0.05).buffer(300)
    land = gpd.GeoSeries([hull], crs="EPSG:3826").to_crs("EPSG:4326").iloc[0]
    edges = shapely.union_all(list(towns.boundary)).difference(county.boundary.buffer(1e-5)).intersection(land)
    # union_all leaves thousands of 2-point pieces that simplify() cannot thin; join them into long lines first.
    edges = shapely.line_merge(edges).simplify(0.0004, preserve_topology=False)
    gpd.GeoDataFrame(geometry=[edges], crs="EPSG:4326").to_file(path, driver="GeoJSON", COORDINATE_PRECISION=5)


def write_json(path: Path, value) -> None:
    # allow_nan=False: NaN is not valid JSON and would break the whole page; fail the build instead.
    path.write_text(json.dumps(value, ensure_ascii=False, indent=1, allow_nan=False))


def feeder_stability(snaps: list[dict]) -> list[dict]:
    out = []
    for a, b in zip(snaps, snaps[1:]):
        fa = {f["feeder"] for f in a["feeders"] if own_listed(f)}
        fb = {f["feeder"] for f in b["feeders"] if own_listed(f)}
        out.append({"from": a["capacity_date"], "to": b["capacity_date"], "common": len(fa & fb),
                    "only_from": len(fa - fb), "only_to": len(fb - fa)})
    return out


def public(s: dict) -> dict:
    return {k: v for k, v in s.items() if k not in ("feeders", "_points")}


def build_county(cfg: dict, manifest: pd.DataFrame, allg: gpd.GeoDataFrame, land_m,
                 pages: dict[str, str], last_checked: dict[str, str]) -> dict:
    towns = county_towns(allg, cfg)
    snaps_src = manifest[manifest.source_id == cfg["source"]].sort_values("file_date")
    s01 = manifest[manifest.source_id == "S01"]

    latest_pts, _, _ = read_member(ROOT / snaps_src.iloc[-1].archive_path, cfg["code"], "tr_feeder")
    crs_result = crs_test(latest_pts, towns.geometry, land_m)
    # Fewest points off land wins; ties go to the documented method (towgs84 first), since the two
    # TWD67 transforms differ by only a few metres.
    order = list(CRS_CANDIDATES)
    crs = min(crs_result, key=lambda k: (crs_result[k]["off_land_points"], crs_result[k]["off_land_over_100m"], order.index(k)))
    assert crs.startswith("twd67"), f"{cfg['code']}: CRS test picked {crs}; review before publishing"

    def same_day_s01(r: pd.Series) -> pd.Series | None:
        twin = s01[s01.file_date == r.file_date]
        return twin.iloc[0] if len(twin) else None

    snaps = [build_snapshot(r, cfg, towns, crs, same_day_s01(r)) for _, r in snaps_src.iterrows()]
    latest = snaps[-1]
    if len(snaps) > 1:
        prev_listed = {f["feeder"] for f in snaps[-2]["feeders"] if own_listed(f)}
        latest["stats"]["no_capacity_listed_in_previous"] = sorted(
            f["feeder"] for f in latest["feeders"] if "no_capacity_row" in f["flags"] and f["feeder"] in prev_listed)

    out = WEB / cfg["slug"]
    out.mkdir(parents=True, exist_ok=True)
    for s in snaps:
        write_csv(WEB / "download" / f"{cfg['slug']}_feeders_{s['capacity_date']}.csv", s, cfg, pages)
    write_points(out / "transformers.geojson", latest)
    shown, omitted = display_towns(towns, latest["_points"])
    write_towns(out / "townships.geojson", shown)
    write_town_lines(out / "town_lines.geojson", shown, latest["_points"])
    write_json(out / "feeders.json", latest["feeders"])
    return {
        **{k: cfg[k] for k in ("code", "slug", "name", "short", "source", "district")},
        "source_title": SOURCES[cfg["source"]]["title"],
        "dataset_page": pages[cfg["source"]],
        "source_last_checked": last_checked[cfg["source"]],
        "townships": int(len(towns)),
        "map_parts_omitted": omitted,
        "crs": {"chosen": crs, "tests": crs_result},
        "latest": public(latest),
        "history": [public(s) for s in snaps],
        "feeder_key_stability": feeder_stability(snaps),
        "s01_s02_cross_check": cross_check(manifest, cfg["code"]) if cfg["source"] == "S02" else [],
    }


def main() -> None:
    manifest = pd.read_csv(MANIFEST, dtype=str)
    # Only reviewed snapshots are published; newer archived ones wait in the manifest.
    approved = set(json.loads(PUBLISHED.read_text(encoding="utf-8"))["approved"])
    manifest = manifest[manifest.snapshot_id.isin(approved)]
    allg, towns_meta = load_townships(manifest)
    land_m = all_land(allg)
    latest_row = {sid: manifest[manifest.source_id == sid].sort_values("file_date").iloc[-1] for sid in ("S01", "S02")}
    pages = {sid: r.dataset_page for sid, r in latest_row.items()}

    check_log = pd.read_csv(CHECKS, dtype=str)
    ok = check_log.result != "error" if "result" in check_log else True
    last_checked = {sid: check_log[(check_log.source_id == sid) & ok].checked_at.max() for sid in ("S01", "S02")}

    WEB.mkdir(parents=True, exist_ok=True)
    counties = [build_county(cfg, manifest, allg, land_m, pages, last_checked) for cfg in COUNTIES]
    built_on = date.today().isoformat()
    summary = {
        "method_version": METHOD_VERSION,
        "built_on": built_on,
        # The oldest of the counties' sources: the site is only as fresh as its stalest county.
        "source_last_checked": min(c["source_last_checked"] for c in counties),
        "scope": "、".join(c["name"] for c in counties) + "・配電饋線（distribution）",
        "grid_level": "distribution",
        "license": LICENSE,
        "sources": {
            "S02": {"title": SOURCES["S02"]["title"], "publisher": "台灣電力公司",
                    "dataset_page": pages["S02"], "source_url": latest_row["S02"].source_url,
                    "declared_frequency": "每 6 月（平臺頁面標示）"},
            "S01": {"title": SOURCES["S01"]["title"], "publisher": "台灣電力公司",
                    "dataset_page": pages["S01"], "source_url": latest_row["S01"].source_url},
            "S12": {"title": "鄉鎮市區界線(TWD97經緯度)", "publisher": "內政部國土測繪中心", **towns_meta},
        },
        "source_titles": {
            "S01": SOURCES["S01"]["short"], "S02": SOURCES["S02"]["short"],
            "S04": "立法院預算中心 115 年度總預算案整體評估報告（一二）",
            "S12": "內政部鄉鎮市區界線（TWD97 經緯度）",
            "S14": "立法院預算中心 114 年度總預算案整體評估報告（一○一）",
        },
        # Every approved archive file, for the methods page.
        "archive": manifest[["snapshot_id", "source_id", "origin", "file_date", "file_date_basis", "members", "sha256",
                             "archive_path"]].fillna("").sort_values(["source_id", "file_date"]).to_dict("records"),
        "national": national_stats(latest_row["S01"]),
        "classes": CLASSES,
        "default_county": DEFAULT_COUNTY,
        "counties": counties,
    }
    write_json(WEB / "summary.json", summary)
    for c in counties:
        write_audit(c, summary)
    print(json.dumps({c["slug"]: {"crs": c["crs"]["chosen"], "capacity_date": c["latest"]["capacity_date"],
                                  **{k: c["latest"]["stats"][k] for k in ("feeders_with_capacity", "zero", "zero_share",
                                                                         "transformer_points", "feeders_other_county",
                                                                         "feeders_no_capacity_row", "points_no_capacity_row",
                                                                         "points_outside_county")}}
                      for c in counties}, ensure_ascii=False, indent=1))


def pct(x: float) -> str:
    return f"{x * 100:.1f}%"


def write_audit(c: dict, s: dict) -> None:
    L = c["latest"]
    st = L["stats"]
    code, short = c["code"], c["short"]
    national = c["source"] == "S01"
    cfg = next(x for x in COUNTIES if x["code"] == code)
    ballpark = c["crs"]["tests"]["epsg3828_ballpark"]
    chosen = c["crs"]["tests"][c["crs"]["chosen"]]
    tables = (f"`{code}_capacity.csv`（`FEEDER`, `CAPACITY`＝饋線可併網容量，單位千瓦）與 `{code}_tr_feeder.csv`"
              "（`ROWNUM`, `X`, `Y`, `FEEDER`＝變壓器 TWD67 座標及所屬饋線）")
    lines = [
        f"# {c['name']}再生能源可併網容量：資料審計",
        "",
        f"> 本檔由 `src/transform/build_counties.py` 自動產生（方法版本 {s['method_version']}，建置日 {s['built_on']}）。請勿手動編輯。",
        "",
        f"來源：{c['source_title']}（{c['source']}，{c['dataset_page']}）。",
        "",
        "## 1. 檔案結構（已確認）",
        "",
        f"- 全國檔 ZIP 內每個縣市各有兩張表與 schema；本縣使用 {tables}。" if national
        else f"- ZIP 內含兩張表與各自 schema：{tables}。",
        *([f"- 台電 {code} 檔涵蓋{'與'.join(cfg['moi'])}；{'、'.join(cfg['moi'][1:])}的區在鄉鎮欄加上市名前綴（如「{cfg['moi'][1]}東區」）。"]
          if len(cfg["moi"]) > 1 else []),
        f"- 地圖只畫鄉鎮之間的界線（限有變壓器分布的範圍），不畫縣的外圍邊，並省略 {c['map_parts_omitted']} 塊沒有任何變壓器的離岸沙洲或小島（沿海行政界線涵蓋潮間帶，與底圖海岸線不一致）；鄉鎮歸屬仍以完整界線計算。",
        "- **容量以饋線為單位。** 變壓器點位本身沒有容量，只標示該饋線設備位置；同一饋線多點是正常結構，不是重複容量。",
        "- CSV 內沒有資料日期欄位。本專案以 ZIP 內各成員檔的修改時間作為「檔案日期」，並分別記錄容量表與點位表的日期。",
        "",
        "## 2. 版本與日期",
        "",
        "| 快照 | 來源 | 容量表日期 | 點位表日期 | 原始檔 SHA-256 |",
        "|---|---|---|---|---|",
    ]
    for h in c["history"]:
        lines.append(f"| {h['snapshot_id']} | {h['origin']} | {h['capacity_date']} | {h['points_date']} | `{h['archive_sha256'][:16]}…` |")
    lines += [
        "",
        "平臺頁面標示每 6 個月更新；已取得的版本間隔約一年（可能有未被 Wayback 保存的中間版本）。",
        "",
        "## 3. 座標系統驗證",
        "",
        "變壓器必在陸地上，因此以「落在陸域之外（海上）」的點數作為主要判準；陸域為內政部全臺鄉鎮界線的聯集。",
        "",
        f"| 轉換方式 | 落在{c['name']}界內 | 落在陸域外 | 陸域外逾 100 m | 與採用方案平均差距 |",
        "|---|---|---|---|---|",
    ]
    for k, v in c["crs"]["tests"].items():
        lines.append(f"| `{k}` {v['name']} | {pct(v['inside_county'])} | {v['off_land_points']} | {v['off_land_over_100m']} | {v['mean_offset_from_towgs84_m']} m |")
    lines += [
        "",
        f"採用：**`{c['crs']['chosen']}`**。兩種獨立的 TWD67→TWD97 方法結果一致（差距數公尺），"
        + ("且無任何點位落在陸域外；" if chosen["off_land_points"] == 0
           else f"落在陸域外 {chosen['off_land_points']} 點" + ("，皆在海岸線 100 公尺內（界線精度範圍）；" if chosen["off_land_over_100m"] == 0
                                                          else f"，其中 {chosen['off_land_over_100m']} 點逾 100 公尺，待查；")),
        f"若直接使用 PROJ 對 EPSG:3828 的預設轉換（僅概略偏移，無基準轉換），全部點位會系統性偏移約 {ballpark['mean_offset_from_towgs84_m']:,.0f} 公尺，"
        f"並有 {ballpark['off_land_points']:,} 點落在陸域外。",
        "",
        f"## 4. 最新快照品質（容量表 {L['capacity_date']}）",
        "",
        f"- 饋線容量列 {st['capacity_rows']} 筆、不重複饋線 {st['feeders_with_capacity']} 條；負值 {st['negatives']} 筆。",
        f"- 公布容量為 0：{st['zero']} 條（{pct(st['zero_share'])}）；未滿 1 MW：{st['under_1mw']} 條（{pct(st['under_1mw_share'])}）。",
        f"- 中位數 {st['median_kw']:,} kW（四分位 {st['p25_kw']:,}–{st['p75_kw']:,} kW）；最大值 {st['max_kw']:,} kW，共 {st['count_at_max']} 條等於最大值（疑似上限值，待向台電確認）。",
        f"- 變壓器點位 {st['transformer_points']:,} 個，分屬 {st['feeders_in_points']} 個饋線代號；落在縣界外 {st['points_outside_county']} 個"
        + (f"，其中 {st['points_far_outside_county']} 個距縣界逾 {FAR_OUTSIDE_M // 1000} 公里（饋線 {'、'.join(st['far_outside_feeders'])}），疑為台電歸錯縣的資料列。"
           if st["points_far_outside_county"] else "。"),
        f"- 點位表有、{short}容量表沒有的變壓器共 {st['points_without_capacity_feeder']:,} 個，分四種：",
        f"  - 鄰縣饋線 {st['feeders_other_county']} 條（{st['points_other_county']:,} 個點）：容量見同日全國檔（S01）其他縣市容量表，本站取用並標示來源。"
        if L["s01_snapshot_id"] else "  - 鄰縣饋線：本版無同日全國檔，未補值。",
        f"  - 各縣市容量表都沒有 {st['feeders_no_capacity_row']} 條（{st['points_no_capacity_row']:,} 個點）：標為「{NO_CAPACITY_LABEL}」，成因待確認。"
        + (f"其中 {len(st['no_capacity_listed_in_previous'])} 條（{'、'.join(st['no_capacity_listed_in_previous'])}）在前一版容量表中仍有數值，這一版才從容量表消失。"
           if st.get("no_capacity_listed_in_previous") else ""),
        f"  - 所屬饋線欄位為「{FEEDER_CODE_NONE}」：{st['points_feeder_code_none']} 個點。",
        f"  - 代號在多個鄰縣容量表出現且數值不同、因此不補值：{st['feeders_ambiguous_other_county']} 條。",
        (f"- 同日全國檔中，同一饋線代號在多個縣市容量表出現且數值不同者：{st['national_codes_conflicting']} 個"
         + (f"（例：{'、'.join(st['national_conflict_examples'])}）" if st["national_conflict_examples"] else "")
         + "。")
        if L["s01_snapshot_id"] else "",
        *[
            f"- {h['s01_snapshot_id']} 全國檔有 {h['stats']['national_codes_conflicting']} 個代號在多縣數值不同"
            f"（例：{'、'.join(h['stats']['national_conflict_examples'])}）：饋線代號不保證全國唯一，跨縣比對須連同縣市判斷。"
            for h in c["history"] if h["stats"].get("national_codes_conflicting")
        ],
        f"- {short}容量表有、點位表沒有的饋線 {st['feeders_no_points']} 條（地圖上無法定位，只列在饋線清單）。",
        "",
        "## 5. 全國檔（S01）與縣檔交叉比對",
        "",
    ]
    if national:
        lines.append("- 本縣直接使用全國檔，未另行下載縣檔比對；雲林在每個共同日期，全國檔與縣檔的兩張表都逐位元組相同（見雲林審計）。")
    else:
        lines += [f"- {x['file_date']}：容量表 {x['capacity']}；點位表 {x['tr_feeder']}" for x in c["s01_s02_cross_check"]] or ["- 無同日期版本可比對。"]
    lines += ["", "## 6. 饋線代號跨版本穩定性", "", "| 前版 | 後版 | 共同 | 僅前版 | 僅後版 |", "|---|---|---|---|---|"]
    lines += [f"| {x['from']} | {x['to']} | {x['common']} | {x['only_from']} | {x['only_to']} |" for x in c["feeder_key_stability"]]
    lines += [
        "",
        "代號相同不代表饋線範圍或計算條件相同；跨版本差值須另經可比性檢查（spec §8.3）後才發布。",
        "",
        "## 7. 未決問題（P0）",
        "",
        "1. 容量計算條件、日期與上限值的官方定義。",
        f"2. 各縣市容量表都沒有的 {st['feeders_no_capacity_row']} 條饋線，與有容量但無點位的 {st['feeders_no_points']} 條饋線，是否有對應關係（新設、改名或停用）。",
        "3. 檔案日期（ZIP 成員修改時間）與台電實際計算基準日的關係。",
    ]
    AUDIT_DIR.mkdir(parents=True, exist_ok=True)
    (AUDIT_DIR / f"{c['slug']}_capacity_audit.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
