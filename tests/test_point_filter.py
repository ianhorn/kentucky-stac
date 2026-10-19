import json

from kentucky_stac.pdal_clip import _pipeline_json, filtered_path
from kentucky_stac.point_filter import ALL_CLASSES, PRESETS, PointFilter


def test_default_keeps_everything():
    everything = PointFilter()
    assert not everything.active and not everything.empty
    assert everything.qgis_expression() == "" and everything.pdal_limits() == "" and everything.pdal_expression() == ""
    assert everything.summary() == "all points"


def test_single_class():
    ground = PointFilter(classes=frozenset({2}))
    assert ground.active
    assert ground.qgis_expression() == "Classification = 2"
    assert ground.pdal_limits() == "Classification[2:2]"
    assert ground.summary() == "Ground"


def test_separate_classes_are_ored_and_neighbours_joined():
    two = PointFilter(classes=frozenset({2, 6}))
    assert two.qgis_expression() == "(Classification = 2 OR Classification = 6)"
    assert two.pdal_limits() == "Classification[2:2],Classification[6:6]"
    vegetation = PointFilter(classes=frozenset({3, 4, 5}))
    assert vegetation.qgis_expression() == "Classification >= 3 AND Classification <= 5"
    assert vegetation.pdal_limits() == "Classification[3:5]"


def test_other_classes_stay_in_when_the_open_box_is_ticked():
    without_noise = PointFilter(classes=PRESETS["Without noise"])
    assert "Classification[0:6]" in without_noise.pdal_limits()
    assert without_noise.pdal_limits().endswith("Classification[8:17],Classification[19:255]")
    assert without_noise.qgis_expression().endswith("Classification >= 19)")


def test_first_return():
    first = PointFilter(returns="first")
    assert first.active and first.qgis_expression() == "ReturnNumber = 1"
    assert first.pdal_limits() == "ReturnNumber[1:1]" and first.pdal_expression() == ""
    assert first.summary() == "first return"
    both = PointFilter(classes=frozenset({2}), returns="first")
    assert both.qgis_expression() == "Classification = 2 AND ReturnNumber = 1"
    assert both.pdal_limits() == "Classification[2:2],ReturnNumber[1:1]"
    assert both.summary() == "Ground; first return"


def test_last_return_compares_two_dimensions():
    last = PointFilter(returns="last")
    assert last.active and last.qgis_expression() == "ReturnNumber = NumberOfReturns"
    assert last.pdal_limits() == "" and last.pdal_expression() == "ReturnNumber == NumberOfReturns"
    both = PointFilter(classes=frozenset({2}), returns="last")
    assert both.qgis_expression() == "Classification = 2 AND ReturnNumber = NumberOfReturns"
    assert both.pdal_limits() == "Classification[2:2]"


def test_no_class_ticked_is_empty_and_gives_no_rule():
    nothing = PointFilter(classes=frozenset(), returns="last")
    assert nothing.empty
    assert nothing.qgis_expression() == "" and nothing.pdal_limits() == "" and nothing.pdal_expression() == ""


def test_summary_is_short():
    assert PointFilter(classes=frozenset({2, 6}), returns="last").summary() == "Ground, Building; last return"
    assert PointFilter(classes=ALL_CLASSES - {7}).summary() == "19 classes"


def test_pipeline_filters_after_cropping():
    plain = json.loads(_pipeline_json("in.laz", "out.laz", [], "Classification[2:2]"))["pipeline"]
    assert [s["type"] for s in plain] == ["readers.las", "filters.range", "writers.las"]
    assert plain[1]["limits"] == "Classification[2:2]" and plain[2]["inputs"] == ["keep"]
    both = json.loads(_pipeline_json("in.laz", "out.laz", ["POLYGON((0 0,1 0,1 1,0 0))"], "ReturnNumber[1:1]"))["pipeline"]
    assert [s["type"] for s in both] == ["readers.las", "filters.crop", "filters.range", "writers.las"]
    assert both[2]["inputs"] == ["crop0"]
    clip_only = json.loads(_pipeline_json("in.laz", "out.laz", ["POLYGON((0 0,1 0,1 1,0 0))"]))["pipeline"]
    assert "filters.range" not in [s["type"] for s in clip_only]
    last = json.loads(_pipeline_json("in.laz", "out.laz", [], "Classification[2:2]", "ReturnNumber == NumberOfReturns"))["pipeline"]
    assert [s["type"] for s in last] == ["readers.las", "filters.range", "filters.expression", "writers.las"]
    assert last[2]["inputs"] == ["keep"] and last[3]["inputs"] == ["match"]


def test_filtered_names():
    assert filtered_path("D:/x/N1.copc.laz", "D:/out").replace("\\", "/") == "D:/out/N1.filtered.laz"
    assert filtered_path("D:/x/N1.laz", "D:/out", clipped=True).replace("\\", "/") == "D:/out/N1.clipped.filtered.laz"
