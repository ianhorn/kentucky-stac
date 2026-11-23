"""The search Filters: what a search sends must not depend on whether the Filters section is open."""

import os

import pytest

pytest.importorskip("qgis.core", reason="needs QGIS (qgis.core)")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from kentucky_stac.filters_panel import DEFAULT_MAX_CLOUD, FiltersPanel  # noqa: E402
from kentucky_stac.stac import SearchQuery  # noqa: E402


@pytest.fixture
def panel(qgis_app):
    widget = FiltersPanel(False)
    widget.show()
    yield widget
    widget.close()


def test_the_cloud_filter_is_sent_whether_the_filters_section_is_open_or_collapsed(panel):
    """The reported bug: set the filter, search, collapse the section, search again: the second search was unfiltered."""
    panel.header.setChecked(True)
    panel.cloud_check.setChecked(True)
    assert panel.apply(SearchQuery()).max_cloud_cover == float(DEFAULT_MAX_CLOUD)
    panel.header.setChecked(False)  # collapse it
    assert panel.apply(SearchQuery()).max_cloud_cover == float(DEFAULT_MAX_CLOUD)
    assert "1 active" in panel.header.text()  # what the header promises is what is sent


def test_the_other_filters_are_sent_when_collapsed_too(panel):
    panel.header.setChecked(True)
    panel.from_check.setChecked(True)
    panel.to_check.setChecked(True)
    panel.sort_combo.setCurrentIndex(1)
    panel.header.setChecked(False)
    query = panel.apply(SearchQuery())
    assert query.start is not None and query.end is not None and query.sortby == "desc"
    assert "2 active" in panel.header.text()


def test_the_filters_stay_for_the_next_search_until_reset(panel):
    panel.cloud_check.setChecked(True)
    panel.cloud_spin.setValue(35)
    for _ in range(3):  # search after search
        assert panel.apply(SearchQuery()).max_cloud_cover == 35.0
    panel.reset()
    assert panel.apply(SearchQuery()).max_cloud_cover is None and panel.cloud_spin.value() == DEFAULT_MAX_CLOUD


def test_a_point_cloud_tab_never_sends_cloud_cover(qgis_app):
    lidar = FiltersPanel(True)
    lidar.cloud_check.setChecked(True)  # not shown on this tab, but however it got ticked
    assert lidar.apply(SearchQuery()).max_cloud_cover is None
    assert "active" not in lidar.header.text()
