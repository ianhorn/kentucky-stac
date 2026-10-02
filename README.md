# Kentucky STAC

QGIS plugin for searching, loading and downloading [Kentucky From Above](https://kyfromabove.ky.gov/)
imagery, elevation and LiDAR (COPC) through its STAC API. Successor to the ArcGIS Pro add-ins
`kyfromabove-stac-addin` and `kylidar-addin`.

Full documentation: [`docs/index.html`](docs/index.html) (published via GitHub Pages once enabled on
this repo).

## Development

The plugin lives in `kentucky_stac/`. For live testing, junction that folder into a QGIS profile's
`python\plugins` directory (LTR and latest each have their own profile), then use the Plugin
Reloader plugin to pick up code changes.

## License

GPL-2.0-or-later.
