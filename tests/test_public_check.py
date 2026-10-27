import pytest

from app.public_check import normalise_url
from app.validation import ValidationError


def test_normalise_url_lowercases_the_host_and_keeps_the_path_case():
    assert normalise_url("https://EXAMPLE.com/Path") == "https://example.com/Path"


def test_normalise_url_adds_a_slash_for_an_empty_path():
    assert normalise_url("https://example.com") == "https://example.com/"


def test_normalise_url_drops_the_fragment_and_the_default_port():
    assert normalise_url("http://example.com:80/a#top") == "http://example.com/a"
    assert normalise_url("https://example.com:443/") == "https://example.com/"


def test_normalise_url_keeps_a_non_default_port_and_the_query():
    assert normalise_url("https://example.com:8443/a?x=1") == "https://example.com:8443/a?x=1"


def test_normalise_url_strips_spaces_around_the_url():
    assert normalise_url("  https://example.com/  ") == "https://example.com/"


def test_normalise_url_rejects_a_bad_scheme():
    with pytest.raises(ValidationError):
        normalise_url("ftp://example.com/")


def test_normalise_url_rejects_a_username_and_password():
    with pytest.raises(ValidationError):
        normalise_url("https://user:pass@example.com/")


def test_normalise_url_rejects_an_invalid_port():
    with pytest.raises(ValidationError):
        normalise_url("https://example.com:99999/")
