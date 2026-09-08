"""Tests for xpman.gui.feedback: report/body/URL composition (pure, no Qt)."""

from __future__ import annotations

from urllib.parse import parse_qs, urlparse

from xpman.gui.feedback import (
    CONTACT_EMAIL,
    GITHUB_REPO,
    FeedbackMessage,
    collect_diagnostics,
    github_issue_url,
    mailto_url,
    plaintext_report,
)

_DIAG = {"xpman version": "0.5.0", "OS": "Windows-11", "Python": "3.11.9"}


def _msg(category="bug", summary="Crash on launch", description="It crashed."):
    return FeedbackMessage(category=category, summary=summary, description=description, diagnostics=_DIAG)


def test_title_prefixes_the_human_category():
    assert _msg("bug").title() == "[Bug report] Crash on launch"
    assert _msg("feature").title() == "[Feature request] Crash on launch"
    assert _msg("feedback").title() == "[General feedback] Crash on launch"


def test_title_and_body_handle_empty_input_gracefully():
    m = _msg(summary="   ", description="")
    assert m.title() == "[Bug report] (no summary)"
    assert "(no description provided)" in m.body()


def test_body_includes_description_and_diagnostics():
    body = _msg(description="Steps: 1, 2, 3").body()
    assert "Steps: 1, 2, 3" in body
    assert "xpman version: 0.5.0" in body
    assert "Python: 3.11.9" in body


def test_github_label_maps_category():
    assert _msg("bug").github_label() == "bug"
    assert _msg("feature").github_label() == "enhancement"
    assert _msg("feedback").github_label() == "feedback"


def test_github_issue_url_targets_the_repo_and_prefills_fields():
    url = github_issue_url(_msg("feature", summary="Add dark mode"))
    parsed = urlparse(url)
    assert parsed.netloc == "github.com"
    assert parsed.path == f"/{GITHUB_REPO}/issues/new"
    q = parse_qs(parsed.query)
    assert q["title"] == ["[Feature request] Add dark mode"]
    assert q["labels"] == ["enhancement"]
    assert "It crashed." in q["body"][0]


def test_mailto_url_targets_contact_and_encodes_spaces_as_pct20():
    url = mailto_url(_msg())
    assert url.startswith(f"mailto:{CONTACT_EMAIL}?")
    # mail clients don't decode '+' as space in a mailto query -> spaces must be %20, never '+'.
    assert "+" not in url.split("?", 1)[1]
    q = parse_qs(urlparse(url).query)
    assert q["subject"] == ["[Bug report] Crash on launch"]


def test_plaintext_report_combines_title_and_body():
    report = plaintext_report(_msg())
    assert report.startswith("[Bug report] Crash on launch")
    assert "It crashed." in report


def test_collect_diagnostics_has_the_expected_keys():
    diag = collect_diagnostics()
    assert set(diag) == {"xpman version", "OS", "Python"}
    assert all(isinstance(v, str) and v for v in diag.values())
