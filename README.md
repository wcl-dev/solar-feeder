# 台灣太陽光電併網瓶頸觀測站

雲林縣配電饋線的**台電公布可併網容量**現況地圖。每個數字都附資料日期、來源與存檔雜湊，可直接引用。

- 網站：（GitHub Pages 網址，發布後填入）
- 方法與限制：`web/methods.html`
- 資料審計：[`docs/audit/S02_capacity_audit.md`](docs/audit/S02_capacity_audit.md)
- 決策與 spec 修正：[`docs/decisions.md`](docs/decisions.md)

> 本站為獨立整理，不代表台電或政府機關立場，也不是正式併網判定工具。公布容量低或為 0，不代表新案一定無法併網。

## 資料

| ID | 來源 | 用途 |
|---|---|---|
| S02 | 台灣電力公司〈雲林縣再生能源可併網容量〉（[data.gov.tw/dataset/44208](https://data.gov.tw/dataset/44208)） | 饋線容量與變壓器點位（核心） |
| S01 | 台灣電力公司〈全國各縣市再生能源可併網容量〉（[161874](https://data.gov.tw/dataset/161874)） | 交叉檢查 |
| S12 | 內政部國土測繪中心〈鄉鎮市區界線(TWD97經緯度)〉（[7441](https://data.gov.tw/dataset/7441)） | 鄉鎮歸屬、底圖參考 |
| S04 | 立法院預算中心〈115年度中央政府總預算案整體評估報告〉一二、表2 | 雲林區處等待併網（區處層級） |

原始檔存於 `data/raw/<來源>/<檔案日期>/`，設為唯讀，取得紀錄與 SHA-256 在 `data/manifests/snapshots.csv`；每次檢查來源都記錄在 `data/manifests/checks.csv`。人工擷取的表格在 `data/curated/`，附出處與核對說明。

## 重建

需要 [uv](https://docs.astral.sh/uv/)。

```bash
uv sync
uv run python src/ingest/fetch_snapshots.py   # 檢查來源、存檔新版本（--force 忽略排程與條件式請求）
uv run python src/transform/build_yunlin.py   # 產生 web/data/ 與審計報告
uv run python src/transform/build_queue.py    # 產生 web/data/queue.json
python3 -m http.server 8765 --directory web    # 本機預覽 http://localhost:8765
```

`build_yunlin.py` 會先驗證座標系統（TWD67 點位必須全部落在陸地上），驗證不通過就停止建置。網站只使用 `data/manifests/published.json` 裡核准過的存檔版本。

## 自動更新

台電更新時會覆寫同一網址，漏掉一版就找不回來，所以來源每週檢查一次（`.github/workflows/weekly-check.yml`，週一 09:00 台灣時間）。

| 來源 | 檢查 | 發現新版時 |
|---|---|---|
| S01／S02 台電可併網容量 | 每週，條件式請求（沒變只收到 304，不下載） | 原始檔立即存檔到 main、請 Wayback 另存一份，並開「新版資料待審」PR；**合併 PR 才上線** |
| S12 鄉鎮界線 | 每 90 天比對 data.gov.tw 上的下載網址 | 印出提醒，人工存檔 |
| S04 立法院報告 | 每年 9 月 15 日自動開 issue 提醒 | 人工擷取、核對（`data/curated/`） |

網站頁首顯示「本站最後檢查來源」；超過 45 天沒有成功檢查會出現提醒。

手動操作：

```bash
uv run python src/ingest/approve.py --pending          # 已存檔、待審核的版本
uv run python src/report/summarize_change.py           # 待審版本與目前上線版本的差異摘要
uv run python src/ingest/approve.py S02-2026-10-05     # 核准上線（之後重建並 commit）
```

### 推上 GitHub 時要開的設定

1. Settings → Pages → Source 選 **GitHub Actions**。
2. Settings → Actions → General → Workflow permissions 選 **Read and write**，並勾選 **Allow GitHub Actions to create and approve pull requests**（否則無法自動開審核 PR）。
3. 公開 repo 若 60 天沒有活動，GitHub 會停用排程；每週的檢查紀錄 commit 應可避免，頁首的逾期提醒是最後一道保險。
4. 若本機也設了每週排程，上 GitHub 後請關掉，避免兩邊同時改 `data/manifests/` 造成衝突。

## 結構

```text
data/raw/         原始檔存檔（唯讀）
data/curated/     人工擷取表格＋核對說明
data/manifests/   存檔、檢查、核准與 Wayback 備份紀錄
docs/             審計報告、決策紀錄
src/ingest/       下載與存檔
src/transform/    清理、座標轉換、輸出
src/report/       新版審核摘要
src/publish/      單檔預覽版打包
web/              靜態網站（GitHub Pages 發布此資料夾）
```

## 授權

- 程式碼：MIT（見 `LICENSE`）。
- 資料：台電與內政部資料依[政府資料開放授權條款－第1版](https://data.gov.tw/license)利用並顯名；立法院報告依其網站資料開放宣告註明出處。本專案的衍生資料（`web/data/`）沿用相同顯名要求。
- 底圖：OpenFreeMap、© OpenStreetMap 貢獻者。
