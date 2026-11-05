"""Tests for the PDAL pipeline builders. pdal_clip imports qgis.core, so these run only where QGIS is
installed (the QGIS job in CI, or a QGIS Python console) and are skipped everywhere else."""

import json

import pytest

pdal_clip = pytest.importorskip("kentucky_stac.pdal_clip", reason="needs QGIS (qgis.core)")

POLYGON = "POLYGON((0 0,1 0,1 1,0 0))"


def _stages(*args):
    return json.loads(pdal_clip._pipeline_json(*args))["pipeline"]


def test_pipeline_filters_after_cropping():
    plain = _stages("in.laz", "out.laz", [], "Classification[2:2]")
    assert [s["type"] for s in plain] == ["readers.las", "filters.range", "writers.las"]
    assert plain[1]["limits"] == "Classification[2:2]" and plain[2]["inputs"] == ["keep"]
    both = _stages("in.laz", "out.laz", [POLYGON], "ReturnNumber[1:1]")
    assert [s["type"] for s in both] == ["readers.las", "filters.crop", "filters.range", "writers.las"]
    assert both[2]["inputs"] == ["crop0"]
    clip_only = _stages("in.laz", "out.laz", [POLYGON])
    assert "filters.range" not in [s["type"] for s in clip_only]
    last = _stages("in.laz", "out.laz", [], "Classification[2:2]", "ReturnNumber == NumberOfReturns")
    assert [s["type"] for s in last] == ["readers.las", "filters.range", "filters.expression", "writers.las"]
    assert last[2]["inputs"] == ["keep"] and last[3]["inputs"] == ["match"]


def test_filtered_names():
    filtered = pdal_clip.filtered_path("D:/x/N1.copc.laz", "D:/out")
    assert filtered.replace("\\", "/") == "D:/out/N1.filtered.laz"
    clipped = pdal_clip.filtered_path("D:/x/N1.laz", "D:/out", clipped=True)
    assert clipped.replace("\\", "/") == "D:/out/N1.clipped.filtered.laz"
