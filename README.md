# lidar_extract

Single-file CLI tool that extracts **IGN LiDAR HD** elevation models around a
point and writes georeferenced GeoTIFFs ready for **shadow computation** in
QGIS / SAGA / GRASS / other GIS or solar software.

For a coordinate (WGS84) it produces, in Lambert-93 (EPSG:2154):

| file | content | use for |
|---|---|---|
| `mnt_1m.tif` | terrain (ground, buildings/trees removed) | terrain-only shading |
| `mns_1m.tif` | surface (ground + buildings + vegetation) | **realistic cast shadows** |
| `objects_height_1m.tif` | MNS − MNT (object height above ground) | context / masking |
| `color_mnt.png`, `color_mns.png` | colour height pictures (hypsometric tint) | visualisation |
| `relief_mnt.png`, `relief_mns.png` | colour height + hillshade (shaded relief) | visualisation |
| `hillshade_mnt.png`, `hillshade_mns.png` | grey hillshade previews | sanity check |
| `target_point.geojson` | the requested point as a GIS marker (WGS84; properties carry EPSG:2154 coordinates and MNT/MNS heights) | drop on the map in QGIS/other GIS |
| `metadata.json` | bbox (2154 + WGS84), CRS, stats, SHA-256, exact request URLs, attribution | traceability |

The preview PNGs can carry a small crosshair at the requested point with
`--cross` (magenta on the colour pictures, black on the hillshades); it is off
by default so the pictures stay clean, and the DEM GeoTIFFs are never modified
(elevation values stay intact for shadow computation).

## Requirements

- Python ≥ 3.10 (tested with 3.12) — standard library only, no pip packages
- System GDAL ≥ 3.4: the `gdaldem` CLI and the Python `osgeo` bindings
- Internet access to `https://data.geopf.fr` (IGN Géoplateforme)

Notes: `numpy` is deliberately not used — the GDAL bindings on this machine are
compiled against numpy 1.x, so all raster I/O goes through GDAL's C++ API and
the stdlib `array` module. `gdaldem` (CLI) is used for the hillshade previews
because the Python `gdal.DEMProcessing` wrapper writes empty rasters here.

## Quick start

```bash
# 1 km x 1 km around Paris (48.9, 2.3), 1 m pixels, MNT + MNS + objects + previews
python3 lidar_extract.py

# 100 m x 100 m at the native 0.50 m resolution (real detail, not interpolation)
python3 lidar_extract.py --size 100 --res 0.5

# other sizes / locations
python3 lidar_extract.py --size 2000 --out dem_paris
python3 lidar_extract.py --lat 48.8584 --lon 2.2945 --size 500    # Eiffel Tower
```

## Options

| option | default | meaning |
|---|---|---|
| `--lat`, `--lon` | `48.9`, `2.3` | point in WGS84 (defaults to Paris) |
| `--size` | `1000` | side of the square area, metres |
| `--res` | `1` | pixel size, metres — the LiDAR HD product is served natively down to **0.50 m**; finer values are interpolated by the service |
| `--layers` | `mnt,mns` | subset: `mnt`, `mns`, `rgealti` |
| `--out` | `output` | output directory |
| `--no-color` | off | skip the colour height pictures |
| `--cross` | off | draw the target crosshair on the preview pictures |
| `--no-fallback` | off | disable MNT → RGE ALTI fallback |
| `--timeout` | `120` | HTTP timeout, seconds |
| `--list-layers` | — | show available sources |

Exit codes: `0` = all requested layers written, `1` = partial (some layers
failed), `2` = nothing produced.

## How it works

1. The point is projected to EPSG:2154 and a square bbox is snapped to the
   requested resolution grid (down to the native 0.50 m).
2. The IGN Géoplateforme WMS-R service (`data.geopf.fr/wms-r/wms`) is queried
   with `FORMAT=image/tiff`, which returns **raw Float32 elevation grids**
   (not a rendered picture) for the layers:
   - `IGNF_LIDAR-HD_MNT_ELEVATION.ELEVATIONGRIDCOVERAGE.LAMB93`
   - `IGNF_LIDAR-HD_MNS_ELEVATION.ELEVATIONGRIDCOVERAGE.LAMB93`
   - `ELEVATION.ELEVATIONGRIDCOVERAGE.HIGHRES` (RGE ALTI 1 m, fallback)
3. Georeferencing (bbox + EPSG:2154), DEFLATE compression, tiling and the
   nodata flag are attached locally with GDAL.
4. `objects_height` = MNS − MNT, statistics are computed, previews rendered
   and `metadata.json` written.

If LiDAR HD has no coverage at the point (or the service fails), the tool
automatically retries the terrain layer with RGE ALTI 1 m; unusable results
are detected (valid-pixel statistics) and deleted. LiDAR HD LAMB93 layers
cover metropolitan France only.

## Feeding the DEM to a shadow tool

The GeoTIFFs are standard compressed, tiled Float32 rasters:

- CRS **EPSG:2154** (RGF93 / Lambert-93), **1 m** or **0.50 m** pixels
  (0.50 m is the finest native LiDAR HD grid)
- heights in metres **NGF-IGN69** (above sea level), nodata = `-99999`
- all rasters of a run share the exact same grid (stack them directly)

Choose the **MNS** file (`mns_1m.tif`, or `mns_0p5m.tif` at 0.50 m) for
realistic shadows (buildings and trees cast shadows); use the MNT only if you
want bare-terrain shading.

Typical usage:

- **QGIS**: drag the `.tif` into the canvas → *Raster → Analysis → Hillshade*,
  or a shadow/solar-radiation plugin.
- **GRASS GIS**: `r.sun elevation=mns_1m.tif ...` (or the 0.50 m file)
- **SAGA GIS**: *Potential Incoming Solar Radiation* with the MNS as elevation.
- Heights are sea-level (NGF-IGN69), so any tool computing sun position from
  altitude/slope works without offset correction.

## Colour height pictures

`color_*.png` / `relief_*.png` colourise the elevations with a green → yellow →
orange → white ramp (low → high), each scaled to its own min–max range for full
contrast; `relief_*` additionally multiplies the colours with the hillshade
(classic shaded relief). Nodata is transparent. Small areas are upscaled
bilinearly for viewing (e.g. a 100 m square becomes a 600×600 px picture);
the GeoTIFFs keep the requested grid (as fine as the native 0.50 m). The exact
ramp and per-file height ranges are recorded in `metadata.json` (`colors` section).

## Verified example

Run with the default location (**48.9, 2.3**, Paris) for 1 km² @ 1 m:

- MNT 13.6 – 37.8 m, MNS 21.7 – 100.3 m (objects up to +72.6 m)
- center terrain 36.47 m, center surface 43.09 m (NGF-IGN69)
- run time ≈ 2 s, ~15 MB output

The same area at the native 0.50 m grid (`--size 100 --res 0.5`) resolves
building edges that the 1 m product smooths out.

## Data source & license

Elevation data: **IGN LiDAR HD (MNT/MNS, 1 m and 0.50 m)** and **RGE ALTI 1 m**,
served by the IGN Géoplateforme (`https://data.geopf.fr`), Licence Ouverte
**Etalab 2.0**.

> © IGN – LiDAR HD / RGE ALTI – Licence Ouverte Etalab 2.0

Keep this attribution with any redistribution or published map.
