# Manual QA checklist

CI covers the logic and the widgets' behaviour, but it can't click through QGIS against the live Kentucky From
Above catalog. Run this before a release, especially before dropping `experimental=True`.

**Setup.** Install the build under test (a linked `kentucky_stac/` folder, or the release zip via *Install from
ZIP*). Open **View → Panels → Log Messages** and keep the **Kentucky STAC** tab visible throughout.

**Reading the log.** The plugin writes a warning with a full traceback only when something unexpected fails in a
background task (adding a layer, building a mosaic, downloading, loading collections, searching, registering a
server mosaic). A normal run of this checklist should add **no** Kentucky STAC warnings. Copy any traceback into
the issue you file.

Use a small area (a few tiles, e.g. one county) so each step is quick. Tick the box when it behaves as described.

## Start-up

- [ ] QGIS starts with the plugin enabled and no error dialog; **Plugins → Kentucky STAC** opens the dock.
- [ ] Both tabs list collections (**Imagery / DEM** and **LiDAR Pointcloud**); the status line shows a count.
- [ ] *Reload* re-fetches the list.

## Area of interest

- [ ] Draw a **Polygon**: right-click finishes it, the tool deactivates itself, the AOI shows on the map.
- [ ] Draw a **Point** and a **Line**; each replaces the previous AOI.
- [ ] **Select features** (click, then drag a box) turns existing polygons into the AOI.
- [ ] **Current extent** and **Clear** work. After setting any AOI, a "Ky STAC AOI" scratch layer is
      created (no "could not save it to a layer" warning in the message bar).

## Imagery / DEM tab

- [ ] Tick a collection, *Zoom to collection* moves the map; with nothing ticked it is grayed out.
- [ ] **Search area of interest** lists tiles; the results list and the map footprints highlight together
      (hover a row, click a footprint, drag-select several).
- [ ] Filters: a date range, *Max cloud cover*, *Sort*, *Max tiles* each change the result as described.
- [ ] A truncated result says "showing the first N of M".
- [ ] **Add selected to map** streams the COGs; each lands where its footprint is (not far away in the wrong place).
- [ ] Right-click a raster: a band combination, an index (NDVI), and a single band each add a layer.
- [ ] **Export VRT** with two or more tiles writes a `.vrt` that opens and looks continuous.
- [ ] **Export as MosaicJSON** writes a file; **Add as server mosaic** adds a streaming layer.
- [ ] **Download selected...** shows sizes, downloads, and with *Clip to area of interest* ticked (polygon AOI)
      clips to the AOI shape. *Cancel* during a download stops it cleanly.
- [ ] **Export as script** produces a notebook / Python / shell script that downloads the same tiles when run.

## LiDAR Pointcloud tab

- [ ] Search lists point cloud tiles with point counts and a compound projection label.
- [ ] **Add selected to map** streams COPC tiles; the cloud is drawn in the right place (check a 3DEP collection).
- [ ] **Add selected as Virtual Point Cloud** combines two or more tiles into one layer.
- [ ] **Point filter**: keep only *Ground*, then add or download; the result has only those points. Unticking every
      class shows "The point filter keeps nothing" and does nothing.
- [ ] A **download with clip** (polygon AOI, PDAL on `PATH`) produces `.clipped.laz`; with a filter, `.filtered.laz`.
- [ ] With PDAL removed from `PATH`, clip and Virtual Point Cloud options report that PDAL is needed (no crash).

## Other STAC APIs (experimental)

- [ ] **Sources...** → pick one from the STAC Index dropdown → *Add*; tabs rename to **Raster** / **Point Cloud**
      and its collections are labeled `Title · Source`.
- [ ] A **Microsoft Planetary Computer** collection: search, add a tile to the map (Azure token signing), no 403.
- [ ] An unreachable or wrong URL is reported for that source only; the other sources still work.
- [ ] A catalog-of-catalogs URL (not an API) reports the catalogs to use instead.
- [ ] Switching back to **KyFromAbove** restores the original tab names.

## Robustness

- [ ] Search with no AOI set, or with a very large AOI, gives a clear message rather than a hang or traceback.
- [ ] Disconnect the network and run a search: a clear "Search failed" message, QGIS stays responsive.
- [ ] Close and reopen QGIS: the source list is remembered; no errors on start-up.
- [ ] Both **QGIS 3.34+ LTR** and the current release (or QGIS 4) pass the sections above.

## Result

Record the QGIS version, OS, plugin version, and anything that failed (with the Log Messages traceback). If
everything passes, the release can drop `experimental=True`; see "Releasing" in the [README](README.md).
