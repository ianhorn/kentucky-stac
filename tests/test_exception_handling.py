"""Exception handling: expected failures fall back quietly, unexpected ones are no longer swallowed.
Needs QGIS (these modules import qgis.core), so it is skipped outside the QGIS CI job."""

import pytest

pytest.importorskip("qgis.core", reason="needs QGIS (qgis.core)")

from kentucky_stac import diagnostics, signing  # noqa: E402
from kentucky_stac.stac import StacError  # noqa: E402


@pytest.fixture(autouse=True)
def _empty_token_cache():
    signing._cache.clear()
    yield
    signing._cache.clear()


def test_log_exception_writes_the_traceback(monkeypatch):
    logged = []
    monkeypatch.setattr(diagnostics.QgsMessageLog, "logMessage", lambda *args: logged.append(args))
    try:
        raise ValueError("boom")
    except ValueError:
        diagnostics.log_exception("Could not do the thing")
    message, tag, _level = logged[0]
    assert tag == "Kentucky STAC"
    assert "Could not do the thing" in message and "ValueError: boom" in message


@pytest.mark.parametrize("failure", [StacError("offline"), ValueError("not json"), KeyError("token")])
def test_token_lookup_failures_give_no_token_and_are_remembered(monkeypatch, failure):
    calls = []

    def fake_transport(*args, **kwargs):
        calls.append(args)
        if isinstance(failure, KeyError):
            return b"{}"  # valid JSON without a "token"
        if isinstance(failure, ValueError):
            return b"<html>"
        raise failure

    monkeypatch.setattr(signing, "qgis_transport", fake_transport)
    assert signing._fetch_token("acct", "cont") is None
    assert signing._fetch_token("acct", "cont") is None  # the failure is cached, so no second request
    assert len(calls) == 1


def test_unexpected_errors_in_token_lookup_are_not_hidden(monkeypatch):
    def broken_transport(*args, **kwargs):
        raise RuntimeError("a bug, not a network problem")

    monkeypatch.setattr(signing, "qgis_transport", broken_transport)
    with pytest.raises(RuntimeError):
        signing._fetch_token("acct", "cont")
