"""Pytest unit tests for the load-test helper functions.

These tests exercise the pure helpers from ``tests.load_test`` without
starting the mock server or touching the network.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from tests.load_test import (
    build_sites,
    check_url_with_private,
    evaluate,
    summarise,
)


class TestBuildSites:
    """Tests for the pure site-list builder."""

    def test_returns_twenty_sites(self):
        sites = build_sites(12345)
        assert len(sites) == 20

    def test_categories_distribution(self):
        sites = build_sites(12345)
        # Count by path pattern
        fast = [s for s in sites if "/ok" in s["url"]]
        slow = [s for s in sites if "/slow?ms=6000" in s["url"]]
        hang = [s for s in sites if "/hang" in s["url"]]
        errors = [s for s in sites if "/status/500" in s["url"]]
        assert len(fast) == 5
        assert len(slow) == 5
        assert len(hang) == 5
        assert len(errors) == 5

    def test_ids_are_sequential(self):
        sites = build_sites(12345)
        ids = [s["id"] for s in sites]
        assert ids == list(range(1, 21))

    def test_all_enabled(self):
        sites = build_sites(12345)
        assert all(s["enabled"] for s in sites)

    def test_slow_threshold_is_5000(self):
        sites = build_sites(12345)
        assert all(s["slow_threshold_ms"] == 5000 for s in sites)

    def test_expected_status_200(self):
        sites = build_sites(12345)
        assert all(s["expected_status"] == 200 for s in sites)


class TestSummarise:
    """Tests for the summary extractor."""

    def test_extracts_counts(self):
        summary = {
            "sites_checked": 20,
            "counts": {"OK": 8, "SLOW": 5, "CONNECT_TIMEOUT": 5, "HTTP_5XX": 2},
        }
        stats = summarise(summary)
        assert stats["sites_checked"] == 20
        assert stats["ok"] == 8
        assert stats["slow"] == 5
        assert stats["timeout"] == 5
        assert stats["http_5xx"] == 2
        assert stats["other"] == 0

    def test_missing_keys_default_to_zero(self):
        summary = {"sites_checked": 0, "counts": {}}
        stats = summarise(summary)
        assert stats == {
            "sites_checked": 0,
            "ok": 0,
            "slow": 0,
            "timeout": 0,
            "http_5xx": 0,
            "other": 0,
        }

    def test_handles_extra_keys_gracefully(self):
        summary = {
            "sites_checked": 10,
            "counts": {"OK": 3, "SLOW": 2, "DNS_FAILURE": 1, "INTERNAL_ERROR": 4},
        }
        stats = summarise(summary)
        assert stats["sites_checked"] == 10
        assert stats["ok"] == 3
        assert stats["slow"] == 2
        assert stats["timeout"] == 0
        assert stats["http_5xx"] == 0
        assert stats["other"] == 5  # DNS_FAILURE + INTERNAL_ERROR


class TestEvaluate:
    """Tests for the T18 pass/fail logic."""

    def test_passes_when_five_slow_and_under_deadline(self):
        summary = {
            "sites_checked": 20,
            "counts": {"OK": 10, "SLOW": 5, "CONNECT_TIMEOUT": 5, "HTTP_5XX": 0},
        }
        # Well under the 25 s deadline
        assert evaluate(summary, 15_000) is True

    def test_fails_when_wrong_slow_count(self):
        summary = {
            "sites_checked": 20,
            "counts": {"OK": 15, "SLOW": 3, "CONNECT_TIMEOUT": 2, "HTTP_5XX": 0},
        }
        assert evaluate(summary, 10_000) is False

    def test_fails_when_over_deadline(self):
        summary = {
            "sites_checked": 20,
            "counts": {"OK": 10, "SLOW": 5, "CONNECT_TIMEOUT": 5, "HTTP_5XX": 0},
        }
        # 30 s > 25 s deadline
        assert evaluate(summary, 30_000) is False

    def test_fails_when_wrong_total_sites(self):
        summary = {
            "sites_checked": 15,
            "counts": {"OK": 10, "SLOW": 5},
        }
        assert evaluate(summary, 10_000) is False

    def test_boundary_exact_deadline(self):
        summary = {
            "sites_checked": 20,
            "counts": {"OK": 10, "SLOW": 5, "CONNECT_TIMEOUT": 5},
        }
        # Exactly at deadline should fail (strict <)
        assert evaluate(summary, 25_000) is False
        # Just under should pass
        assert evaluate(summary, 24_999) is True


class TestCheckUrlWithPrivate:
    """Test the wrapper that forces allow_private=True.

    We don't actually hit the network here – we just verify the wrapper
    forwards the right arguments and the flag is set.
    """

    def test_forwards_all_args(self):
        with patch("tests.load_test.check_url") as mock_check:
            mock_check.return_value = type("R", (), {"result": "OK"})()
            check_url_with_private(
                "http://example.com",
                slow_threshold_ms=123,
                cert_expiry_warning_days=14,
                expected_status=200,
                expected_keyword="foo",
            )
            args, kwargs = mock_check.call_args
            assert args == ("http://example.com",)
            assert kwargs["slow_threshold_ms"] == 123
            assert kwargs["cert_expiry_warning_days"] == 14
            assert kwargs["expected_status"] == 200
            assert kwargs["expected_keyword"] == "foo"
            assert kwargs["allow_private"] is True


if __name__ == "__main__":
    pytest.main([__file__, "-v"])