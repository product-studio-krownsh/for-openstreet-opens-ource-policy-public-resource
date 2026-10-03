# for-openstreet-opens-ource-policy-public-resource
資料地圖授權用公開資料

## 島嶼慢車 地圖資料（Island Slow Train map data）

iOS App「島嶼慢車」內建離線地圖所用的**衍生資料庫**（Derivative Database）公開副本。依 [Open Database License (ODbL) 1.0](https://opendatacommons.org/licenses/odbl/1-0/) §4.6／§4.7，App Store 版本受技術保護措施（FairPlay）限制，因此在此另行提供一份免費、不受限制的副本與建置方法。

> 地圖資料 © OpenStreetMap contributors（© OpenStreetMap 貢獻者），依 ODbL 1.0 授權。<https://www.openstreetmap.org/copyright>

**更新日期：2026-10-03**（pack 建置：2026-10-01；OSM 資料時間：2026-10-01T01:54:00Z）

### 內容

| 檔案 | 說明 |
|---|---|
| `tw.sqlite` | 地圖 pack（`network_id = tw`，`pack_version = 2026.10-osm`，`format = slw-mappack/1`，`PRAGMA user_version = 1`）。與 App 內建版本逐位元相同 |
| `manifest.json` | pack 清單（版本、SHA-256、大小、bbox） |
| `LICENSE` | ODbL 1.0 全文（適用於本資料庫） |
| `build-scripts/fetch_osm.py` | 以 Overpass API 抓取 OSM 鐵路 relation／way、車站與海岸線 |
| `build-scripts/build_map_pack.py` | 組線形、切段、簡化並輸出 pack（含建置閘門） |

SHA-256（`tw.sqlite`）：`f6637d8b074fa20e21ea59401bf4de9ab053882264e9724fc54809ff323fe84b`

#### `tw.sqlite` 資料表

| 資料表 | 筆數 | 內容 |
|---|---|---|
| `pack_meta` | — | 版本、編碼、容差、OSM 資料時間與 relation、授權與顯名文字 |
| `land` | 62 | 海岸線島形（lod0／lod1），Google Encoded Polyline（precision 5，lat,lon，WGS84） |
| `rail_segment` | 68 | 依站間切段的軌道線形（lod0／lod1） |
| `station_anchor` | 36 | 站點投影到 OSM 軌道線上的位置（高速鐵路 12 站、縱貫線山線主要站 24 站） |

### 產生方式

1. 以 Overpass API 取得 OSM `railway=rail`（relation 4500369、5867234、5571202，斷口以全台 `railway=rail` 補上）與 `natural=coastline`（bbox 21.85–25.35N／119.25–122.10E）。
2. 依站間切段；以 Douglas–Peucker 簡化（lod0：軌道 100 m／海岸 300 m；lod1：軌道 10 m／海岸 30 m）；以區域遮罩排除 bbox 內的非台灣島嶼。
3. 站點座標取自交通部 TDX 平臺車站資料（StationPosition），投影到 OSM 軌道線上，記錄於 `station_anchor`。
4. 座標以 Google Encoded Polyline（precision 5，約 1.1 m）存放。

`build_map_pack.py` 另需 TDX 車站／路線資料與 App 的車站參考資料庫（`reference.sqlite`，政府資料開放授權）作為輸入與比對；這些不屬於 ODbL 衍生資料庫，本 repo 未附，請自行向 [TDX 運輸資料流通服務](https://tdx.transportdata.tw/) 以自己的會員金鑰取得。Natural Earth 與國土測繪中心資料僅用於比對，未打包。腳本路徑與參數為原始建置環境的紀錄，可能需依你的環境調整。

### 授權與顯名

- **本資料庫（`tw.sqlite`）**：依 ODbL 1.0 授權，全文見 [`LICENSE`](LICENSE)。使用、改作或公開散布時，請保留「© OpenStreetMap contributors」顯名並依 ODbL 以相同授權釋出衍生資料庫。
- **地圖資料來源**：© OpenStreetMap contributors，<https://www.openstreetmap.org/copyright>。
- **站點座標來源**：交通部 TDX 平臺（資料介接「交通部TDX平臺」），依「[政府資料開放授權條款－第1版](https://data.gov.tw/license)」釋出。
- `build-scripts/` 內的腳本僅為建置方法說明之用。

### 聲明

本專案與交通部、國營臺灣鐵路股份有限公司、台灣高速鐵路股份有限公司無任何關聯，亦未獲其背書。資料為示意用途，不保證精確，不提供即時列車資訊。

---

### English summary

Public copy of the **derivative database** used for the offline map in the iOS app "島嶼慢車" (Island Slow Train), published under the [Open Database License (ODbL) 1.0](https://opendatacommons.org/licenses/odbl/1-0/) (§4.6/§4.7), because the App Store build is distributed with technical protection measures.

- **Contents:** `tw.sqlite` (simplified Taiwan rail lines — HSR and the TRA Western Line mountain route — coastline polygons, and 36 station anchors, encoded as Google Encoded Polyline precision 5), `manifest.json`, the full ODbL 1.0 text in `LICENSE`, and the build scripts in `build-scripts/`.
- **How it was made:** OSM `railway=rail` and `natural=coastline` fetched via Overpass (data timestamp 2026-10-01T01:54:00Z), split between stations, simplified with Douglas–Peucker (two LODs); station positions from MOTC TDX projected onto the OSM lines.
- **Attribution:** Map data © OpenStreetMap contributors, ODbL 1.0 — <https://www.openstreetmap.org/copyright>. Station positions: MOTC TDX, Open Government Data License, version 1.0 (<https://data.gov.tw/license>).
- **Disclaimer:** Not affiliated with or endorsed by the Ministry of Transportation and Communications, Taiwan Railway Corporation, or Taiwan High Speed Rail Corporation.
- **Last updated:** 2026-10-03.
