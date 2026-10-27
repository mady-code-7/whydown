"""Tests for the WhyDown frontend static files."""

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _read(relative: str) -> str:
    return (ROOT / relative).read_text(encoding="utf-8")


def test_firebase_json_serves_the_frontend_folder_from_the_whydown_site():
    data = json.loads(_read("firebase.json"))
    assert data["hosting"]["public"] == "frontend"
    assert data["hosting"]["site"] == "whydown-nm"


def test_firebaserc_points_to_the_whydown_project():
    assert json.loads(_read(".firebaserc"))["projects"]["default"] == "whydown-nm"


def test_index_html_has_the_form_and_result_elements():
    text = _read("frontend/index.html")
    for element_id in [
        "check-form",
        "url-input",
        "check-button",
        "status",
        "result",
        "result-title",
        "result-note",
        "result-error",
        "stage-table",
        "stage-rows",
        "result-meta",
    ]:
        assert f'id="{element_id}"' in text


def test_index_html_links_the_stylesheet_and_the_script():
    text = _read("frontend/index.html")
    assert 'href="style.css"' in text
    assert 'src="app.js"' in text


def test_index_html_loads_nothing_from_other_sites():
    text = _read("frontend/index.html")
    assert "src=\"http" not in text
    assert 'href="http' not in text
    assert text.count("<script") == 1


def test_index_html_has_no_inline_scripts_or_handlers():
    text = _read("frontend/index.html").lower()
    assert "onclick=" not in text
    assert "<script>" not in text


def test_style_css_supports_dark_mode_and_the_hidden_attribute():
    text = _read("frontend/style.css")
    assert "prefers-color-scheme: dark" in text
    assert "[hidden]" in text