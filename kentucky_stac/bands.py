"""Which bands a STAC item offers, and the band combinations and spectral indices that can be made from them.

Pure Python -- no QGIS import -- so it's unit-testable. The layers themselves are built in band_layers.py.

A *band* is one raster band of one asset, found by its common name (red, green, blue, nir, swir16, ...),
which STAC catalogs give in `eo:bands` / `bands`, or failing that by the asset's key. A formula such as
`(nir - red) / (nir + red)` names bands this way and is checked here before anything is opened.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from .catalog import can_add_to_map
from .stac import Item


@dataclass(frozen=True)
class Band:
    asset: str  # the asset's key
    index: int  # 1-based band number inside that asset's file
    label: str  # for menus: the band's name, else the asset's title or key
    common: str  # normalised common name ("red", "nir", "swir16", ...) or ""
    href: str
    scale: float = 1.0  # raster:bands scale/offset: value = DN * scale + offset
    offset: float = 0.0


# A wanted name -> the common names (or asset keys) that can stand in for it, best first.
ALIASES: Dict[str, Tuple[str, ...]] = {
    "nir": ("nir", "nir08", "nir09"),
    "swir16": ("swir16", "swir1"),
    "swir22": ("swir22", "swir2"),
    "rededge": ("rededge", "rededge1", "rededge2", "rededge3"),
    "coastal": ("coastal", "aerosol"),
    "lwir": ("lwir", "lwir11", "lwir12"),
}

# Red, green, blue channels of each band combination.
COMBINATIONS: Dict[str, Tuple[str, str, str]] = {
    "True color": ("red", "green", "blue"),
    "Color infrared (vegetation)": ("nir", "red", "green"),
    "Shortwave infrared": ("swir22", "nir", "red"),
    "False color (urban)": ("swir22", "swir16", "red"),
    "Agriculture": ("swir16", "nir", "blue"),
    "Geology": ("swir22", "swir16", "blue"),
    "Bathymetric": ("red", "green", "coastal"),
}

INDICES: Dict[str, str] = {
    "NDVI (vegetation)": "(nir - red) / (nir + red)",
    "NDWI (water, green/NIR)": "(green - nir) / (green + nir)",
    "MNDWI (water, green/SWIR)": "(green - swir16) / (green + swir16)",
    "NDMI (moisture)": "(nir - swir16) / (nir + swir16)",
    "NBR (burn ratio)": "(nir - swir22) / (nir + swir22)",
    "NDBI (built-up)": "(swir16 - nir) / (swir16 + nir)",
    "SAVI (soil-adjusted vegetation)": "1.5 * (nir - red) / (nir + red + 0.5)",
    "EVI (enhanced vegetation)": "2.5 * (nir - red) / (nir + 6 * red - 7.5 * blue + 1)",
}

FUNCTIONS = ("sqrt", "abs", "ln", "log10", "sin", "cos", "tan", "asin", "acos", "atan", "min", "max")
_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_ALLOWED = re.compile(r"^[A-Za-z0-9_ ().,+\-*/^]*$")


def _norm(name: Optional[str]) -> str:
    return (name or "").strip().lower().replace("-", "").replace(" ", "")


def _entry_value(entry: dict, *names: str):
    for name in names:
        if entry.get(name) is not None:
            return entry[name]
    return None


def item_bands(item: Item) -> List[Band]:
    """Every raster band the item offers, one per band of each mappable asset (a single-band asset gives one)."""
    raw_assets = (item.raw or {}).get("assets", {}) or {}
    out: List[Band] = []
    for key, asset in item.assets.items():
        if not asset.href or not can_add_to_map(asset) or asset.is_copc:
            continue
        raw = raw_assets.get(key, {}) or {}
        entries = raw.get("eo:bands") or raw.get("bands") or []
        rasters = raw.get("raster:bands") or []
        title = asset.title or key
        if not entries:
            common = _norm(_entry_value(raw, "eo:common_name", "common_name")) or _norm(key)
            scale, offset = _scale_offset(rasters[0] if rasters else raw)
            out.append(Band(key, 1, title, common, asset.href, scale, offset))
            continue
        for i, entry in enumerate(entries):
            common = _norm(_entry_value(entry, "common_name", "eo:common_name")) or (_norm(key) if len(entries) == 1 else "")
            label = str(entry.get("description") or entry.get("name") or f"band {i + 1}")
            scale, offset = _scale_offset(rasters[i] if i < len(rasters) else entry)
            out.append(Band(key, i + 1, f"{title}: {label}" if len(entries) > 1 else title, common, asset.href, scale, offset))
    return out


def _scale_offset(entry: dict) -> Tuple[float, float]:
    try:
        return float(entry.get("scale", 1.0) or 1.0), float(entry.get("offset", 0.0) or 0.0)
    except (TypeError, ValueError):
        return 1.0, 0.0


def find_band(bands: Sequence[Band], wanted: str) -> Optional[Band]:
    """The band called `wanted`: a common name or an asset key, trying the usual stand-ins (nir08 for nir...)."""
    names = ALIASES.get(_norm(wanted), (_norm(wanted),))
    for name in names:
        for band in bands:
            if band.common == name:
                return band
        for band in bands:
            if _norm(band.asset) == name and band.index == 1:
                return band
    return None


def available_combinations(bands: Sequence[Band]) -> List[str]:
    return [name for name, channels in COMBINATIONS.items() if all(find_band(bands, c) for c in channels)]


def formula_names(formula: str) -> List[str]:
    """The band names a formula uses (not its function names), in order of first use."""
    seen: List[str] = []
    for match in _NAME.finditer(formula):
        name = match.group(0)
        if name.lower() in FUNCTIONS and formula[match.end():].lstrip().startswith("("):
            continue
        if name not in seen:
            seen.append(name)
    return seen


def check_formula(formula: str) -> Optional[str]:
    """A problem with the formula, or None. Only arithmetic, band names and a few functions are allowed."""
    text = (formula or "").strip()
    if not text:
        return "Enter a formula."
    if not _ALLOWED.match(text):
        return "Use only band names, numbers, + - * / ^ ( ) and the functions " + ", ".join(FUNCTIONS) + "."
    if text.count("(") != text.count(")"):
        return "The brackets don't match."
    if not formula_names(text):
        return "The formula names no band."
    return None


def available_indices(bands: Sequence[Band], extra: Optional[Dict[str, str]] = None) -> List[str]:
    """The index names (built in, then the user's own) whose bands are all present."""
    every = dict(INDICES)
    every.update(extra or {})
    return [name for name, formula in every.items() if check_formula(formula) is None and all(find_band(bands, n) for n in formula_names(formula))]


def to_expression(formula: str, resolve: Callable[[Band], str], bands: Sequence[Band]) -> str:
    """The formula with each band name replaced by the text `resolve` gives for that band.

    The band's scale and offset are applied (`(token * scale + offset)`) so a Landsat DN becomes reflectance,
    which matters for ratios because the offset does not cancel. Raises ValueError for a name no band answers to.
    """
    missing = [n for n in formula_names(formula) if find_band(bands, n) is None]
    if missing:
        raise ValueError("this tile has no band called " + ", ".join(missing))

    def swap(match: "re.Match[str]") -> str:
        name = match.group(0)
        if name.lower() in FUNCTIONS and formula[match.end():].lstrip().startswith("("):
            return name
        band = find_band(bands, name)
        token = resolve(band)
        if band.scale != 1.0 or band.offset != 0.0:
            token = f"({token} * {band.scale!r} + {band.offset!r})"
        return token

    return _NAME.sub(swap, formula)


def bands_for_formula(formula: str, bands: Sequence[Band]) -> List[Band]:
    return [b for b in (find_band(bands, n) for n in formula_names(formula)) if b is not None]
