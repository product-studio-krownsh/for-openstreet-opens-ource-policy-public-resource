#!/usr/bin/env python3
"""tw-rail-focus 地圖 pack 研究版建置：比對站點、組線形、簡化、量大小、輸出樣本 pack。

輸入：data/map/raw/（fetch_tdx.py、fetch_osm.py、fetch_naturalearth.py 的輸出）＋ data/final/reference.sqlite
輸出：data/map/out/
  station_compare.csv      36 站 TDX vs OSM 距離與站點到線距離
  line_compare.json        OSM 線形 vs TDX Shape 偏差統計
  coast_compare.json       OSM 海岸線 vs Natural Earth 10m 偏差統計
  size_matrix.json         各容差下頂點數與編碼大小
  mappack-tw.sqlite        研究版 map pack（ODbL 部分；格式見 MAP_SPEC.md）
  preview.geojson          除錯用（未簡化過的組合線＋站點）
用法：/workspace/map-venv/bin/python data/map/build_map_pack.py
"""
from __future__ import annotations
import csv
import datetime as dt
import heapq
import json
import math
import sqlite3
import zlib
from pathlib import Path

import shapefile  # pyshp
from pyproj import Transformer
from shapely import wkt
from shapely.geometry import LineString, MultiLineString, Point, Polygon, box, mapping, shape
from shapely.ops import linemerge, polygonize, substring, transform, unary_union

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent  # data/
RAW = HERE / "raw"
OUT = HERE / "out"
REF = ROOT / "final" / "reference.sqlite"
STAMP = "20261001"

FWD = Transformer.from_crs("EPSG:4326", "EPSG:3826", always_xy=True)  # TWD97 TM2，公尺
INV = Transformer.from_crs("EPSG:3826", "EPSG:4326", always_xy=True)
to_m = lambda g: transform(FWD.transform, g)  # noqa: E731
to_ll = lambda g: transform(INV.transform, g)  # noqa: E731

FLAG_M = 200.0
RAIL_TOL = {"lod0": 100.0, "lod1": 10.0}    # Douglas–Peucker 容差（公尺）
COAST_TOL = {"lod0": 300.0, "lod1": 30.0}
COAST_MIN_AREA = {"lod0": 2.0e6, "lod1": 0.05e6}  # 小於此面積（m²）的島嶼不收
# network 範圍遮罩：bbox 西北角會掃到福建外海島嶼（南日島等，2026-10-01 實測），只收島形重心落在遮罩內的陸地
REGION_MASK = [box(119.95, 21.85, 122.10, 25.35),  # 本島＋綠島蘭嶼小琉球龜山島
               box(119.25, 23.10, 119.80, 23.85)]  # 澎湖
PACK_LINES = {"hsr": ["thsr_north"], "wl_mountain": ["tra_wl_south", "tra_taichung"]}  # 南向 relation 4500371 在南港—松山間有約 100 m 斷口（2026-10-01 實測），改用北向 4500369（單一連通）
TDX_SHAPE_FOR = {"hsr": ("thsr", ["HSRL"]), "wl_mountain": ("tra", ["TL-N", "TL-M", "TL-S"])}


def hav(lon1, lat1, lon2, lat2) -> float:
    r = 6371008.8
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def encode_polyline(coords, precision=5) -> str:
    """Google Encoded Polyline（lat,lon 順序），precision 5 ≈ 1.1 m。"""
    f = 10 ** precision
    out, plat, plon = [], 0, 0
    for lon, lat in coords:
        ilat, ilon = round(lat * f), round(lon * f)
        for v in (ilat - plat, ilon - plon):
            v = ~(v << 1) if v < 0 else (v << 1)
            while v >= 0x20:
                out.append(chr((0x20 | (v & 0x1F)) + 63)); v >>= 5
            out.append(chr(v + 63))
        plat, plon = ilat, ilon
    return "".join(out)


def load_json(name):
    return json.loads((RAW / f"{name}_{STAMP}.json").read_text(encoding="utf-8"))


# ---------- 1. 站點 ----------
def reference_stations():
    con = sqlite3.connect(REF)
    rows = con.execute("""SELECT ls.line_id, ls.seq, s.id, s.source, s.source_id, s.name_zh, s.name_en, ls.cumulative_km
                          FROM line_station ls JOIN station s ON s.id = ls.station_id ORDER BY ls.line_id, ls.seq""").fetchall()
    con.close()
    return [dict(zip(["line_id", "seq", "id", "source", "source_id", "name_zh", "name_en", "km"], r)) for r in rows]


def tdx_positions():
    pos = {}
    for r in load_json("tdx_thsr_station"):
        p = r["StationPosition"]; pos[f"hsr:{r['StationID']}"] = (p["PositionLon"], p["PositionLat"], r.get("UpdateTime"))
    tra = load_json("tdx_tra_station")
    for r in tra["Stations"]:
        p = r["StationPosition"]; pos[f"tra:{r['StationID']}"] = (p["PositionLon"], p["PositionLat"], tra.get("UpdateTime"))
    return pos


def osm_station_index():
    els = load_json("osm_stations")["elements"]
    idx = {}
    for e in els:
        t = e.get("tags", {})
        if t.get("railway") != "station" or e["type"] != "node":
            continue
        op = t.get("operator", "") + t.get("network", "")
        lon, lat = e["lon"], e["lat"]
        if "高速鐵路" in op or "高鐵" in op:
            idx[("hsr", t.get("name", "").replace("臺", "台"))] = (lon, lat, e["id"], t.get("name"))
        elif ("鐵路" in op or "TRA" in op or "臺鐵" in op) and t.get("ref", "").isdigit():
            idx[("tra", int(t["ref"]))] = (lon, lat, e["id"], t.get("name"))
    return idx


# ---------- 2. 線形：relation → 圖 → 依站序最短路 ----------
REL_W, OTHER_W, SERVICE_W = 1.0, 3.0, 10.0  # relation 邊優先；其他正線加罰 3 倍；側線／站場 10 倍


def rail_graph(rel_keys):
    """relation 成員邊權重 1；其餘 railway=rail 邊加罰，只用來補 relation 斷口。"""
    d = load_json("osm_rail_relations")["elements"]
    allw = load_json("osm_rail_ways")["elements"]
    manifest = json.loads((RAW / f"osm_manifest_{STAMP}.json").read_text())
    rel_ids = {manifest["relations"][k] for k in rel_keys}
    nodes = {e["id"]: (e["lon"], e["lat"]) for e in d + allw if e["type"] == "node"}
    ways = {e["id"]: e for e in d + allw if e["type"] == "way"}
    rel_way_ids = set()
    for r in (e for e in d if e["type"] == "relation" and e["id"] in rel_ids):
        rel_way_ids |= {m["ref"] for m in r["members"] if m["type"] == "way" and m["role"] == ""}
    adj: dict[int, list[tuple[int, float, float]]] = {}
    for wid, w in ways.items():
        t = w.get("tags", {})
        if t.get("railway") != "rail":
            continue
        k = REL_W if wid in rel_way_ids else (SERVICE_W if t.get("service") else OTHER_W)
        ns = w["nodes"]
        for a, b in zip(ns, ns[1:]):
            da = hav(*nodes[a], *nodes[b])
            adj.setdefault(a, []).append((b, da * k, da)); adj.setdefault(b, []).append((a, da * k, da))
    rel_nodes = {n for wid in rel_way_ids if wid in ways for n in ways[wid]["nodes"]}
    return nodes, adj, rel_nodes


def dijkstra(adj, s, t):
    dist, prev, pq = {s: 0.0}, {}, [(0.0, s)]
    while pq:
        dd, u = heapq.heappop(pq)
        if u == t:
            break
        if dd > dist.get(u, 1e18):
            continue
        for v, w, _ in adj.get(u, ()):
            nd = dd + w
            if nd < dist.get(v, 1e18):
                dist[v], prev[v] = nd, u; heapq.heappush(pq, (nd, v))
    if t not in dist:
        raise SystemExit(f"no path {s}->{t}")
    path = [t]
    while path[-1] != s:
        path.append(prev[path[-1]])
    path = path[::-1]
    real = sum(next(r for v, _, r in adj[a] if v == b) for a, b in zip(path, path[1:]))
    return path, real


LEG_RATIO = (0.92, 1.10)  # 站間路徑長 ÷ 官方里程差；里程樁有斷鏈（改線後未重編），>3% 只記錄不擋


def build_line(line_id, stations, pos):
    nodes, adj, rel_nodes = rail_graph(PACK_LINES[line_id])
    # 只吸附到 relation 成員上的節點，避免吸到側線
    gnodes = [n for n in rel_nodes if n in adj]
    def nearest(lon, lat):
        best = min(gnodes, key=lambda n: (nodes[n][0] - lon) ** 2 + ((nodes[n][1] - lat) * 1.1) ** 2)
        return best, hav(lon, lat, *nodes[best])
    wps = [nearest(*pos[s["id"]][:2]) for s in stations]
    path, legs = [wps[0][0]], []
    for i, ((a, _), (b, _)) in enumerate(zip(wps, wps[1:])):
        p, real = dijkstra(adj, a, b); path += p[1:]
        km = stations[i + 1]["km"] - stations[i]["km"]
        ratio = real / 1000 / km
        legs.append({"from": stations[i]["name_zh"], "to": stations[i + 1]["name_zh"], "path_km": round(real / 1000, 2),
                     "mileage_km": round(km, 2), "ratio": round(ratio, 3)})
        if not (LEG_RATIO[0] <= ratio <= LEG_RATIO[1]):
            raise SystemExit(f"{line_id} {legs[-1]} 超出里程比例 {LEG_RATIO}")
    # 這條線用到多少非 relation 的補洞邊
    patched = 0.0
    for a, b in zip(path, path[1:]):
        w, r = next((w, r) for v, w, r in adj[a] if v == b)
        if w > r * 1.0001:
            patched += r
    line_ll = LineString([nodes[n] for n in path])
    return line_ll, [w[1] for w in wps], legs, round(patched, 1)


# ---------- 3. 海岸線 ----------
def osm_land():
    els = load_json("osm_coastline")["elements"]
    lines = [LineString([(g["lon"], g["lat"]) for g in e["geometry"]]) for e in els if e["type"] == "way"]
    merged = linemerge(lines)
    polys = list(polygonize(merged))
    mask = unary_union(REGION_MASK)
    return [p for p in polys if p.is_valid and mask.contains(p.representative_point())], merged


def ne_land(bounds):
    out = []
    for name in ("ne_10m_land", "ne_10m_minor_islands"):
        sf = shapefile.Reader(str(RAW / "ne" / name / f"{name}.shp"))
        for shp in sf.shapes():
            g = shape(shp.__geo_interface__)
            if g.intersects(bounds):
                out.append(g.intersection(bounds))
    return unary_union(out)


def nlsc_land(bounds):
    """國土測繪中心縣市界（OGDL）聯集成陸地，作為非 ODbL 海岸線備案的比對。"""
    shp = RAW / "nlsc" / "COUNTY_MOI_1090820.shp"
    if not shp.exists():
        return None
    sf = shapefile.Reader(str(shp), encoding="utf-8")
    gs = [shape(x.__geo_interface__).buffer(0) for x in sf.shapes()]
    return unary_union([g.intersection(bounds) for g in gs if g.intersects(bounds)])


def sample_dist(a_m, b_m, step=100.0):
    """a 上每 step 公尺取樣，量到 b 的距離。"""
    n = max(2, int(a_m.length // step))
    ds = sorted(b_m.distance(a_m.interpolate(i / (n - 1), normalized=True)) for i in range(n))
    return {"samples": n, "mean_m": round(sum(ds) / n, 1), "p50_m": round(ds[n // 2], 1),
            "p95_m": round(ds[int(n * 0.95)], 1), "max_m": round(ds[-1], 1)}


def simplify_ll(g_ll, tol_m):
    return to_ll(to_m(g_ll).simplify(tol_m, preserve_topology=True))


def coords_of(g):
    if g.geom_type == "LineString":
        return list(g.coords)
    if g.geom_type == "Polygon":
        return list(g.exterior.coords)
    raise ValueError(g.geom_type)


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    ref = reference_stations()
    pos = tdx_positions()
    osm_idx = osm_station_index()

    # 線形
    lines, snap, legs, patched = {}, {}, {}, {}
    for line_id in PACK_LINES:
        st = [s for s in ref if s["line_id"] == line_id]
        lines[line_id], snap[line_id], legs[line_id], patched[line_id] = build_line(line_id, st, pos)

    # 站點比對
    rows = []
    for s in ref:
        lon, lat, upd = pos[s["id"]]
        key = ("hsr", s["name_zh"].replace("臺", "台")) if s["source"] == "TDX-THSR" else ("tra", int(s["source_id"]))
        o = osm_idx.get(key)
        line_m = to_m(lines[s["line_id"]])
        rows.append({
            "line_id": s["line_id"], "seq": s["seq"], "station_id": s["id"], "name_zh": s["name_zh"],
            "tdx_lon": lon, "tdx_lat": lat,
            "osm_node": o[2] if o else "", "osm_name": o[3] if o else "",
            "osm_lon": o[0] if o else "", "osm_lat": o[1] if o else "",
            "tdx_osm_m": round(hav(lon, lat, o[0], o[1]), 1) if o else "",
            "tdx_to_line_m": round(line_m.distance(to_m(Point(lon, lat))), 1),
            "osm_to_line_m": round(line_m.distance(to_m(Point(o[0], o[1]))), 1) if o else "",
            "flag_gt_200m": "YES" if o and hav(lon, lat, o[0], o[1]) > FLAG_M else "",
        })
    with (OUT / "station_compare.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0])); w.writeheader(); w.writerows(rows)

    # 站點在線上的位置（公尺），用來切段
    measures = {}
    for line_id, g in lines.items():
        gm = to_m(g)
        ms = [gm.project(to_m(Point(*pos[s["id"]][:2]))) for s in ref if s["line_id"] == line_id]
        if any(b <= a for a, b in zip(ms, ms[1:])):
            raise SystemExit(f"{line_id}: 站點投影不是嚴格遞增 {ms}")
        measures[line_id] = ms

    # 線形 vs TDX Shape
    tdx_shapes = {"thsr": load_json("tdx_thsr_shape"), "tra": load_json("tdx_tra_shape")["Shapes"]}
    line_cmp = {}
    for line_id, (src, ids) in TDX_SHAPE_FOR.items():
        parts = [wkt.loads(x["Geometry"]) for x in tdx_shapes[src] if x["LineID"] in ids]
        tdx_g = unary_union([to_m(p) for p in parts])
        osm_g = to_m(lines[line_id])
        # TDX 只取 OSM 線 2 km 緩衝內（排除延伸到站外的部分）
        tdx_clip = tdx_g.intersection(osm_g.buffer(2000))
        line_cmp[line_id] = {
            "osm_length_km": round(osm_g.length / 1000, 2),
            "tdx_shape_ids": ids,
            "tdx_shape_update": sorted({x.get("UpdateTime") for x in tdx_shapes[src] if x["LineID"] in ids}),
            "osm_to_tdx": sample_dist(osm_g, tdx_g),
            "tdx_to_osm": sample_dist(linemerge(tdx_clip) if tdx_clip.geom_type == "MultiLineString" else tdx_clip, osm_g),
            "station_snap_node_m": [round(x, 1) for x in snap[line_id]],
            "legs_vs_mileage": legs[line_id],
            "patched_non_relation_m": patched[line_id],
        }
    # 找偏差大的區段（osm→tdx > 50 m），方便人工查
    for line_id, (src, ids) in TDX_SHAPE_FOR.items():
        parts = [wkt.loads(x["Geometry"]) for x in tdx_shapes[src] if x["LineID"] in ids]
        tdx_g = unary_union([to_m(p) for p in parts]); osm_g = to_m(lines[line_id])
        bad, cur = [], None
        n = int(osm_g.length // 100)
        for i in range(n + 1):
            p = osm_g.interpolate(i * 100.0); dd = tdx_g.distance(p)
            if dd > 50:
                if cur is None:
                    cur = [i * 100.0, i * 100.0, dd]
                cur[1] = i * 100.0; cur[2] = max(cur[2], dd)
            elif cur is not None:
                bad.append(cur); cur = None
        if cur: bad.append(cur)
        line_cmp[line_id]["osm_vs_tdx_gt50m_runs"] = [
            {"from_km": round(a / 1000, 1), "to_km": round(b / 1000, 1), "max_m": round(m, 0),
             "at_lonlat": [round(c, 5) for c in to_ll(osm_g.interpolate((a + b) / 2)).coords[0]]}
            for a, b, m in bad if b - a >= 0]
    (OUT / "line_compare.json").write_text(json.dumps(line_cmp, ensure_ascii=False, indent=2))

    # 海岸線
    polys, _ = osm_land()
    main_isl = max(polys, key=lambda p: to_m(p).area)
    bounds = box(119.25, 21.85, 122.10, 25.35)
    ne = ne_land(bounds)
    ne_main = max((g for g in getattr(ne, "geoms", [ne])), key=lambda p: to_m(p).area)
    coast_cmp = {
        "osm_polygons": len(polys),
        "osm_main_island_area_km2": round(to_m(main_isl).area / 1e6, 1),
        "ne_main_island_area_km2": round(to_m(ne_main).area / 1e6, 1),
        "osm_main_coast_km": round(to_m(main_isl).exterior.length / 1000, 1),
        "ne_main_coast_km": round(to_m(ne_main).exterior.length / 1000, 1),
        "ne_main_vertices": len(ne_main.exterior.coords),
        "osm_main_vertices": len(main_isl.exterior.coords),
        "ne_to_osm": sample_dist(to_m(ne_main).exterior, to_m(main_isl).exterior, 200),
        "stations_outside_ne_land": [s["name_zh"] for s in ref if not ne_main.buffer(0).contains(Point(*pos[s["id"]][:2]))],
        "stations_outside_osm_land": [s["name_zh"] for s in ref if not main_isl.contains(Point(*pos[s["id"]][:2]))],
    }
    nl = nlsc_land(bounds)
    if nl is not None:
        nl_main = max((g for g in getattr(nl, "geoms", [nl])), key=lambda p: to_m(p).area)
        coast_cmp["nlsc_main_island_area_km2"] = round(to_m(nl_main).area / 1e6, 1)
        coast_cmp["nlsc_to_osm"] = sample_dist(to_m(nl_main).exterior, to_m(main_isl).exterior, 200)
        coast_cmp["osm_to_nlsc"] = sample_dist(to_m(main_isl).exterior, to_m(nl_main).exterior, 200)
        coast_cmp["ne_to_nlsc"] = sample_dist(to_m(ne_main).exterior, to_m(nl_main).exterior, 200)
    (OUT / "coast_compare.json").write_text(json.dumps(coast_cmp, ensure_ascii=False, indent=2))

    # 大小矩陣
    land_all = [p for p in polys if to_m(p).area >= 0.05e6]
    size = {"rail": {}, "coast": {}}
    for tol in (2, 5, 10, 20, 50, 100, 200):
        nv = nb = 0
        for line_id, g in lines.items():
            ms = measures[line_id]
            gm = to_m(g)
            for a, b in zip(ms, ms[1:]):
                seg = to_ll(substring(gm, a, b).simplify(tol, preserve_topology=False))
                c = coords_of(seg); nv += len(c); nb += len(encode_polyline(c))
        size["rail"][tol] = {"vertices": nv, "polyline5_bytes": nb}
    for tol in (10, 30, 50, 100, 300, 500, 1000):
        nv = nb = 0
        for p in land_all:
            if to_m(p).area < (0.05e6 if tol <= 100 else 2e6):
                continue
            sp = simplify_ll(p, tol)
            if sp.is_empty:
                continue
            c = coords_of(sp); nv += len(c); nb += len(encode_polyline(c))
        size["coast"][tol] = {"vertices": nv, "polyline5_bytes": nb}
    size["raw"] = {"rail_vertices": sum(len(g.coords) for g in lines.values()),
                   "coast_vertices": sum(len(p.exterior.coords) for p in land_all)}
    (OUT / "size_matrix.json").write_text(json.dumps(size, ensure_ascii=False, indent=2))

    # ---------- 樣本 pack ----------
    pack = OUT / "mappack-tw.sqlite"
    if pack.exists():
        pack.unlink()
    con = sqlite3.connect(pack)
    con.execute("PRAGMA user_version = 1")
    con.executescript("""
CREATE TABLE pack_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE land (
  id INTEGER PRIMARY KEY, lod INTEGER NOT NULL CHECK (lod IN (0,1)),
  area_km2 REAL NOT NULL, min_lon REAL NOT NULL, min_lat REAL NOT NULL, max_lon REAL NOT NULL, max_lat REAL NOT NULL,
  ring TEXT NOT NULL
);
CREATE TABLE rail_segment (
  line_id TEXT NOT NULL, from_seq INTEGER NOT NULL, to_seq INTEGER NOT NULL, lod INTEGER NOT NULL CHECK (lod IN (0,1)),
  length_m REAL NOT NULL, path TEXT NOT NULL,
  PRIMARY KEY (line_id, from_seq, lod), CHECK (to_seq = from_seq + 1)
);
CREATE TABLE station_anchor (
  station_id TEXT PRIMARY KEY, line_id TEXT NOT NULL, seq INTEGER NOT NULL,
  lon REAL NOT NULL, lat REAL NOT NULL, measure_m REAL NOT NULL,
  snap_offset_m REAL NOT NULL,
  UNIQUE (line_id, seq)
);
""")
    lod_n = {"lod0": 0, "lod1": 1}
    for lod, tol in COAST_TOL.items():
        for p in polys:
            a = to_m(p).area
            if a < COAST_MIN_AREA[lod]:
                continue
            sp = simplify_ll(p, tol)
            if sp.is_empty or sp.geom_type != "Polygon":
                continue
            c = coords_of(sp); mnx, mny, mxx, mxy = sp.bounds
            con.execute("INSERT INTO land(lod,area_km2,min_lon,min_lat,max_lon,max_lat,ring) VALUES (?,?,?,?,?,?,?)",
                        (lod_n[lod], round(a / 1e6, 3), round(mnx, 5), round(mny, 5), round(mxx, 5), round(mxy, 5), encode_polyline(c)))
    for line_id, g in lines.items():
        gm = to_m(g); ms = measures[line_id]
        st = [s for s in ref if s["line_id"] == line_id]
        for (sa, a), (sb, b) in zip(zip(st, ms), zip(st[1:], ms[1:])):
            raw_seg = substring(gm, a, b)
            for lod, tol in RAIL_TOL.items():
                c = coords_of(to_ll(raw_seg.simplify(tol, preserve_topology=False)))
                con.execute("INSERT INTO rail_segment VALUES (?,?,?,?,?,?)",
                            (line_id, sa["seq"], sb["seq"], lod_n[lod], round(raw_seg.length, 1), encode_polyline(c)))
        for s, m in zip(st, ms):
            sl, sla, _ = pos[s["id"]]
            p_on = to_ll(gm.interpolate(m)).coords[0]
            r = next(x for x in rows if x["station_id"] == s["id"])
            # pack 只放「TDX 點投影到 OSM 線上」的結果；TDX 原始座標與比對值留在 QA 檔（station_compare.csv）
            con.execute("INSERT INTO station_anchor VALUES (?,?,?,?,?,?,?)",
                        (s["id"], line_id, s["seq"], round(p_on[0], 6), round(p_on[1], 6), round(m, 1),
                         round(hav(sl, sla, *p_on), 1)))
    osm_man = json.loads((RAW / f"osm_manifest_{STAMP}.json").read_text())
    rcon = sqlite3.connect(REF)
    ref_version = rcon.execute("SELECT value FROM meta WHERE key='data_version'").fetchone()[0]
    rcon.close()
    meta = {
        "reference_data_version": ref_version,
        "network_id": "tw", "pack_version": "2026.10-osm", "format": "slw-mappack/1",
        "coord_encoding": "google-polyline precision=5 (lat,lon), WGS84",
        "bbox": "119.25,21.85,122.10,25.35",
        "rail_tolerance_m": json.dumps(RAIL_TOL), "coast_tolerance_m": json.dumps(COAST_TOL),
        "osm_timestamp": osm_man["files"]["osm_rail_relations"]["osm_base"],
        "osm_relations": json.dumps(osm_man["relations"]),
        "license": "ODbL-1.0", "license_url": "https://opendatacommons.org/licenses/odbl/1-0/",
        "attribution_short": "© OpenStreetMap",
        "attribution_zh": "地圖資料 © OpenStreetMap 貢獻者，依 ODbL 授權；站點座標：交通部 TDX 平臺（政府資料開放授權條款第 1 版）。",
        "attribution_en": "Map data © OpenStreetMap contributors (ODbL). Station positions: MOTC TDX (Open Government Data License v1.0).",
        "copyright_url": "https://www.openstreetmap.org/copyright",
        "built_at": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
    }
    con.executemany("INSERT INTO pack_meta VALUES (?,?)", meta.items())
    con.commit()
    # 建置閘門：每條線 N-1 段 × 2 LOD、每站一個 anchor、偏移與比對值在門檻內
    n_seg = con.execute("SELECT COUNT(*) FROM rail_segment").fetchone()[0]
    n_anchor = con.execute("SELECT COUNT(*) FROM station_anchor").fetchone()[0]
    max_off = con.execute("SELECT MAX(snap_offset_m) FROM station_anchor").fetchone()[0]
    if n_seg != 2 * (11 + 23) or n_anchor != 36:
        raise SystemExit(f"pack 筆數不對：segment={n_seg} anchor={n_anchor}")
    if max_off > 150:
        raise SystemExit(f"站點投影偏移 {max_off} m > 150 m")
    if any(r["tdx_osm_m"] == "" for r in rows):
        raise SystemExit("有站沒有對到 OSM 車站 node")
    con.execute("VACUUM"); con.close()

    # 預覽 geojson
    feats = [{"type": "Feature", "properties": {"line_id": k}, "geometry": mapping(v)} for k, v in lines.items()]
    feats += [{"type": "Feature", "properties": {"id": s["id"], "name": s["name_zh"]}, "geometry": mapping(Point(*pos[s["id"]][:2]))} for s in ref]
    (OUT / "preview.geojson").write_text(json.dumps({"type": "FeatureCollection", "features": feats}, ensure_ascii=False))

    raw_bytes = pack.stat().st_size
    gz = len(zlib.compress(pack.read_bytes(), 9))
    con = sqlite3.connect(pack)
    counts = {t: con.execute(f"SELECT COUNT(*), COALESCE(SUM(LENGTH({c})),0) FROM {t}").fetchone()
              for t, c in (("land", "ring"), ("rail_segment", "path"), ("station_anchor", "station_id"))}
    by_lod = con.execute("SELECT 'land', lod, COUNT(*), SUM(LENGTH(ring)) FROM land GROUP BY lod UNION ALL SELECT 'rail', lod, COUNT(*), SUM(LENGTH(path)) FROM rail_segment GROUP BY lod").fetchall()
    con.close()
    summary = {"pack_bytes": raw_bytes, "pack_zlib_bytes": gz, "tables": counts, "by_lod": by_lod}
    (OUT / "pack_summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2))
    flagged = [r for r in rows if r["flag_gt_200m"]]
    print("stations:", len(rows), "flagged >200m:", [(r["station_id"], r["name_zh"], r["tdx_osm_m"]) for r in flagged])
    print("line_compare:", json.dumps({k: {kk: v[kk] for kk in ("osm_length_km", "osm_to_tdx", "tdx_to_osm")} for k, v in line_cmp.items()}, ensure_ascii=False))
    print("coast:", json.dumps(coast_cmp, ensure_ascii=False))
    print("pack:", json.dumps(summary, ensure_ascii=False))


if __name__ == "__main__":
    main()
