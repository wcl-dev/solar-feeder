(() => {
  "use strict";

  const CFG = window.SITE_CONFIG || {};
  // Standalone preview: data, CSVs and the methods page are embedded by src/publish/build_preview.py.
  const INLINE = window.__INLINE_DATA__ || null;
  const blobUrls = {};
  const downloadHref = (path) => {
    if (!INLINE || typeof INLINE[path] !== "string") return path;
    return (blobUrls[path] ||= URL.createObjectURL(new Blob(["\ufeff" + INLINE[path]], { type: "text/csv;charset=utf-8" })));
  };
  const COLORS = { "-1": "#898781", 0: "#eb6834", 1: "#86b6ef", 2: "#3987e5", 3: "#256abf", 4: "#104281" };
  const NO_CAP = -1;
  const NO_CAP_LABEL = "台電未列容量";
  const CODE_NONE = "無"; // Taipower's placeholder: transformer with no feeder assigned
  const FLAG_TEXT = {
    no_capacity_row: "台電雲林與全國容量表都沒有列出這條饋線",
    no_transformer_points: "台電變壓器位置表沒有這條饋線，地圖上無法定位",
    points_outside_county: "部分變壓器落在雲林縣界外",
    capacity_from_other_county: "鄰縣饋線：容量取自台電全國檔中鄰縣的容量表",
    ambiguous_other_county: "代號在多個鄰縣容量表出現且數值不同，未補值",
    feeder_code_none: "台電變壓器位置表中，這些變壓器的所屬饋線寫「無」",
    negative_capacity: "原始容量為負值",
    duplicate_capacity_row: "容量表中重複出現",
  };
  const TAG_TIP = {
    observed: "事實：台電原始資料直接記錄的數值",
    derived: "計算：本專案依公開方法由原始資料計算而得",
  };
  const BASEMAP = "https://tiles.openfreemap.org/styles/positron";

  const $ = (s, el = document) => el.querySelector(s);
  const $$ = (s, el = document) => [...el.querySelectorAll(s)];
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
  const fmt = (n, d = 0) => (n == null ? "—" : Number(n).toLocaleString("zh-TW", { minimumFractionDigits: d, maximumFractionDigits: d }));
  const pct = (x) => `${(x * 100).toFixed(1)}%`;
  const today = () => new Date().toLocaleDateString("sv-SE");
  const tag = (kind) => `<span class="tag" data-tag="${kind}" title="${TAG_TIP[kind]}">${kind === "observed" ? "事實" : "計算"}</span>`;
  const swatch = (c) => `<span class="swatch${c === NO_CAP ? " na" : ""}" style="background:${COLORS[c]}" aria-hidden="true"></span>`;

  let S, L, feeders, byId, classes, map, popup;
  let selected = null;
  const active = new Set();
  const bounds = {};
  const sortState = { key: "capacity_kw", dir: "asc" };

  // A local file path must never end up in a citation, so file:/blob: pages have no shareable URL.
  const pageUrl = () => CFG.siteUrl || (/^(file|blob):/.test(location.protocol) ? null : location.href.split("#")[0]);
  const csvPath = () => downloadHref(`data/download/yunlin_feeders_${L.capacity_date}.csv`);
  const archiveUrl = (path = L.archive_path) => (CFG.repoUrl ? `${CFG.repoUrl}/blob/${CFG.repoBranch || "main"}/${path}` : null);
  const classLabel = (c) => (c === NO_CAP ? NO_CAP_LABEL : classes.find((k) => k.id === c).label);
  const feederName = (id) => (id === CODE_NONE ? "（未標饋線）" : id);

  async function load() {
    const get = (u) => (INLINE && u in INLINE
      ? (INLINE[u] == null ? Promise.reject(new Error(`${u}: not embedded`)) : Promise.resolve(INLINE[u]))
      : fetch(u).then((r) => (r.ok ? r.json() : Promise.reject(new Error(`${u}: ${r.status}`)))));
    const [summary, tx, towns, queue] = await Promise.all([
      get("data/summary.json"),
      get("data/transformers.geojson"),
      get("data/townships.geojson"),
      get("data/queue.json").catch(() => null),
    ]);
    S = summary;
    L = S.latest;
    feeders = S.feeders;
    byId = Object.fromEntries(feeders.map((f) => [f.feeder, f]));
    // Legend basis = what the map draws (feeders with transformers in Yunlin, incl. neighbour-county feeders).
    const M = L.stats.map;
    classes = [...S.classes, { id: NO_CAP, label: NO_CAP_LABEL }].map((c) => ({
      ...c, count: M.class_feeders[c.id] ?? 0, points: M.class_transformers[c.id] ?? 0,
    })).filter((c) => c.id !== NO_CAP || c.points > 0);
    classes.forEach((c) => active.add(c.id));

    for (const ft of tx.features) {
      const [x, y] = ft.geometry.coordinates;
      const b = (bounds[ft.properties.f] ||= [x, y, x, y]);
      b[0] = Math.min(b[0], x); b[1] = Math.min(b[1], y); b[2] = Math.max(b[2], x); b[3] = Math.max(b[3], y);
    }

    renderHeader();
    renderKpis();
    renderLegend();
    renderQueue(queue);
    renderTowns(towns);
    renderTableTools(towns);
    renderTable();
    renderCitations();
    renderFooter();
    initMap(tx, towns);
    bindCopy();
  }

  function renderHeader() {
    const checked = (S.source_last_checked || "").slice(0, 10);
    $("#dates").innerHTML =
      `台電容量表日期 <b>${esc(L.capacity_date)}</b>（依台電 ZIP 內檔案日期）・本站最後檢查來源 <b>${esc(checked)}</b>・方法版本 ${esc(S.method_version)}`;
  }

  function renderKpis() {
    const st = L.stats;
    const hist = S.history.map((h) => `<span>${h.capacity_date.slice(0, 7)}</span><b>${pct(h.stats.zero_share)}</b>`).join("");
    $("#kpis").innerHTML = `
      <div class="tile">
        <p class="tile-label">雲林容量表列出的饋線 ${tag("observed")}</p>
        <p class="tile-value">${fmt(st.feeders_with_capacity)}<small>條</small></p>
        <p class="tile-foot">變壓器 ${fmt(st.transformer_points)} 具（台電變壓器位置表）</p>
      </div>
      <div class="tile">
        <p class="tile-label">${swatch(0)}公布可併網容量為 0 ${tag("derived")}</p>
        <p class="tile-value">${fmt(st.zero)}<small>條（${pct(st.zero_share)}）</small></p>
        <p class="tile-foot">不代表這些饋線上的新案一定無法併網</p>
      </div>
      <div class="tile">
        <p class="tile-label">每條饋線公布容量的中位數 ${tag("derived")}</p>
        <p class="tile-value">${fmt(st.median_kw)}<small>kW</small></p>
        <p class="tile-foot">四分位 ${fmt(st.p25_kw)}–${fmt(st.p75_kw)} kW；各饋線不可加總</p>
      </div>
      <div class="tile">
        <p class="tile-label">公布為 0 的比例・各版 ${tag("derived")}</p>
        <p class="tile-hist">${hist}</p>
        <p class="tile-foot">各版獨立呈現；跨版可比性檢查完成前不計算增減。<a href="methods.html#history">說明</a></p>
      </div>`;
  }

  function renderLegend() {
    const M = L.stats.map;
    const total = classes.reduce((a, c) => a + c.count, 0);
    const totalPts = M.transformers;
    const sub = { 0: "台電公布值為 0", [NO_CAP]: "雲林與全國容量表都沒有" };
    $("#legend").innerHTML = classes.map((c) => `
      <li><button type="button" aria-pressed="true" data-class="${c.id}">
        ${swatch(c.id)}
        <span class="lab">${esc(c.label)}${sub[c.id] ? `<small>${sub[c.id]}</small>` : ""}</span>
        <span class="cnt">${fmt(c.count)} 條<small>變壓器 ${pct(c.points / totalPts)}</small></span>
        <span class="bar" aria-hidden="true"><i style="width:${(c.points / totalPts) * 100}%;background:${COLORS[c.id]}"></i></span>
      </button></li>`).join("") +
      `<li class="legend-note">地圖上的每個點是一具變壓器，顏色是它所屬饋線的公布容量。公布為 0 的多是長饋線、變壓器多：它們占雲林容量表饋線的 ${pct(L.stats.zero_share)}，卻占地圖上變壓器的 ${pct(byClassPts(0, totalPts))}，所以橘色看起來比條數比例多。<br>條數以地圖上的 ${fmt(total)} 條饋線計，含 ${fmt(L.stats.feeders_other_county)} 條供電到雲林的鄰縣饋線，不含 ${fmt(L.stats.feeders_no_points)} 條沒有位置資料的饋線。</li>` +
      `<li class="legend-actions"><button type="button" id="legend-all">全部顯示</button>・<button type="button" id="legend-zero">只看公布為 0</button></li>`;
    $("#legend").addEventListener("click", (e) => {
      const b = e.target.closest("button");
      if (!b) return;
      if (b.id === "legend-all") classes.forEach((c) => active.add(c.id));
      else if (b.id === "legend-zero") { active.clear(); active.add(0); }
      else {
        const id = Number(b.dataset.class);
        active.has(id) ? active.delete(id) : active.add(id);
      }
      $$("#legend [data-class]").forEach((x) => x.setAttribute("aria-pressed", String(active.has(Number(x.dataset.class)))));
      applyClassFilter();
    });
  }

  function byClassPts(id, totalPts) {
    const c = classes.find((k) => k.id === id);
    return c ? c.points / totalPts : 0;
  }

  function applyClassFilter() {
    if (!map || !map.getLayer("tx")) return;
    map.setFilter("tx", ["in", ["get", "c"], ["literal", [...active]]]);
  }

  function renderQueue(q) {
    if (!q) return;
    const band = $("#queue-band");
    band.hidden = false;
    band.innerHTML = `
      <div>
        <h2>等待併網：${esc(q.district)} ${tag("observed")}</h2>
        <p class="scope">這是台電「區營業處」層級的彙總數字，無法定位到鄉鎮、饋線或個別案場，所以不畫在地圖上，也不分攤到鄉鎮。</p>
      </div>
      <div class="band-figs">
        <div><div class="v">${fmt(q.cases)}<small>件</small></div><div class="l">太陽光電等待併網</div></div>
        <div><div class="v">${fmt(q.capacity_mw, 2)}<small>MW</small></div><div class="l">合計容量</div></div>
        <div><div class="v">${fmt(q.over_2y_cases)}<small>件</small></div><div class="l">其中「超過2年以上」（${fmt(q.over_2y_mw, 2)} MW）</div></div>
        <div><div class="v">${(q.share_of_listed_mw * 100).toFixed(2)}<small>%</small></div><div class="l">占表列 ${q.listed_districts} 個區處等待容量${q.rank_by_mw_among_listed === 1 ? "，表列最高" : ""}</div></div>
      </div>
      <p class="src">截至 ${esc(q.as_of)}。${esc(q.citation)}<a href="${esc(q.page_url)}">原文頁面</a>・<a href="${esc(q.source_url)}">PDF</a>（本站存檔版本 ${esc(q.snapshot_id)}）。${esc(q.note)}</p>`;
  }

  function renderTowns(towns) {
    const rows = towns.features.map((t) => t.properties.name).map((name) => {
      const fs = feeders.filter((f) => f.main_township === name && f.capacity_source === "S02");
      const vals = fs.map((f) => f.capacity_kw).sort((a, b) => a - b);
      const mid = vals.length ? (vals.length % 2 ? vals[(vals.length - 1) / 2] : (vals[vals.length / 2 - 1] + vals[vals.length / 2]) / 2) : null;
      const zero = fs.filter((f) => f.capacity_kw <= 0).length;
      return { name, n: fs.length, zero, share: fs.length ? zero / fs.length : 0, mid };
    }).sort((a, b) => b.share - a.share || b.n - a.n);
    $("#town-table tbody").innerHTML = rows.map((r) => `
      <tr><th scope="row">${esc(r.name)}</th>
        <td class="num">${fmt(r.n)}</td>
        <td class="num">${fmt(r.zero)}</td>
        <td><div class="ibar"><span><i style="width:${r.share * 100}%"></i></span><b>${r.n ? pct(r.share) : "—"}</b></div></td>
        <td class="num">${fmt(r.mid)}</td></tr>`).join("");
  }

  function renderTableTools(towns) {
    $("#town-filter").insertAdjacentHTML("beforeend",
      towns.features.map((t) => `<option>${esc(t.properties.name)}</option>`).join(""));
    $("#dl-csv").href = csvPath();
    $("#dl-csv").download = `yunlin_feeders_${L.capacity_date}.csv`;
    $("#q").addEventListener("input", renderTable);
    $("#town-filter").addEventListener("change", renderTable);
    $$("#feeders thead button").forEach((b) => b.addEventListener("click", () => {
      const k = b.dataset.sort;
      sortState.dir = sortState.key === k && sortState.dir === "asc" ? "desc" : "asc";
      sortState.key = k;
      renderTable();
    }));
    $("#feeders tbody").addEventListener("click", (e) => {
      const tr = e.target.closest("tr[data-id]");
      if (!tr) return;
      selectFeeder(tr.dataset.id, { fly: true });
      $("#map").scrollIntoView({ behavior: "smooth", block: "center" });
    });
  }

  function renderTable() {
    const q = $("#q").value.trim().toUpperCase();
    const town = $("#town-filter").value;
    const { key, dir } = sortState;
    const rows = feeders
      .filter((f) => !q || f.feeder.toUpperCase().includes(q) || (f.townships || "").includes($("#q").value.trim()))
      .filter((f) => !town || (f.townships || "").split("、").includes(town))
      .sort((a, b) => {
        const va = a[key], vb = b[key];
        const na = va == null || va === "", nb = vb == null || vb === "";
        if (na || nb) return na - nb || a.feeder.localeCompare(b.feeder);
        const c = typeof va === "string" ? va.localeCompare(vb, "zh-Hant") : va - vb;
        return (dir === "asc" ? c : -c) || a.feeder.localeCompare(b.feeder);
      });
    $$("#feeders thead button").forEach((b) => (b.dataset.dir = b.dataset.sort === key ? dir : ""));
    $("#feeders tbody").innerHTML = rows.map((f) => `
      <tr data-id="${esc(f.feeder)}" aria-selected="${f.feeder === selected}">
        <td><b>${esc(feederName(f.feeder))}</b>${f.capacity_source === "S01" ? `<small class="src-note">${esc(f.capacity_county)}</small>` : ""}</td>
        <td class="num">${fmt(f.capacity_kw, 1)}</td>
        <td><span class="cls">${swatch(f.class)}${esc(classLabel(f.class))}</span></td>
        <td>${esc(f.main_township || "—")}</td>
        <td class="num">${fmt(f.transformer_points)}</td>
        <td class="flag">${(f.flags ? f.flags.split(";") : []).map((x) => esc(FLAG_TEXT[x] || x)).join("；")}</td>
      </tr>`).join("");
    $("#row-count").textContent = `顯示 ${fmt(rows.length)} / ${fmt(feeders.length)} 條`;
  }

  function dataCitation(feederId) {
    const year = L.capacity_date.slice(0, 4);
    const f = feederId ? byId[feederId] : null;
    const national = f && f.capacity_source === "S01";
    const src = national ? S.sources.S01 : S.sources.S02;
    const snap = national ? L.s01_snapshot_id : L.snapshot_id;
    const arch = archiveUrl(national ? L.s01_archive_path : L.archive_path);
    return `${src.publisher}（${year}）。${src.title}［資料集；容量表檔案日期 ${L.capacity_date}］。政府資料開放平臺，${src.dataset_page}。` +
      (feederId ? `${national ? `${f.capacity_county}容量表，` : ""}饋線 ${feederName(feederId)}。` : "") +
      `本站存檔版本 ${snap}${arch ? `，${arch}` : ""}。` +
      (feederId && pageUrl() ? `檢視頁面：${pageUrl()}#feeder=${encodeURIComponent(feederId)}。` : "") +
      `取用日期 ${today()}。`;
  }

  function siteCitation() {
    return `台灣太陽光電併網瓶頸觀測站（${S.built_on.slice(0, 4)}）。雲林縣配電饋線：公布可併網容量現況（方法版本 ${S.method_version}；台電容量表 ${L.capacity_date}）。${pageUrl() || "（正式網址發布後提供）"}。取用日期 ${today()}。`;
  }

  function renderCitations() {
    $("#cite-data").textContent = dataCitation();
    $("#cite-site").textContent = siteCitation();
  }

  function renderFooter() {
    const repo = CFG.repoUrl ? `程式與存檔：<a href="${esc(CFG.repoUrl)}">${esc(CFG.repoUrl.replace("https://", ""))}</a>。` : "";
    $("#foot").innerHTML = `
      <p>資料來源：台灣電力公司〈${esc(S.sources.S02.title)}〉（<a href="${esc(S.sources.S02.dataset_page)}">政府資料開放平臺</a>）；內政部國土測繪中心〈${esc(S.sources.S12.title)}〉（<a href="${esc(S.sources.S12.dataset_page)}">政府資料開放平臺</a>）。依<a href="${esc(S.license.url)}">${esc(S.license.name)}</a>利用。</p>
      <p>本站為獨立整理，不代表台電或政府機關立場，也不是正式併網判定或選址工具。官方最新參考值請查<a href="https://service.taipower.com.tw/hcweb/geohc/" rel="noopener">台電配電級再生能源可併容量查詢系統</a>。</p>
      <p>底圖：OpenFreeMap、© OpenStreetMap 貢獻者。${repo}<a href="methods.html">方法與資料</a></p>`;
  }

  function cardHtml(f) {
    const zero = f.capacity_kw != null && f.capacity_kw <= 0;
    // These three are already explained by the card's own rows and callout.
    const explained = new Set(["capacity_from_other_county", "no_capacity_row", "feeder_code_none"]);
    const flags = (f.flags ? f.flags.split(";") : []).filter((x) => !explained.has(x));
    const national = f.capacity_source === "S01";
    const arch = archiveUrl(national ? L.s01_archive_path : L.archive_path);
    const snap = national ? L.s01_snapshot_id : L.snapshot_id;
    const callout = f.feeder === CODE_NONE
      ? `<b>這些變壓器沒有標所屬饋線。</b>台電變壓器位置表中，它們的「所屬饋線」欄位寫「無」，所以無法對應到任何容量。`
      : f.capacity_kw == null
      ? `<b>台電沒有公布這條饋線的容量。</b>它的變壓器在台電的位置表裡，但雲林與全國的容量表都沒有列出；原因尚待向台電確認。`
      : zero
        ? `<b>公布值為 0，不等於無法併網。</b>這是台電在特定日期與計算條件下的參考值；實際能否併網須經台電個案審查，也受案件順序、上游變電設備等影響。`
        : `<b>這是參考值，不是保證。</b>實際可併容量會隨新案件扣減，也受上游變電設備限制；不可與其他饋線的數字加總。`;
    return `
      <div class="card-head">
        <h2>${f.feeder === CODE_NONE ? "未標饋線的變壓器" : `饋線 ${esc(f.feeder)}`}</h2>
        <button class="close" type="button" id="card-close" aria-label="關閉饋線資訊，回到圖例">✕ 回到圖例</button>
      </div>
      <p class="big">${f.capacity_kw == null ? "—" : fmt(f.capacity_kw, 1)}<small>kW</small></p>
      <div class="class-chip">${swatch(f.class)}級距：${esc(classLabel(f.class))} ${tag("observed")}</div>
      <dl class="meta">
        <dt>資料日期</dt><dd>台電容量表 ${esc(L.capacity_date)}</dd>
        ${national ? `<dt>容量來源</dt><dd style="white-space:normal">鄰縣饋線：取自台電全國檔的${esc(f.capacity_county)}容量表（雲林檔未列）</dd>` : ""}
        <dt>電網層級</dt><dd>配電饋線</dd>
        <dt>雲林境內變壓器</dt><dd>${fmt(f.transformer_points)} 具（位置表 ${esc(L.points_date)}）</dd>
        <dt>主要鄉鎮</dt><dd>${f.main_township ? `${esc(f.main_township)}（${pct(f.main_township_share)}的變壓器）` : "—"} ${tag("derived")}</dd>
        ${f.townships && f.townships.includes("、") ? `<dt>涵蓋鄉鎮</dt><dd style="white-space:normal">${esc(f.townships)}</dd>` : ""}
      </dl>
      ${flags.length ? `<ul class="flags">${flags.map((x) => `<li>${esc(FLAG_TEXT[x] || x)}</li>`).join("")}</ul>` : ""}
      <p class="callout">${callout}</p>
      <div class="cite-mini">
        <h3>${f.feeder === CODE_NONE ? "引用" : "引用這條饋線"}</h3>
        <p class="cite-text" id="cite-feeder">${esc(dataCitation(f.feeder))}</p>
        <div class="btn-row">
          <button class="btn" type="button" data-copy="cite-feeder">複製引用</button>
          ${pageUrl() ? `<button class="btn" type="button" data-copy-text="${esc(`${pageUrl()}#feeder=${encodeURIComponent(f.feeder)}`)}">複製連結</button>` : ""}
        </div>
      </div>
      <ul class="links">
        <li><a href="https://service.taipower.com.tw/hcweb/geohc/" target="_blank" rel="noopener">到台電 FGIS 查最新官方值 ↗</a></li>
        <li><a href="${csvPath()}" download="yunlin_feeders_${esc(L.capacity_date)}.csv">下載本版饋線表（CSV）</a></li>
        <li>原始檔存檔：${esc(snap)}${arch ? `（<a href="${esc(arch)}">檔案</a>）` : ""}</li>
      </ul>`;
  }

  function selectFeeder(id, { fly = false } = {}) {
    const f = byId[id];
    if (!f) return;
    selected = id;
    $("#legend-block").hidden = true;
    const card = $("#feeder-card");
    card.hidden = false;
    card.innerHTML = cardHtml(f);
    $("#card-close").addEventListener("click", clearSelection);
    history.replaceState(null, "", `#feeder=${encodeURIComponent(id)}`);
    $$("#feeders tbody tr").forEach((tr) => tr.setAttribute("aria-selected", String(tr.dataset.id === id)));
    if (map && map.getLayer("tx-sel")) map.setFilter("tx-sel", ["==", ["get", "f"], id]);
    const b = bounds[id];
    if (fly && map && b) map.fitBounds([[b[0], b[1]], [b[2], b[3]]], { padding: 80, maxZoom: 14, duration: 600 });
  }

  function clearSelection() {
    selected = null;
    $("#feeder-card").hidden = true;
    $("#legend-block").hidden = false;
    history.replaceState(null, "", location.pathname + location.search);
    $$("#feeders tbody tr").forEach((tr) => tr.setAttribute("aria-selected", "false"));
    if (map && map.getLayer("tx-sel")) map.setFilter("tx-sel", ["==", ["get", "f"], ""]);
  }

  function initMap(tx, towns) {
    const pts = [];
    const walk = (c) => (typeof c[0] === "number" ? pts.push(c) : c.forEach(walk));
    towns.features.forEach((f) => walk(f.geometry.coordinates));
    const xs = pts.map((p) => p[0]), ys = pts.map((p) => p[1]);
    map = new maplibregl.Map({
      container: "map",
      style: BASEMAP,
      bounds: [[Math.min(...xs), Math.min(...ys)], [Math.max(...xs), Math.max(...ys)]],
      fitBoundsOptions: { padding: 16 },
      minZoom: 7,
      maxZoom: 17,
      attributionControl: { compact: true, customAttribution: "資料：台灣電力公司、內政部國土測繪中心" },
      localIdeographFontFamily: "'PingFang TC', 'Noto Sans TC', 'Microsoft JhengHei', sans-serif",
      cooperativeGestures: window.matchMedia("(pointer: coarse)").matches,
    });
    map.addControl(new maplibregl.NavigationControl({ showCompass: false }), "top-right");
    map.addControl(new maplibregl.ScaleControl({ unit: "metric" }), "bottom-left");

    // If the basemap style cannot be fetched (offline, blocked network), keep the data layers on a plain background.
    let noBasemap = false;
    map.on("error", () => {
      if (noBasemap || map.isStyleLoaded()) return;
      noBasemap = true;
      map.setStyle({ version: 8, sources: {}, layers: [{ id: "bg", type: "background", paint: { "background-color": "#eef0f0" } }] });
      $(".map-col").insertAdjacentHTML("afterbegin", '<p class="map-warn" role="status">底圖無法載入（可能沒有網路或被防火牆阻擋），以下只顯示饋線與鄉鎮界線。</p>');
    });

    map.on("load", () => {
      // Basemap labels: Chinese names only.
      for (const l of map.getStyle().layers) {
        const tf = l.layout && l.layout["text-field"];
        if (tf && JSON.stringify(tf).includes("name")) {
          map.setLayoutProperty(l.id, "text-field", ["coalesce", ["get", "name:zh-Hant"], ["get", "name:zh"], ["get", "name"]]);
        }
      }
      const labels = {
        type: "FeatureCollection",
        features: towns.features.map((f) => ({
          type: "Feature",
          geometry: { type: "Point", coordinates: [f.properties.label_lon, f.properties.label_lat] },
          properties: { name: f.properties.name },
        })),
      };
      map.addSource("towns", { type: "geojson", data: towns });
      map.addSource("town-labels", { type: "geojson", data: labels });
      map.addSource("tx", { type: "geojson", data: tx });

      const color = ["match", ["get", "c"], 0, COLORS[0], 1, COLORS[1], 2, COLORS[2], 3, COLORS[3], 4, COLORS[4], "rgba(0,0,0,0)"];
      const radius = ["interpolate", ["linear"], ["zoom"], 8, 0.9, 10, 1.5, 12, 2.6, 14, 4.2, 16, 6.5];
      map.addLayer({ id: "town-line", type: "line", source: "towns", paint: { "line-color": "#9a988f", "line-width": ["interpolate", ["linear"], ["zoom"], 8, 0.6, 13, 1.4] } });
      map.addLayer({
        id: "tx", type: "circle", source: "tx",
        // No class is drawn on top: stacking one class above the rest would exaggerate its area.
        layout: { "circle-sort-key": ["case", ["==", ["get", "c"], -1], 0, 1] },
        paint: {
          "circle-color": color,
          "circle-radius": radius,
          "circle-opacity": 0.92,
          "circle-stroke-width": ["case", ["==", ["get", "c"], -1], 1, 0],
          "circle-stroke-color": COLORS[-1],
        },
      });
      map.addLayer({
        id: "tx-sel", type: "circle", source: "tx", filter: ["==", ["get", "f"], ""],
        paint: {
          "circle-color": color,
          "circle-radius": ["interpolate", ["linear"], ["zoom"], 8, 3.8, 10, 4.6, 12, 5.7, 14, 7.3, 16, 9.5],
          "circle-stroke-width": 1.6,
          "circle-stroke-color": "#0b0b0b",
        },
      });
      // Text layers need the basemap's glyph server; skip labels when it is unavailable.
      if (!noBasemap) map.addLayer({
        id: "town-label", type: "symbol", source: "town-labels",
        layout: { "text-field": ["get", "name"], "text-size": ["interpolate", ["linear"], ["zoom"], 8, 11, 12, 14], "text-allow-overlap": false },
        paint: { "text-color": "#3d3c39", "text-halo-color": "rgba(252,252,251,0.95)", "text-halo-width": 1.6 },
      });
      applyClassFilter();

      popup = new maplibregl.Popup({ closeButton: false, closeOnClick: false, offset: 8 });
      const hit = (pt) => map.queryRenderedFeatures([[pt.x - 6, pt.y - 6], [pt.x + 6, pt.y + 6]], { layers: ["tx"] });
      map.on("mousemove", (e) => {
        const fs = hit(e.point);
        map.getCanvas().style.cursor = fs.length ? "pointer" : "";
        if (!fs.length) { popup.remove(); return; }
        const f = byId[fs[0].properties.f];
        if (!f) return;
        popup.setLngLat(e.lngLat).setHTML(
          `<div class="pop"><b>${esc(feederName(f.feeder))}</b>　${f.capacity_kw == null ? NO_CAP_LABEL : `${fmt(f.capacity_kw, 1)} kW`}${f.capacity_source === "S01" ? `（${esc(f.capacity_county)}）` : ""}<br><small>點選看詳細與引用</small></div>`
        ).addTo(map);
      });
      map.on("mouseout", () => popup.remove());
      map.on("click", (e) => {
        const fs = hit(e.point);
        if (fs.length) selectFeeder(fs[0].properties.f);
      });

      const m = location.hash.match(/feeder=([^&]+)/);
      if (m) selectFeeder(decodeURIComponent(m[1]), { fly: true });
    });
  }

  function bindCopy() {
    document.addEventListener("click", async (e) => {
      const b = e.target.closest("[data-copy], [data-copy-text]");
      if (!b) return;
      const text = b.dataset.copyText ?? document.getElementById(b.dataset.copy).textContent;
      const label = b.textContent;
      try {
        await navigator.clipboard.writeText(text);
        b.textContent = "已複製";
      } catch {
        b.textContent = "請手動選取";
      }
      b.classList.add("done");
      setTimeout(() => { b.textContent = label; b.classList.remove("done"); }, 1600);
    });
  }

  function bindStandalone() {
    if (window.__PREVIEW__) {
      document.body.insertAdjacentHTML("afterbegin",
        `<div class="preview-banner" role="note">${esc(window.__PREVIEW__)}</div>`);
    }
    if (!window.__METHODS_HTML__) return;
    let url;
    document.addEventListener("click", (e) => {
      const a = e.target.closest('a[href^="methods.html"]');
      if (!a) return;
      e.preventDefault();
      url ||= URL.createObjectURL(new Blob([window.__METHODS_HTML__], { type: "text/html;charset=utf-8" }));
      window.open(url + (a.hash || ""), "_blank");
    });
  }

  bindStandalone();
  load().catch((err) => {
    console.error(err);
    $("#dates").textContent = "資料載入失敗，請重新整理。";
  });
})();
