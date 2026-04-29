"""OFAC SDN sanctions list — load / cache / contains."""
from __future__ import annotations

import responses

from skr_crypto.server import sanctions
from skr_crypto.server.config import SANCTIONS_LIST_URL


def teardown_function(_fn):
    sanctions._override_for_tests(None)


# ---------------------------------------------------------------------------
# parse()
# ---------------------------------------------------------------------------


def test_parse_skips_blank_and_comments():
    text = (
        "TAddress1\n"
        "\n"
        "# this is a comment\n"
        "  TAddress2  \n"
        "\n"
    )
    assert sanctions._parse(text) == {"TAddress1", "TAddress2"}


def test_parse_strips_inline_whitespace():
    assert sanctions._parse("\n  TFoo  \n\nTBar\n") == {"TFoo", "TBar"}


# ---------------------------------------------------------------------------
# contains()
# ---------------------------------------------------------------------------


def test_contains_returns_none_when_not_loaded():
    sanctions._override_for_tests(None)
    assert sanctions.contains("TAnything") is None


def test_contains_true_for_match():
    sanctions._override_for_tests({"TBadActor"})
    assert sanctions.contains("TBadActor") is True


def test_contains_false_for_no_match():
    sanctions._override_for_tests({"TBadActor"})
    assert sanctions.contains("TGoodActor") is False


# ---------------------------------------------------------------------------
# load()
# ---------------------------------------------------------------------------


@responses.activate
def test_load_from_url_success(tmp_path, monkeypatch):
    monkeypatch.setattr(sanctions, "_cache_path",
                        lambda: tmp_path / "ofac.txt")
    # conftest disables refresh by default (hermetic) — flip it on
    # for this test, since we explicitly want the URL fetch path.
    monkeypatch.setattr(sanctions, "SANCTIONS_LIST_REFRESH", True)
    body = "TBlocked1\nTBlocked2\n# comment\n"
    responses.add(
        responses.GET, SANCTIONS_LIST_URL,
        body=body, status=200,
    )
    n = sanctions.load()
    assert n == 2
    assert sanctions.contains("TBlocked1") is True
    assert sanctions.contains("TBlocked2") is True
    assert sanctions.contains("TUnrelated") is False
    # Cache was written.
    cache = tmp_path / "ofac.txt"
    assert cache.exists()
    assert "TBlocked1" in cache.read_text()


@responses.activate
def test_load_falls_back_to_disk_cache_on_url_failure(tmp_path, monkeypatch):
    monkeypatch.setattr(sanctions, "_cache_path",
                        lambda: tmp_path / "ofac.txt")
    monkeypatch.setattr(sanctions, "SANCTIONS_LIST_REFRESH", True)
    (tmp_path / "ofac.txt").write_text("TFromCache1\nTFromCache2\n")
    responses.add(
        responses.GET, SANCTIONS_LIST_URL,
        status=503, body="upstream",
    )
    n = sanctions.load()
    assert n == 2
    assert sanctions.contains("TFromCache1") is True


@responses.activate
def test_load_empty_when_url_and_cache_both_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(sanctions, "_cache_path",
                        lambda: tmp_path / "no_cache_here.txt")
    monkeypatch.setattr(sanctions, "SANCTIONS_LIST_REFRESH", True)
    responses.add(
        responses.GET, SANCTIONS_LIST_URL,
        status=502, body="bzzzt",
    )
    n = sanctions.load()
    assert n == 0
    # contains() must NOT return None — list is loaded, just empty.
    assert sanctions.contains("TAnything") is False


@responses.activate
def test_load_skip_url_when_refresh_disabled(tmp_path, monkeypatch):
    """SANCTIONS_LIST_REFRESH=false skips the network entirely."""
    monkeypatch.setattr(sanctions, "_cache_path",
                        lambda: tmp_path / "ofac.txt")
    monkeypatch.setattr(sanctions, "SANCTIONS_LIST_REFRESH", False)
    (tmp_path / "ofac.txt").write_text("TOnlyFromCache\n")
    n = sanctions.load()
    # Either responses raised because no mock was registered (URL not
    # called → good), or it succeeded from cache. Either way size is 1.
    assert n == 1
    assert sanctions.contains("TOnlyFromCache") is True


# ---------------------------------------------------------------------------
# status()
# ---------------------------------------------------------------------------


def test_status_unloaded():
    sanctions._override_for_tests(None)
    s = sanctions.status()
    assert s == {"loaded": False, "loaded_from": "not loaded", "size": 0}


def test_status_loaded():
    sanctions._override_for_tests({"TX", "TY", "TZ"})
    s = sanctions.status()
    assert s["loaded"] is True
    assert s["size"] == 3
    assert s["loaded_from"] == "test-override"
