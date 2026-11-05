# Kentucky STAC

[![CI](https://github.com/ianhorn/kentucky-stac/actions/workflows/ci.yml/badge.svg)](https://github.com/ianhorn/kentucky-stac/actions/workflows/ci.yml)
[![QGIS plugin](https://img.shields.io/badge/QGIS-plugin-589632)](https://plugins.qgis.org/plugins/kentucky_stac/)
[![License: GPL v2+](https://img.shields.io/badge/license-GPL--2.0--or--later-blue)](LICENSE)

A QGIS plugin for searching, streaming and downloading [Kentucky From Above](https://kyfromabove.ky.gov/)
imagery, elevation and LiDAR through its STAC API. Draw an area of interest, pick collections, and add
exactly the tiles you need to the map, or download them, without browsing catalogs by hand.

It is the QGIS successor to the ArcGIS Pro add-ins `kyfromabove-stac-addin` (imagery and elevation)
and `kylidar-addin` (LiDAR), merged into one dock panel with a shared area-of-interest workflow.

> **Note:** searching other STAC APIs (beyond Kentucky From Above) is still experimental.

## What it does

- **Search** Kentucky From Above imagery, DEM and LiDAR collections within an area of interest, with
  date, cloud-cover, sort and max-tile filters.
- **Add to the map** by streaming Cloud Optimized GeoTIFFs and COPC point clouds, with no download needed.
- **Download** tiles, optionally clipped to your area of interest.
- **Combine** results into GDAL VRT mosaics, MosaicJSON, titiler server mosaics, or Virtual Point Clouds.
- **LiDAR point filter**: keep chosen classifications and first, last or all returns.
- **Band combinations and indices** (NDVI or your own formula) from a right-click on a raster result.
- **Export as script**: a notebook, Python or shell script that repeats the download outside QGIS.
- **Other STAC APIs** (experimental): search any public STAC API from STAC Index or by URL, including
  Microsoft Planetary Computer (assets signed automatically) and `s3://` assets.

The [full documentation](https://ianhorn.github.io/kentucky-stac/) (also in [`docs/index.html`](docs/index.html))
covers every tool, filter and known issue in detail.

## Requirements

- **QGIS 3.34 or later**, including QGIS 4 (Qt6). The 3.44 LTR on OSGeo4W is the build it is developed on.
- **PDAL on your `PATH`**, for clipping point clouds, filtering by class or return, and building Virtual
  Point Clouds. QGIS from the OSGeo4W installer includes it. Everything else uses only QGIS's bundled
  Python, GDAL and network stack, so there is nothing to `pip install`.

## Install

1. In QGIS open **Plugins → Manage and Install Plugins** and search for **Kentucky STAC** under **All**.
2. Click **Install Plugin**.
3. Open it from **Plugins → Kentucky STAC** (or the toolbar icon).

It is also listed at <https://plugins.qgis.org/plugins/kentucky_stac/>, where each version's zip can be
used with **Install from ZIP**.

## Quick start

1. Set an area of interest with the tools at the top of the dock: draw a point, line or polygon, select
   existing features, or use the current map extent.
2. On the **Imagery / DEM** or **LiDAR Pointcloud** tab, tick one or more collections.
3. Click **Search area of interest**.
4. Select tiles in the results list, then add them to the map, download them, build a mosaic, or export a script.

## Getting help

- **Documentation:** <https://ianhorn.github.io/kentucky-stac/>
- **Bugs and requests:** [open an issue](https://github.com/ianhorn/kentucky-stac/issues). The **Feedback**
  button in the dock opens a pre-filled one with your QGIS, plugin and OS versions attached.
- **Tiles drawn in the wrong place** is a known coordinate-system issue with a workaround; see
  "Known issues" in the documentation.

## Development

The plugin is the `kentucky_stac/` folder. It is deliberately stdlib-only: runtime dependencies come from
QGIS's own Python.

**Live testing.** Symlink (or junction, on Windows) `kentucky_stac/` into a QGIS profile's
`python/plugins` directory, enable it, and use the
[Plugin Reloader](https://plugins.qgis.org/plugins/plugin_reloader/) plugin to pick up code changes. Don't
use *Uninstall* or *Install from ZIP* on a linked folder; they would delete your working copy.

**Checks.** CI runs the same commands you can run locally:

```sh
pip install ruff pytest
ruff check .
python -m pytest
```

Tests that import `qgis` (for example the PDAL pipeline tests) are skipped when QGIS isn't installed.
CI runs the full suite again inside the `qgis/qgis` LTR and stable containers, so those tests do run on
every push and pull request. To run them locally, use QGIS's own Python.

**Error handling.** Catch the specific exceptions a call can raise. A deliberate catch-all, such as
a worker thread or a UI action that must not raise into Qt, carries a
`# noqa: BLE001 - <why>` comment, and worker-thread ones also call `diagnostics.log_exception` so the
traceback reaches QGIS's Log Messages panel. Ruff enforces this.

## Releasing

1. Bump `version` in **both** `kentucky_stac/metadata.txt` and `pyproject.toml`, and add the new entry at
   the top of `changelog` in `metadata.txt`. A test fails if these drift apart.
2. Run through [`QA_CHECKLIST.md`](QA_CHECKLIST.md) in QGIS (CI can't click through the live catalog), merge to `main` once CI is green, then tag and push: `git tag vX.Y.Z && git push origin vX.Y.Z`.
3. The **Release** workflow checks the tag matches `metadata.txt`, re-runs lint and tests, builds
   `kentucky_stac-X.Y.Z.zip`, and attaches it to a GitHub release with the changelog entry as its notes
   (marked pre-release only if `metadata.txt` has `experimental=True`).
4. Upload that zip at <https://plugins.qgis.org/plugins/kentucky_stac/> via **Manage**. This step is manual
   because it needs your OSGeo login. The plugin has been stable since 0.2.0; add `experimental=True` to
   `metadata.txt` only for a release that should be hidden from default installs.

To build the zip locally: `python scripts/package.py` writes it to `dist/`.

## License

GPL-2.0-or-later. See [LICENSE](LICENSE).
