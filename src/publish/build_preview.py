"""Bundle the site into one self-contained HTML file for sharing before publication.

Opening web/index.html from disk fails (browsers block file:// fetches), so this
inlines CSS, JS, MapLibre and all data, embeds the methods page (opened in a new
tab from a blob URL) and adds a "preview" banner. Only the basemap still needs
the network; without it the page falls back to a plain background.

Usage: uv run python src/publish/build_preview.py
"""

from __future__ import annotations

import hashlib
import json
import re
import urllib.request
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
WEB = ROOT / "web"
DIST = ROOT / "dist"

MAPLIBRE = "https://unpkg.com/maplibre-gl@4.7.1/dist/"
# Pinned so a changed CDN file fails the build instead of being shipped.
MAPLIBRE_SHA256 = {
    "maplibre-gl.js": "be9633c4d870e26fb37f1cfe5c5a77181667114003ea16207ac7850d8da8add1",
    "maplibre-gl.css": "576b085fdd9487a65a19215328c1e086c07ce5bf6da09b666b3806d3d008dae9",
}


def vendor(name: str) -> str:
    with urllib.request.urlopen(MAPLIBRE + name, timeout=60) as r:
        blob = r.read()
    digest = hashlib.sha256(blob).hexdigest()
    if digest != MAPLIBRE_SHA256[name]:
        raise SystemExit(f"{name}: sha256 {digest} does not match the pinned value")
    return blob.decode("utf-8")


def script_safe(text: str) -> str:
    """Make text safe inside <script>…</script>."""
    return text.replace("</", "<\\/")


def js_value(value) -> str:
    return script_safe(json.dumps(value, ensure_ascii=False, separators=(",", ":")))


def compact_points(geojson: dict) -> dict:
    """36k point features -> integer arrays (about a quarter of the GeoJSON size)."""
    feeders: list[str] = []
    index: dict[str, int] = {}
    rows: list[int] = []
    for ft in geojson["features"]:
        f = ft["properties"]["f"]
        if f not in index:
            index[f] = len(feeders)
            feeders.append(f)
        lon, lat = ft["geometry"]["coordinates"]
        rows += [round(lon * 1e5), round(lat * 1e5), index[f], ft["properties"]["c"]]
    return {"feeders": feeders, "rows": rows}


DECODE_POINTS = """
(function () {
  var d = window.__INLINE_DATA__, c = d.__points, feats = [];
  for (var i = 0; i < c.rows.length; i += 4) {
    feats.push({ type: "Feature", geometry: { type: "Point", coordinates: [c.rows[i] / 1e5, c.rows[i + 1] / 1e5] },
                 properties: { f: c.feeders[c.rows[i + 2]], c: c.rows[i + 3] } });
  }
  d["data/transformers.geojson"] = { type: "FeatureCollection", features: feats };
  delete d.__points;
})();
"""


def inline_assets(html: str, css: dict[str, str], scripts: dict[str, str]) -> str:
    """Replace <link>/<script src> tags (matched on their file name, any ?v=) with inline content."""
    for name, body in css.items():
        html, n = re.subn(rf'<link rel="stylesheet" href="[^"]*{re.escape(name)}[^"]*">',
                          lambda _: f"<style>\n{body}\n</style>", html)
        assert n == 1, f"stylesheet {name} not found exactly once"
    for name, body in scripts.items():
        html, n = re.subn(rf'<script src="[^"]*{re.escape(name)}[^"]*"></script>',
                          lambda _: f"<script>\n{script_safe(body)}\n</script>", html)
        assert n == 1, f"script {name} not found exactly once"
    return html


def main() -> None:
    summary = json.loads((WEB / "data" / "summary.json").read_text(encoding="utf-8"))
    queue = json.loads((WEB / "data" / "queue.json").read_text(encoding="utf-8"))
    towns = json.loads((WEB / "data" / "townships.geojson").read_text(encoding="utf-8"))
    points = json.loads((WEB / "data" / "transformers.geojson").read_text(encoding="utf-8"))
    csvs = {f"data/download/{p.name}": p.read_text(encoding="utf-8-sig") for p in sorted((WEB / "data" / "download").glob("*.csv"))}

    built = date.today().isoformat()
    banner = (f"預覽版（{built} 建置，台電容量表 {summary['latest']['capacity_date']}）："
              "尚未正式發布，內容與數字可能調整，請勿對外引用。底圖需要網路連線。")
    style = (WEB / "style.css").read_text(encoding="utf-8")
    config = (WEB / "config.js").read_text(encoding="utf-8")
    maplibre_js, maplibre_css = vendor("maplibre-gl.js"), vendor("maplibre-gl.css")

    # Methods page: its own data, opened from a blob URL, so links back to "./" cannot work.
    methods = (WEB / "methods.html").read_text(encoding="utf-8")
    methods = inline_assets(methods, {"style.css": style}, {"config.js": config})
    methods = methods.replace('<nav class="head-nav" aria-label="網站"><a href="./">回到現況地圖</a></nav>',
                              '<nav class="head-nav" aria-label="網站"><span>關閉此分頁即可回到地圖</span></nav>')
    methods = methods.replace('<a href="./">回到現況地圖</a>', "關閉此分頁即可回到地圖")
    assert 'href="./"' not in methods, "methods page still links to ./"
    methods = methods.replace("<title>", "<title>（預覽版）", 1)
    methods = methods.replace("<script>\n(async", "<script>window.__INLINE_DATA__ = "
                              + js_value({"data/summary.json": summary, "data/queue.json": queue, **csvs})
                              + ";</script>\n<script>\n(async", 1)
    assert "__INLINE_DATA__ = " in methods

    index = (WEB / "index.html").read_text(encoding="utf-8")
    index = inline_assets(index, {"maplibre-gl.css": maplibre_css, "style.css": style},
                          {"maplibre-gl.js": maplibre_js, "config.js": config, "app.js": (WEB / "app.js").read_text(encoding="utf-8")})
    data = {"data/summary.json": summary, "data/queue.json": queue, "data/townships.geojson": towns,
            "__points": compact_points(points), **csvs}
    payload = (f"<script>window.__INLINE_DATA__ = {js_value(data)};\n"
               f"window.__PREVIEW__ = {js_value(banner)};\n"
               f"window.__METHODS_HTML__ = {js_value(methods)};</script>\n"
               f"<script>{DECODE_POINTS}</script>\n")
    marker = "<script>\n/**\n * MapLibre GL JS"
    assert index.count(marker) == 1
    index = index.replace(marker, payload + marker)
    index = index.replace("<title>", "<title>（預覽版）", 1)

    # Local paths must not leak into a file meant to be forwarded. (MapLibre itself contains the
    # bare string "file://" in an origin check, so look for actual file URLs instead.)
    for leak in (r"/Users/", r"/private/", r"file:///\w"):
        assert not re.search(leak, index), f"local path {leak!r} found in output"

    DIST.mkdir(exist_ok=True)
    out = DIST / f"雲林饋線容量現況_預覽版_{built}.html"
    out.write_text(index, encoding="utf-8")
    print(f"{out.relative_to(ROOT)}  {out.stat().st_size / 1e6:.2f} MB")


if __name__ == "__main__":
    main()
