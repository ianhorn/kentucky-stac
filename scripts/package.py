"""Build the plugin zip the QGIS plugin repository (and Install from ZIP) expects.

    python scripts/package.py [--out-dir dist] [--notes release-notes.md]

The zip holds the `kentucky_stac/` folder as its single top-level entry. `--notes` also writes the newest
changelog entry from metadata.txt, for use as GitHub release notes. Stdlib only.
"""

from __future__ import annotations

import argparse
import re
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PLUGIN_DIR = ROOT / "kentucky_stac"
SKIP_DIRS = {"__pycache__"}
SKIP_SUFFIXES = {".pyc", ".pyo"}
SKIP_NAMES = {".DS_Store", "Thumbs.db"}


def metadata_text() -> str:
    return (PLUGIN_DIR / "metadata.txt").read_text(encoding="utf-8")


def plugin_version() -> str:
    match = re.search(r"^version=(\S+)$", metadata_text(), re.M)
    if not match:
        raise SystemExit("metadata.txt has no version= line")
    return match.group(1)


def latest_changelog_entry() -> str:
    """The text of the first changelog entry (metadata.txt keeps newest first; continuation lines are indented)."""
    match = re.search(r"^changelog=(.*)$", metadata_text(), re.M)
    if not match:
        raise SystemExit("metadata.txt has no changelog= line")
    return match.group(1).strip()


def plugin_files():
    for path in sorted(PLUGIN_DIR.rglob("*")):
        if not path.is_file():
            continue
        relative = path.relative_to(PLUGIN_DIR)
        if SKIP_DIRS & set(relative.parts) or path.suffix in SKIP_SUFFIXES or path.name in SKIP_NAMES:
            continue
        yield path


def build_zip(out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    target = out_dir / f"kentucky_stac-{plugin_version()}.zip"
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in plugin_files():
            archive.write(path, Path("kentucky_stac") / path.relative_to(PLUGIN_DIR))
    return target


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--out-dir", type=Path, default=ROOT / "dist")
    parser.add_argument("--notes", type=Path, help="also write the newest changelog entry to this file")
    args = parser.parse_args()
    print(build_zip(args.out_dir))
    if args.notes:
        args.notes.write_text(latest_changelog_entry() + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
