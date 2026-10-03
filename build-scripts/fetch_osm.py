#!/usr/bin/env python3
"""用 Overpass API 抓 OSM 台灣鐵路 relation、車站、海岸線到 data/map/raw/。

資料授權：© OpenStreetMap contributors，ODbL 1.0。
用法：/workspace/map-venv/bin/python data/map/fetch_osm.py [只抓的查詢名 ...]
"""
from __future__ import annotations
import datetime as dt
import json
import time
from pathlib import Path
import requests

HERE = Path(__file__).resolve().parent
RAW = HERE / "raw"
# overpass-api.de 的 TLS 在 box 上會被斷線，改用 http；第二個是 mail.ru 鏡像。
ENDPOINTS = [
    "http://overpass-api.de/api/interpreter",
    "https://maps.mail.ru/osm/tools/overpass/api/interpreter",
]
BBOX = "21.85,119.25,25.35,122.10"  # 南,西,北,東：台灣本島＋澎湖＋綠島蘭嶼
# 實測得到的 relation id（2026-10-01 以 route~railway|train 列出 279 筆後挑選）
RELATIONS = {
    "thsr_south": 4500371,   # 台灣高速鐵路(南向)  route=railway service=high_speed
    "thsr_north": 4500369,   # 台灣高速鐵路(北向)
    "tra_wl_south": 5867234, # 縱貫線(南下) route=railway
    "tra_wl_north": 5867233, # 縱貫線(北上)
    "tra_taichung": 5571202, # 臺中線（山線，竹南—彰化）
    "tra_coast": 1827334,    # 海岸線（比對用，不打包）
}
QUERIES = {
    "osm_rail_relations": f"""[out:json][timeout:180];
(relation(id:{','.join(str(v) for v in RELATIONS.values())});)->.r;
.r out body;
way(r.r)->.w;
.w out body;
node(w.w);
out skel qt;""",
    # 全台 railway=rail（含 service=* 側線），用來補 relation 斷口；建圖時 relation 邊權重 1、非 relation 邊加罰
    "osm_rail_ways": f"""[out:json][timeout:300];
way["railway"="rail"]({BBOX})->.w;
.w out body;
node(w.w);
out skel qt;""",
    "osm_stations": f"""[out:json][timeout:180];
(
  nwr["railway"="station"]({BBOX});
  nwr["public_transport"="station"]["train"="yes"]({BBOX});
  nwr["railway"="halt"]({BBOX});
);
out center tags;""",
    "osm_coastline": f"""[out:json][timeout:300];
way["natural"="coastline"]({BBOX});
out geom;""",
}


def run(q: str) -> bytes:
    last = None
    for url in ENDPOINTS:
        for attempt in range(2):
            try:
                r = requests.post(url, data={"data": q}, timeout=400,
                                  headers={"User-Agent": "tw-rail-focus-research/0.1"})
                if r.status_code == 200 and r.content.lstrip().startswith(b"{"):
                    return r.content
                last = f"{url} HTTP {r.status_code} {r.content[:120]!r}"
            except requests.RequestException as e:  # noqa: PERF203
                last = f"{url} {e}"
            time.sleep(5)
    raise SystemExit(f"Overpass failed: {last}")


def main() -> None:
    import sys
    only = set(sys.argv[1:])
    RAW.mkdir(parents=True, exist_ok=True)
    stamp = dt.datetime.now().strftime("%Y%m%d")
    manifest = {"fetched_at": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
                "license": "ODbL 1.0 © OpenStreetMap contributors", "relations": RELATIONS, "files": {}}
    old = RAW / f"osm_manifest_{stamp}.json"
    if only and old.exists():
        manifest["files"] = json.loads(old.read_text())["files"]
    for name, q in QUERIES.items():
        if only and name not in only:
            continue
        body = run(q)
        d = json.loads(body)
        out = RAW / f"{name}_{stamp}.json"
        out.write_bytes(body)
        manifest["files"][name] = {"file": out.name, "bytes": len(body),
                                   "elements": len(d.get("elements", [])),
                                   "osm_base": d.get("osm3s", {}).get("timestamp_osm_base"), "query": q}
        print(f"{name}: {len(body)} bytes, {len(d.get('elements', []))} elements, base={manifest['files'][name]['osm_base']}")
    (RAW / f"osm_manifest_{stamp}.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
