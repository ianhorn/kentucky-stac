from urllib.parse import parse_qs, urlparse

from kentucky_stac.feedback import (
    REPO_URL,
    SUBJECT,
    direct_payload,
    environment_summary,
    github_issue_url,
    summarize_error,
)


def test_environment_summary_skips_unknown_parts():
    assert environment_summary("0.1.0", "3.44.4", " Windows-11 ") == "Kentucky STAC 0.1.0 | QGIS 3.44.4 | Windows-11"
    assert environment_summary("", None, None) == "Kentucky STAC unknown"


def test_github_issue_url_prefills_template_and_environment():
    url = github_issue_url("Kentucky STAC 0.1.0 | QGIS 3.44")
    assert url.startswith(f"{REPO_URL}/issues/new?")
    body = parse_qs(urlparse(url).query)["body"][0]
    assert "**What happened?**" in body and body.endswith("Environment: Kentucky STAC 0.1.0 | QGIS 3.44")


def test_direct_payload():
    p = direct_payload("It broke.\n\n", "  me@example.com ", "ENV")
    assert p["message"] == "It broke.\n\n---\nEnvironment: ENV"
    assert p["_subject"] == SUBJECT and p["_replyto"] == "me@example.com" and p["_gotcha"] == ""
    assert direct_payload("x", "", "ENV")["_replyto"] is None


def test_summarize_error():
    assert summarize_error('{"errors": [{"message": "Form not found"}]}') == "Form not found"
    assert summarize_error('{"error": "Rate limited"}') == "Rate limited"
    assert summarize_error("<html>oops</html>") == "<html>oops</html>"
