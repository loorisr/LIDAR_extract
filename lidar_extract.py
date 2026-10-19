#!/usr/bin/env python3
"""Extract IGN LiDAR HD elevation models (MNT/MNS) around a point.

Writes georeferenced GeoTIFFs (EPSG:2154, NGF-IGN69 heights) ready for
shadow computation in QGIS / SAGA / GRASS / any other GIS or solar software.

Data source: IGN Géoplateforme WMS-R (https://data.geopf.fr/wms-r/wms), which
serves *raw Float32 elevation grids* (not shaded pictures) when requested with
FORMAT=image/tiff. Georeferencing is re-attached locally.

Notes:
  - No pip packages needed: stdlib + system GDAL (CLI and Python bindings).
  - numpy is deliberately NOT used (the GDAL bindings on this machine are
    compiled against numpy 1.x). All raster I/O goes through GDAL's C++ API
    and the stdlib ``array`` module.

Usage:
  python3 lidar_extract.py                              # Paris (48.9, 2.3)
  python3 lidar_extract.py --size 2000 --res 1
  python3 lidar_extract.py --lat 43.2965 --lon 5.3698   # Marseille
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import struct
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from array import array
from datetime import datetime, timezone
from pathlib import Path

import warnings

warnings.filterwarnings("ignore", category=FutureWarning, module="osgeo")
warnings.filterwarnings("ignore", message="Neither osr.UseExceptions")

from osgeo import gdal, osr  # noqa: E402

# Avoid .aux.xml sidecars; everything we need is written into the GeoTIFF or
# into metadata.json.
gdal.SetConfigOption("GDAL_PAM_ENABLED", "NO")


# Do NOT call gdal.UseExceptions()/osr.UseExceptions() here: on this system
# they import gdal_array, which is compiled against numpy 1.x and crashes with
# numpy 2.x. All GDAL calls below are checked explicitly instead.
def _gdal_err(what: str) -> RuntimeError:
    return RuntimeError(f"GDAL error during {what}: {gdal.GetLastErrorMsg()}")


def open_ds(path, update: bool = False):
    ds = gdal.Open(str(path), gdal.GA_Update if update else gdal.GA_ReadOnly)
    if ds is None:
        raise _gdal_err(f"open {path}")
    return ds

TOOL_NAME = "lidar_extract.py"
VERSION = "1.4"

# Default location (Paris) so the tool runs without private coordinates.
DEFAULT_LAT = 48.9
DEFAULT_LON = 2.3

WMS_URL = "https://data.geopf.fr/wms-r/wms"
WMS_VERSION = "1.3.0"
USER_AGENT = f"{TOOL_NAME}/{VERSION} (+https://data.geopf.fr)"

NODATA = -99999.0
ATTRIBUTION = "© IGN – LiDAR HD / RGE ALTI – Licence Ouverte Etalab 2.0"
VERTICAL_DATUM = "NGF-IGN69"

LAYERS = {
    "mnt": {
        "id": "IGNF_LIDAR-HD_MNT_ELEVATION.ELEVATIONGRIDCOVERAGE.LAMB93",
        "label": "LiDAR HD MNT (terrain / ground, native 1 m / 0.50 m)",
    },
    "mns": {
        "id": "IGNF_LIDAR-HD_MNS_ELEVATION.ELEVATIONGRIDCOVERAGE.LAMB93",
        "label": "LiDAR HD MNS (surface: ground + buildings/vegetation, native 1 m / 0.50 m)",
    },
    "rgealti": {
        "id": "ELEVATION.ELEVATIONGRIDCOVERAGE.HIGHRES",
        "label": "RGE ALTI 1 m (terrain, fallback)",
    },
}

# Metropolitan France (approx. envelope of the LAMB93 coverage).
FRANCE_LON = (-6.0, 10.0)
FRANCE_LAT = (41.0, 52.0)

# Colour ramp for the height pictures: (fraction of range, RGB).
# Low = green, high = white (works both for a 10 m urban range and mountains).
PALETTE_FRACTIONS = [
    (0.00, (49, 130, 65)),
    (0.20, (116, 179, 101)),
    (0.40, (200, 222, 130)),
    (0.55, (247, 239, 163)),
    (0.70, (238, 186, 107)),
    (0.85, (204, 130, 80)),
    (1.00, (255, 255, 255)),
]


class WmsError(RuntimeError):
    """Raised when the Géoplateforme returns an error or unusable data."""


# --------------------------------------------------------------------------- #
# Geometry helpers
# --------------------------------------------------------------------------- #

def _transform(from_epsg: int, to_epsg: int, x: float, y: float) -> tuple[float, float]:
    src = osr.SpatialReference()
    src.ImportFromEPSG(from_epsg)
    src.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    dst = osr.SpatialReference()
    dst.ImportFromEPSG(to_epsg)
    dst.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    ct = osr.CoordinateTransformation(src, dst)
    tx, ty, _ = ct.TransformPoint(x, y)
    return tx, ty


def wgs84_to_l93(lon: float, lat: float) -> tuple[float, float]:
    return _transform(4326, 2154, lon, lat)


def l93_to_wgs84(x: float, y: float) -> tuple[float, float]:
    return _transform(2154, 4326, x, y)


def build_bbox(center_x: float, center_y: float, size_m: float, res_m: float) -> tuple[float, float, float, float]:
    """Square bbox of side ``size_m``, snapped to the ``res_m`` lattice."""
    n = int(round(size_m / res_m))
    half = n * res_m / 2.0
    xmin = math.floor((center_x - half) / res_m) * res_m
    ymin = math.floor((center_y - half) / res_m) * res_m
    return xmin, ymin, xmin + n * res_m, ymin + n * res_m


# --------------------------------------------------------------------------- #
# WMS access
# --------------------------------------------------------------------------- #

def _parse_service_exception(data: bytes) -> str:
    try:
        root = ET.fromstring(data)
        for el in root.iter():
            if el.tag.endswith("ExceptionText") and el.text:
                return el.text.strip()
    except ET.ParseError:
        pass
    return data[:300].decode("utf-8", "replace").strip()


def fetch_layer(layer_id: str, bbox: tuple, width: int, height: int, timeout: float) -> tuple[bytes, str]:
    """Fetch a raw Float32 elevation GeoTIFF from the Géoplateforme WMS-R.

    Returns ``(tiff_bytes, request_url)``.
    """
    params = {
        "SERVICE": "WMS",
        "VERSION": WMS_VERSION,
        "REQUEST": "GetMap",
        "LAYERS": layer_id,
        "STYLES": "",
        "CRS": "EPSG:2154",
        "BBOX": "%.3f,%.3f,%.3f,%.3f" % bbox,
        "WIDTH": str(width),
        "HEIGHT": str(height),
        "FORMAT": "image/tiff",
    }
    url = WMS_URL + "?" + urllib.parse.urlencode(params)

    last_err: Exception | None = None
    for attempt in range(3):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = resp.read()
            break
        except urllib.error.HTTPError as exc:
            body = exc.read()
            if b"Exception" in body[:3000]:
                raise WmsError(_parse_service_exception(body)) from None
            last_err = exc
        except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
            last_err = exc
        if attempt < 2:
            time.sleep(2.0 * (attempt + 1))
    else:
        raise WmsError(f"network error after 3 attempts: {last_err}")

    if not (data[:4] == b"II*\x00" or data[:4] == b"MM\x00*"):
        if data.lstrip()[:1] == b"<":
            raise WmsError(_parse_service_exception(data))
        raise WmsError(f"unexpected response (not a TIFF), {len(data)} bytes")
    return data, url


# --------------------------------------------------------------------------- #
# Raster helpers (numpy-free)
# --------------------------------------------------------------------------- #

def write_geotiff(data: bytes, dst: Path, bbox: tuple) -> None:
    """Write the raw WMS grid with correct georeferencing (EPSG:2154, north-up).

    The geotransform is set explicitly on purpose: gdal.Translate's
    ``outputBounds`` maps to ``-a_ullr ulx uly lrx lry`` (not
    minX/minY/maxX/maxY), which silently produced a south-up raster when
    handed a standard bbox.
    """
    xmin, ymin, xmax, ymax = bbox
    mem = "/vsimem/lidar_extract_src.tif"
    gdal.FileFromMemBuffer(mem, data)
    try:
        ds = gdal.Translate(
            str(dst),
            mem,
            options=gdal.TranslateOptions(
                format="GTiff",
                creationOptions=["COMPRESS=DEFLATE", "TILED=YES", "PREDICTOR=2", "BIGTIFF=IF_SAFER"],
            ),
        )
        if ds is None:
            raise _gdal_err("gdal.Translate")
        width, height = ds.RasterXSize, ds.RasterYSize
        ds = None
    finally:
        gdal.Unlink(mem)
    ds = open_ds(dst, update=True)
    ds.SetGeoTransform(
        (xmin, (xmax - xmin) / width, 0.0, ymax, 0.0, -(ymax - ymin) / height)
    )
    srs = osr.SpatialReference()
    srs.ImportFromEPSG(2154)
    ds.SetProjection(srs.ExportToWkt())
    band = ds.GetRasterBand(1)
    band.SetNoDataValue(NODATA)
    band.FlushCache()
    ds = None


def raster_stats(path: Path) -> dict:
    ds = open_ds(path)
    band = ds.GetRasterBand(1)
    band.ComputeStatistics(False)
    values = band.GetStatistics(False, False)
    if values is None:
        raise _gdal_err(f"statistics of {path}")
    minv, maxv, meanv, stddev = values
    valid_pct = band.GetMetadataItem("STATISTICS_VALID_PERCENT")
    ds = None
    return {
        "min_m": round(minv, 2),
        "max_m": round(maxv, 2),
        "mean_m": round(meanv, 2),
        "stddev_m": round(stddev, 2),
        "valid_percent": float(valid_pct) if valid_pct is not None else None,
    }


def stats_ok(stats: dict) -> bool:
    """Usable data: some valid pixels and heights that are not the nodata sentinel."""
    return bool(stats["valid_percent"]) and stats["max_m"] > -1000.0


def read_float_array(path: Path) -> tuple[array, int, int]:
    ds = open_ds(path)
    w, h = ds.RasterXSize, ds.RasterYSize
    raw = ds.GetRasterBand(1).ReadRaster(0, 0, w, h, buf_type=gdal.GDT_Float32)
    ds = None
    if raw is None:
        raise _gdal_err(f"ReadRaster {path}")
    arr = array("f")
    arr.frombytes(raw)
    return arr, w, h


def center_pixel(path: Path, cx: int, cy: int) -> float:
    ds = open_ds(path)
    raw = ds.GetRasterBand(1).ReadRaster(cx, cy, 1, 1, buf_type=gdal.GDT_Float32)
    ds = None
    if raw is None:
        raise _gdal_err(f"ReadRaster {path}")
    return struct.unpack("f", raw)[0]


def write_objects_height(mnt_path: Path, mns_path: Path, dst: Path) -> dict:
    """objects_height = MNS − MNT (buildings/vegetation above ground)."""
    mnt, w, h = read_float_array(mnt_path)
    mns, w2, h2 = read_float_array(mns_path)
    if (w, h) != (w2, h2):
        raise RuntimeError("MNT and MNS grids differ in size")
    diff = array("f", (m - t for m, t in zip(mns, mnt)))

    ref = open_ds(mnt_path)
    gt, wkt = ref.GetGeoTransform(), ref.GetProjection()
    ref = None

    drv = gdal.GetDriverByName("GTiff")
    ds = drv.Create(
        str(dst), w, h, 1, gdal.GDT_Float32,
        options=["COMPRESS=DEFLATE", "TILED=YES", "PREDICTOR=2", "BIGTIFF=IF_SAFER"],
    )
    if ds is None:
        raise _gdal_err(f"create {dst}")
    ds.SetGeoTransform(gt)
    ds.SetProjection(wkt)
    band = ds.GetRasterBand(1)
    band.WriteRaster(0, 0, w, h, diff.tobytes(), buf_type=gdal.GDT_Float32)
    band.SetNoDataValue(NODATA)
    band.ComputeStatistics(False)
    values = band.GetStatistics(False, False)
    if values is None:
        raise _gdal_err(f"statistics of {dst}")
    minv, maxv, meanv, stddev = values
    valid_pct = band.GetMetadataItem("STATISTICS_VALID_PERCENT")
    stats = {
        "min_m": round(minv, 2),
        "max_m": round(maxv, 2),
        "mean_m": round(meanv, 2),
        "stddev_m": round(stddev, 2),
        "valid_percent": float(valid_pct) if valid_pct is not None else None,
    }
    ds = None
    return stats


def _png_dims(src: Path, target: int = 600, max_scale: int = 8) -> tuple[int, int]:
    """Native size, or integer-upscaled (bilinear preview) when very small."""
    ds = open_ds(src)
    w, h = ds.RasterXSize, ds.RasterYSize
    ds = None
    if max(w, h) >= target:
        return w, h
    scale = min(max_scale, max(1, target // max(w, h)))
    return w * scale, h * scale


def _draw_cross(path: Path, x: float, y: float, rgb: bool) -> None:
    """Burn a small crosshair centred on (x, y) [EPSG:2154] into a Byte raster."""
    ds = open_ds(path, update=True)
    gt = ds.GetGeoTransform()
    w, h = ds.RasterXSize, ds.RasterYSize
    col = int((x - gt[0]) / gt[1])          # pixel containing the point
    row = int((y - gt[3]) / gt[5])
    if not (0 <= col < w and 0 <= row < h):
        ds = None
        return
    arm = max(8, min(24, w // 40))
    gap = max(2, arm // 5)
    thick = 2 if w >= 800 else 1
    value = [255, 0, 255] if rgb else [0]   # magenta on pictures, black on hillshade
    for i in range(1, ds.RasterCount + 1):
        band = ds.GetRasterBand(i)
        px = bytearray(band.ReadRaster(0, 0, w, h))
        v = value[(i - 1) % len(value)]
        for t in range(thick):
            rr = min(max(row + t - thick // 2, 0), h - 1)
            cc = min(max(col + t - thick // 2, 0), w - 1)
            for k in range(gap, arm + gap + 1):
                for r2, c2 in ((rr, cc - k), (rr, cc + k), (rr - k, cc), (rr + k, cc)):
                    if 0 <= r2 < h and 0 <= c2 < w:
                        px[r2 * w + c2] = v
        band.WriteRaster(0, 0, w, h, bytes(px), buf_type=gdal.GDT_Byte)
    ds = None


def _gdaldem_to_png(base_cmd: list[str], src: Path, dst_png: Path,
                    target: tuple[float, float] | None = None, rgb: bool = False) -> None:
    """Run gdaldem to a temp GTiff, optionally mark the target, export PNG.

    The ``gdaldem`` CLI is used because the Python ``gdal.DEMProcessing``
    wrapper writes empty rasters in this environment (numpy 1.x/2.x ABI
    mismatch in the GDAL bindings), while the CLI is reliable.
    """
    env = dict(os.environ, GDAL_PAM_ENABLED="NO")
    tmp = dst_png.with_suffix(".tmp.tif")
    up = dst_png.with_suffix(".up.tmp.tif")
    try:
        proc = subprocess.run(base_cmd + [str(tmp)], capture_output=True, text=True, env=env)
        if proc.returncode != 0:
            raise RuntimeError(f"gdaldem failed: {proc.stderr.strip() or proc.stdout.strip()}")
        w, h = _png_dims(src)
        scaled = gdal.Translate(str(up), str(tmp), options=gdal.TranslateOptions(
            format="GTiff", width=w, height=h, resampleAlg="bilinear"))
        if scaled is None:
            raise _gdal_err("gdal.Translate (preview upscale)")
        scaled = None
        if target is not None:
            _draw_cross(up, target[0], target[1], rgb=rgb)
        out = gdal.Translate(str(dst_png), str(up), options=gdal.TranslateOptions(format="PNG"))
        if out is None:
            raise _gdal_err("gdal.Translate (PNG)")
        out = None
    finally:
        tmp.unlink(missing_ok=True)
        up.unlink(missing_ok=True)


def make_hillshade(src: Path, dst_png: Path, azimuth: float = 315.0, altitude: float = 45.0,
                   target: tuple[float, float] | None = None) -> None:
    """Preview hillshade PNG (sanity check; not a shadow map)."""
    base = ["gdaldem", "hillshade", str(src),
            "-az", f"{azimuth:g}", "-alt", f"{altitude:g}", "-compute_edges"]
    _gdaldem_to_png(base, src, dst_png, target=target, rgb=False)


def write_palette(path: Path, vmin: float, vmax: float) -> None:
    """gdaldem colour file: nodata transparent + interpolated height ramp."""
    lines = [f"{NODATA:g} 0 0 0 0"]
    for frac, (r, g, b) in PALETTE_FRACTIONS:
        value = vmin + frac * (vmax - vmin)
        lines.append(f"{value:.2f} {r} {g} {b} 255")
    path.write_text("\n".join(lines) + "\n", encoding="ascii")


def make_color_relief(src: Path, palette: Path, dst_png: Path,
                      target: tuple[float, float] | None = None) -> None:
    """Colour height picture (nodata transparent)."""
    base = ["gdaldem", "color-relief", str(src), str(palette), "-alpha"]
    _gdaldem_to_png(base, src, dst_png, target=target, rgb=True)


def make_shaded_relief(src: Path, palette: Path, dst_png: Path,
                       target: tuple[float, float] | None = None) -> None:
    """Colour height picture combined with hillshade (classic shaded relief)."""
    env = dict(os.environ, GDAL_PAM_ENABLED="NO")
    hs = dst_png.with_suffix(".hs.tmp.tif")
    col = dst_png.with_suffix(".col.tmp.tif")
    up = dst_png.with_suffix(".up.tmp.tif")
    mem = "/vsimem/lidar_extract_relief.tif"
    try:
        for cmd, out in (
            (["gdaldem", "hillshade", str(src), "-az", "315", "-alt", "45", "-compute_edges"], hs),
            (["gdaldem", "color-relief", str(src), str(palette)], col),
        ):
            proc = subprocess.run(cmd + [str(out)], capture_output=True, text=True, env=env)
            if proc.returncode != 0:
                raise RuntimeError(f"gdaldem failed: {proc.stderr.strip() or proc.stdout.strip()}")

        cds, hds = open_ds(col), open_ds(hs)
        w, h = cds.RasterXSize, cds.RasterYSize
        shade = bytes(hds.GetRasterBand(1).ReadRaster(0, 0, w, h))
        ref = open_ds(src)  # same pixel grid as the combined relief
        ref_gt, ref_wkt = ref.GetGeoTransform(), ref.GetProjection()
        ref = None
        drv = gdal.GetDriverByName("GTiff")
        out = drv.Create(mem, w, h, 3, gdal.GDT_Byte, options=["COMPRESS=DEFLATE"])
        if out is None:
            raise _gdal_err("create shaded relief")
        out.SetGeoTransform(ref_gt)
        out.SetProjection(ref_wkt)
        for i in (1, 2, 3):
            band_bytes = bytes(cds.GetRasterBand(i).ReadRaster(0, 0, w, h))
            shaded = bytes((c * s + 127) // 255 for c, s in zip(band_bytes, shade))
            out.GetRasterBand(i).WriteRaster(0, 0, w, h, shaded, buf_type=gdal.GDT_Byte)
        cds = hds = None
        out = None  # flush before reading back
        ow, oh = _png_dims(src)
        scaled = gdal.Translate(str(up), mem, options=gdal.TranslateOptions(
            format="GTiff", width=ow, height=oh, resampleAlg="bilinear"))
        if scaled is None:
            raise _gdal_err("gdal.Translate (relief upscale)")
        scaled = None
        if target is not None:
            _draw_cross(up, target[0], target[1], rgb=True)
        res = gdal.Translate(str(dst_png), str(up), options=gdal.TranslateOptions(format="PNG"))
        if res is None:
            raise _gdal_err("gdal.Translate (shaded relief PNG)")
        res = None
    finally:
        hs.unlink(missing_ok=True)
        col.unlink(missing_ok=True)
        up.unlink(missing_ok=True)
        gdal.Unlink(mem)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #

def extract(args: argparse.Namespace) -> int:
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    center_x, center_y = wgs84_to_l93(args.lon, args.lat)
    bbox = build_bbox(center_x, center_y, args.size, args.res)
    target = (center_x, center_y) if args.cross else None
    xmin, ymin, xmax, ymax = bbox
    width = int(round((xmax - xmin) / args.res))
    height = int(round((ymax - ymin) / args.res))

    res_tag = "%g" % args.res
    res_tag = res_tag.replace(".", "p")
    stem = {k: f"{k}_{res_tag}m" for k in ("mnt", "mns", "rgealti")}

    (lon_min, lat_min) = l93_to_wgs84(xmin, ymin)
    (lon_max, lat_max) = l93_to_wgs84(xmax, ymax)

    print(f"{TOOL_NAME} {VERSION}")
    print(f"Point      : {args.lat:.6f}, {args.lon:.6f} (WGS84)")
    print(f"EPSG:2154  : x={center_x:.1f}  y={center_y:.1f}")
    print(f"Area       : {xmax - xmin:.0f} x {ymax - ymin:.0f} m  @ {args.res:g} m "
          f"({width} x {height} px)")
    print(f"Output     : {out_dir.resolve()}")

    if args.res < 0.5:
        print("NOTE: --res < 0.50 m is below the native LiDAR HD grid; the service interpolates.")
    elif args.res == 0.5:
        print("NOTE: 0.50 m is the native LiDAR HD grid (product 0M50) — real detail, not interpolation.")

    if not (FRANCE_LON[0] <= args.lon <= FRANCE_LON[1] and FRANCE_LAT[0] <= args.lat <= FRANCE_LAT[1]):
        print("WARNING: point outside metropolitan France; LAMB93 LiDAR HD coverage unlikely.")
    if width * height > 16_000_000:
        print("WARNING: large request (>16 Mpx); the service might be slow.")

    layer_keys = [k.strip() for k in args.layers.split(",") if k.strip()]
    unknown = [k for k in layer_keys if k not in LAYERS]
    if unknown:
        raise SystemExit(f"unknown layer(s): {', '.join(unknown)} — choose from {', '.join(LAYERS)}")

    results: dict[str, dict] = {}
    requests_log: list[dict] = []
    failures: list[str] = []

    for key in layer_keys:
        candidates = [key]
        if key == "mnt" and not args.no_fallback:
            candidates.append("rgealti")

        dst = out_dir / f"{stem[key]}.tif"
        attempt_errors: list[str] = []
        for idx, cand in enumerate(candidates):
            info = LAYERS[cand]
            print(f"[{key}] fetching {info['label']} ...", flush=True)
            t0 = time.time()
            try:
                data, url = fetch_layer(info["id"], bbox, width, height, args.timeout)
                write_geotiff(data, dst, bbox)
                stats = raster_stats(dst)
                if not stats_ok(stats):
                    raise WmsError("service returned no usable elevation values here")
            except WmsError as exc:
                attempt_errors.append(f"{info['label']}: {exc}")
                if idx + 1 < len(candidates):
                    print(f"[{key}] {info['label']} unavailable ({exc}); falling back to RGE ALTI")
                    continue
                msg = " | ".join(attempt_errors)
                failures.append(f"{key}: {msg}")
                print(f"[{key}] FAILED: {msg}")
                dst.unlink(missing_ok=True)
                break
            dt = time.time() - t0
            print(f"[{key}] ok in {dt:.1f} s — {stats['min_m']:.1f} … {stats['max_m']:.1f} m "
                  f"(mean {stats['mean_m']:.1f} m)")
            results[key] = {
                "source_layer_id": info["id"],
                "source_label": info["label"],
                "fallback_used": cand != key,
                "file": dst.name,
                "stats": stats,
            }
            requests_log.append({"layer_key": key, "source_layer_id": info["id"], "url": url})
            break

    # objects = buildings/vegetation above ground
    objects_stats = None
    if "mnt" in results and "mns" in results:
        obj_path = out_dir / f"objects_height_{res_tag}m.tif"
        print("[objects] computing MNS − MNT ...", flush=True)
        objects_stats = write_objects_height(out_dir / f"{stem['mnt']}.tif",
                                             out_dir / f"{stem['mns']}.tif",
                                             obj_path)
        print(f"[objects] ok — {objects_stats['min_m']:.1f} … {objects_stats['max_m']:.1f} m "
              f"(mean {objects_stats['mean_m']:.2f} m)")

    # previews
    for key in ("mnt", "mns"):
        if key in results:
            tif = out_dir / f"{stem[key]}.tif"
            png = out_dir / f"hillshade_{key}.png"
            print(f"[{key}] hillshade preview -> {png.name} ...", flush=True)
            make_hillshade(tif, png, target=target)

    # colour height pictures (per-layer height range for full contrast)
    color_info = None
    if not args.no_color and any(k in results for k in ("mnt", "mns")):
        color_info = {}
        for key in ("mnt", "mns"):
            if key not in results:
                continue
            vmin = math.floor(results[key]["stats"]["min_m"])
            vmax = math.ceil(results[key]["stats"]["max_m"])
            if vmax <= vmin:
                vmax = vmin + 1
            palette = out_dir / "palette.tmp.txt"
            write_palette(palette, vmin, vmax)
            try:
                tif = out_dir / f"{stem[key]}.tif"
                print(f"[{key}] colour height picture ({vmin}–{vmax} m) -> color_{key}.png ...", flush=True)
                make_color_relief(tif, palette, out_dir / f"color_{key}.png", target=target)
                print(f"[{key}] shaded colour relief -> relief_{key}.png ...", flush=True)
                make_shaded_relief(tif, palette, out_dir / f"relief_{key}.png", target=target)
            finally:
                palette.unlink(missing_ok=True)
            color_info[key] = {
                "range_m": [vmin, vmax],
                "palette": [
                    {"fraction": f, "rgb": "#%02x%02x%02x" % tuple(rgb)}
                    for f, rgb in PALETTE_FRACTIONS
                ],
                "note": "fraction of range_m; nodata transparent",
            }

    # sweep sidecars left by GDAL CLI runs
    for pattern in ("*.aux.xml", "*.tmp.tif", "*.tmp.txt"):
        for stray in out_dir.glob(pattern):
            stray.unlink()

    # center heights
    cx, cy = width // 2, height // 2
    center_mnt = center_pixel(out_dir / f"{stem['mnt']}.tif", cx, cy) if "mnt" in results else None
    center_mns = center_pixel(out_dir / f"{stem['mns']}.tif", cx, cy) if "mns" in results else None
    if center_mnt is not None:
        print(f"Center terrain height (MNT): {center_mnt:.2f} m {VERTICAL_DATUM}")
    if center_mns is not None:
        print(f"Center surface height (MNS): {center_mns:.2f} m {VERTICAL_DATUM}")

    # GIS marker for the requested point + cross on all preview pictures
    marker = {
        "type": "FeatureCollection",
        "features": [{
            "type": "Feature",
            "properties": {
                "name": "target",
                "lat": args.lat,
                "lon": args.lon,
                "x_epsg2154": round(center_x, 3),
                "y_epsg2154": round(center_y, 3),
                "elevation_mnt_m": center_mnt,
                "elevation_mns_m": center_mns,
            },
            "geometry": {"type": "Point", "coordinates": [args.lon, args.lat]},
        }],
    }
    (out_dir / "target_point.geojson").write_text(
        json.dumps(marker, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    if args.cross:
        print("Target cross drawn on the preview pictures; GIS marker: target_point.geojson")
    else:
        print("GIS marker: target_point.geojson (use --cross to draw a crosshair on the pictures)")

    # metadata
    files = sorted(p for p in out_dir.iterdir() if p.is_file())
    meta = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "tool": f"{TOOL_NAME} {VERSION}",
        "request": {
            "lat": args.lat, "lon": args.lon,
            "size_m": args.size, "resolution_m": args.res,
        },
        "area": {
            "bbox_epsg2154": [xmin, ymin, xmax, ymax],
            "bbox_wgs84": [round(lon_min, 7), round(lat_min, 7), round(lon_max, 7), round(lat_max, 7)],
            "width_px": width, "height_px": height, "pixel_size_m": args.res,
        },
        "crs": {"horizontal": "EPSG:2154 (RGF93 v2b / Lambert-93)", "vertical": VERTICAL_DATUM},
        "center_elevation_m": {"mnt": center_mnt, "mns": center_mns},
        "layers": results,
        "objects_height": objects_stats,
        "colors": color_info,
        "files": [
            {"name": p.name, "bytes": p.stat().st_size, "sha256": sha256_file(p)}
            for p in files
        ],
        "service": WMS_URL,
        "requests": requests_log,
        "attribution": ATTRIBUTION,
    }
    meta_path = out_dir / "metadata.json"
    meta_path.write_text(json.dumps(meta, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    print()
    print("Files written:")
    for p in files:
        print(f"  {p.name:<28} {p.stat().st_size / 1e6:7.2f} MB")
    print(f"  {'metadata.json':<28} (this run: area, CRS, stats, sha256, attribution)")

    if failures:
        print("\nFailures:")
        for f in failures:
            print(f"  - {f}")
        return 2 if not results else 1
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog=TOOL_NAME,
        description="Extract IGN LiDAR HD elevation models (MNT/MNS) around a point, "
                    "as georeferenced GeoTIFFs for shadow computation.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--lat", type=float, default=DEFAULT_LAT, help="latitude, WGS84")
    parser.add_argument("--lon", type=float, default=DEFAULT_LON, help="longitude, WGS84")
    parser.add_argument("--size", type=float, default=1000.0, help="side of the square area, metres")
    parser.add_argument("--res", type=float, default=1.0,
                        help="pixel size, metres; LiDAR HD is served natively down to 0.50 m "
                             "(finer values are interpolated by the service)")
    parser.add_argument("--layers", default="mnt,mns", help="comma-separated: mnt, mns, rgealti")
    parser.add_argument("--out", default="output", help="output directory")
    parser.add_argument("--no-color", action="store_true",
                        help="skip the colour height pictures (color_*.png / relief_*.png)")
    parser.add_argument("--cross", action="store_true",
                        help="draw a crosshair at the target point on the preview pictures")
    parser.add_argument("--no-fallback", action="store_true",
                        help="do not fall back from LiDAR HD MNT to RGE ALTI")
    parser.add_argument("--timeout", type=float, default=120.0, help="HTTP timeout, seconds")
    parser.add_argument("--list-layers", action="store_true", help="list available sources and exit")

    args = parser.parse_args(argv)

    if args.list_layers:
        for key, info in LAYERS.items():
            print(f"{key:<8} {info['id']}")
            print(f"{'':<8} {info['label']}")
        return 0

    if args.size <= 0 or args.res <= 0:
        parser.error("--size and --res must be positive")
    if abs(args.size / args.res - round(args.size / args.res)) > 1e-6:
        parser.error(f"--size ({args.size:g}) must be a multiple of --res ({args.res:g})")
    if not (-90 <= args.lat <= 90 and -180 <= args.lon <= 180):
        parser.error("--lat/--lon out of range")

    return extract(args)


if __name__ == "__main__":
    sys.exit(main())
