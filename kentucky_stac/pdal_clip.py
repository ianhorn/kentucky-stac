"""Crop downloaded point cloud tiles to an AOI polygon via PDAL, one process per tile, before they
are combined into a Virtual Point Cloud (or added directly, for a single tile).

Ported from the old ArcGIS Pro add-in's CopcClipService.cs and re-verified live against a real
downloaded COPC tile with a real AOI polygon (2026-09-28: a west-half crop went from 14,372,853 to
6,307,560 points with matching bounds; a two-disjoint-corner AOI produced only those two regions,
not the tile's full bounding box, confirming filters.merge -- not the full extent -- drives the
output). The constraints below are carried over from that add-in, not re-derived:

 - Crop always via readers.las, never readers.copc's own "polygon" option -- readers.copc's polygon
   crop can flat-out CRASH PDAL (a native segfault, not a clean error) on a complex/high-vertex
   polygon. readers.las reads a COPC file's points identically (COPC is a plain LASzip-compressed
   stream underneath; the octree VLRs readers.copc uses for spatially-indexed partial reads are
   optional metadata a plain LAZ reader just ignores, decompressing every point regardless).
 - filters.crop needs an explicit "a_srs" naming the CRS the *given polygon* is in (EPSG:4326 here,
   matching the STAC search geometry) -- it otherwise assumes the polygon is in the same CRS as the
   point data (a Kentucky State Plane variant, in feet), and every point silently fails the crop,
   producing an empty/degenerate output.
 - filters.crop given several regions does NOT union them -- it produces one output point view PER
   region. A multi-part AOI (see aoi.wkt_parts) needs one filters.crop stage per part off a single
   reader, recombined via filters.merge before the writer.
 - Output is LAZ-compressed (writers.las with compression=true), not plain LAS -- verified: the same
   point count, about 5x smaller on disk (42MB vs 227MB for one 6.3M-point crop).

`real_metadata()` exists because of a bug caught by live testing: vpc.py's build_vpc() copies
pc:count/bbox/geometry straight from the original STAC item, which is correct for an unmodified
download but describes the PRE-crop tile once the linked file has actually been cropped -- a VPC
built that way opens fine and even renders real points, but reports the original, uncropped point
count and footprint (caught by comparing a QGIS-reported combined point count against the actual
per-file counts from `pdal info` on the real cropped files on disk: the layer claimed the full,
uncropped total while the files themselves were genuinely smaller). Every cropped tile's STAC item
must have its bbox/geometry/pc:count replaced with the real, post-crop values before build_vpc() runs.
"""

from __future__ import annotations

import json
import os
import subprocess
from typing import Any, Callable, Dict, List, Optional, Tuple

from qgis.core import QgsCoordinateReferenceSystem, QgsCoordinateTransform, QgsGeometry, QgsProject, QgsRectangle, QgsTask

_PDAL_TIMEOUT = 600  # seconds per tile -- a local crop of one tile, generous but not unbounded


def clipped_path(source_path: str, out_dir: str) -> str:
    """Where a cropped copy of `source_path` should be written."""
    base = os.path.basename(source_path)
    for ext in (".copc.laz", ".laz", ".las"):
        if base.lower().endswith(ext):
            base = base[: -len(ext)]
            break
    return os.path.join(out_dir, base + ".clipped.laz")


def _pipeline_json(input_path: str, output_path: str, wkt_parts: List[str]) -> str:
    stages = [{"type": "readers.las", "filename": input_path, "tag": "read"}]
    crop_tags = []
    for i, wkt in enumerate(wkt_parts):
        tag = f"crop{i}"
        crop_tags.append(tag)
        stages.append({"type": "filters.crop", "polygon": wkt, "a_srs": "EPSG:4326", "inputs": ["read"], "tag": tag})
    final_tag = crop_tags[0]
    if len(crop_tags) > 1:
        stages.append({"type": "filters.merge", "inputs": crop_tags, "tag": "merged"})
        final_tag = "merged"
    stages.append({"type": "writers.las", "filename": output_path, "compression": True, "inputs": [final_tag]})
    return json.dumps({"pipeline": stages})


def crop_one(input_path: str, output_path: str, wkt_parts: List[str], pdal_exe: str = "pdal") -> Optional[str]:
    """Crop one tile to `wkt_parts` (polygon WKT strings in EPSG:4326, one per AOI part). Returns
    an error message on failure, None on success."""
    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    pipeline_path = output_path + ".pipeline.json"
    with open(pipeline_path, "w", encoding="utf-8") as f:
        f.write(_pipeline_json(input_path, output_path, wkt_parts))
    try:
        result = subprocess.run(
            [pdal_exe, "pipeline", pipeline_path], capture_output=True, text=True, timeout=_PDAL_TIMEOUT
        )
        if result.returncode != 0:
            message = (result.stderr or result.stdout or "pdal pipeline failed").strip()
            return (message.splitlines()[-1] if message else "pdal pipeline failed")[:300]
        if not os.path.exists(output_path):
            return "pdal pipeline reported success but wrote no output file"
        return None
    except subprocess.TimeoutExpired:
        return f"timed out after {_PDAL_TIMEOUT}s"
    except OSError as e:
        return str(e)
    finally:
        try:
            os.remove(pipeline_path)
        except OSError:
            pass


def real_metadata(path: str, pdal_exe: str = "pdal") -> Optional[Dict[str, Any]]:
    """The real point count and WGS84 bbox/geometry of a point cloud file already on disk, read via
    `pdal info` -- used to correct a cropped tile's declared STAC metadata (see the module docstring
    for why the original item's values can't just be reused). Returns None if `pdal info` itself
    fails; `count` is 0 (bbox/geometry None) if the crop produced no points at all -- the AOI simply
    doesn't reach this tile."""
    try:
        result = subprocess.run(
            [pdal_exe, "info", "--metadata", path], capture_output=True, text=True, timeout=_PDAL_TIMEOUT
        )
        if result.returncode != 0:
            return None
        meta = json.loads(result.stdout)["metadata"]
        count = int(meta.get("count") or 0)
    except (subprocess.TimeoutExpired, OSError, ValueError, KeyError):
        return None
    if not count:
        return {"count": 0, "bbox": None, "geometry": None}

    geom = QgsGeometry.fromRect(QgsRectangle(meta["minx"], meta["miny"], meta["maxx"], meta["maxy"]))
    srs_wkt = meta.get("spatialreference")
    src_crs = QgsCoordinateReferenceSystem.fromWkt(srs_wkt) if srs_wkt else QgsCoordinateReferenceSystem()
    if src_crs.isValid():
        xf = QgsCoordinateTransform(src_crs, QgsCoordinateReferenceSystem("EPSG:4326"), QgsProject.instance())
        geom.transform(xf)
    box = geom.boundingBox()
    return {
        "count": count,
        "bbox": [box.xMinimum(), box.yMinimum(), box.xMaximum(), box.yMaximum()],
        "geometry": json.loads(geom.asJson(8)),
    }


class CropPointCloudsTask(QgsTask):
    """Crop each (source, destination) pair in `jobs` to `wkt_parts`, one PDAL process per tile, on
    a worker thread, then read back each result's real metadata (see real_metadata). `callback(cropped,
    failed, tile_meta)` runs on the main thread: `cropped` is the list of destination paths that were
    written and have at least one point, `failed` a list of (source basename, error) -- including a
    tile the AOI doesn't reach, whose crop produced zero points -- `tile_meta` maps a cropped path to
    its real {"count", "bbox", "geometry"}. Nothing runs if the task was cancelled."""

    def __init__(
        self,
        jobs: List[Tuple[str, str]],
        wkt_parts: List[str],
        callback: Callable[[List[str], List[Tuple[str, str]], Dict[str, dict]], None],
    ):
        super().__init__(f"Clipping {len(jobs)} tile{'s' if len(jobs) != 1 else ''} to the area of interest")
        self._jobs = jobs
        self._wkt_parts = wkt_parts
        self._callback = callback
        self.cropped: List[str] = []
        self.failed: List[Tuple[str, str]] = []
        self.tile_meta: Dict[str, dict] = {}

    def run(self) -> bool:
        for i, (src, dst) in enumerate(self._jobs):
            if self.isCanceled():
                return False
            error = crop_one(src, dst, self._wkt_parts)
            if error:
                self.failed.append((os.path.basename(src), error))
            else:
                meta = real_metadata(dst)
                if meta is None:
                    self.failed.append((os.path.basename(src), "cropped, but could not read the result's metadata"))
                elif not meta["count"]:
                    self.failed.append((os.path.basename(src), "outside the area of interest"))
                    try:
                        os.remove(dst)
                    except OSError:
                        pass
                else:
                    self.cropped.append(dst)
                    self.tile_meta[dst] = meta
            self.setProgress(100.0 * (i + 1) / len(self._jobs))
        return True

    def finished(self, result: bool) -> None:
        if not self.isCanceled():
            self._callback(self.cropped, self.failed, self.tile_meta)
