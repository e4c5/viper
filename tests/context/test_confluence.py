"""Tests for ConfluenceFetcher (mocked HTTP)."""

from unittest.mock import MagicMock, patch

from code_review.context.confluence import (
    ConfluenceFetcher,
    _extract_page_id_from_url,
    _extract_space_and_title_from_url,
    _html_to_plain_text,
)


def _make_mock_client(resp_data):
    mock_resp = MagicMock()
    mock_resp.json.return_value = resp_data
    mock_resp.raise_for_status = MagicMock()

    mock_enter = MagicMock()
    mock_enter.get = MagicMock(return_value=mock_resp)

    mock_client = MagicMock()
    mock_client.__enter__ = MagicMock(return_value=mock_enter)
    mock_client.__exit__ = MagicMock(return_value=False)
    return mock_client


@patch("code_review.context.confluence.httpx.Client")
def test_fetch_page_by_id(mock_client_cls):
    page_data = {
        "title": "API Design Guidelines",
        "space": {"name": "Engineering"},
        "body": {"storage": {"value": "<p>Use REST for all APIs.</p>"}},
    }
    mock_client_cls.return_value = _make_mock_client(page_data)

    fetcher = ConfluenceFetcher(
        "https://company.atlassian.net/wiki",
        "user@x.com",
        "token",
    )
    result = fetcher.fetch_page_by_id("12345")

    assert "API Design Guidelines" in result
    assert "Engineering" in result
    assert "Use REST for all APIs." in result


@patch("code_review.context.confluence.httpx.Client")
def test_fetch_page_returns_empty_on_error(mock_client_cls):
    mock_client_cls.side_effect = Exception("timeout")

    fetcher = ConfluenceFetcher("https://company.atlassian.net/wiki", "u", "t")
    result = fetcher.fetch_page_by_id("999")

    assert result == ""


@patch("code_review.context.confluence.httpx.Client")
def test_fetch_page_by_url_with_page_id(mock_client_cls):
    page_data = {
        "title": "Release Process",
        "space": {"name": "DevOps"},
        "body": {"storage": {"value": "<p>Follow the release checklist.</p>"}},
    }
    mock_client_cls.return_value = _make_mock_client(page_data)

    fetcher = ConfluenceFetcher("https://company.atlassian.net/wiki", "u", "t")
    url = "https://company.atlassian.net/wiki/spaces/DEVOPS/pages/77777/Release+Process"
    result = fetcher.fetch_page_by_url(url)

    assert "Release Process" in result


def test_html_to_plain_text_basic():
    html = "<p>Hello <b>world</b></p><p>Second para</p>"
    text = _html_to_plain_text(html)
    assert "Hello" in text
    assert "world" in text
    assert "Second para" in text

def test_html_to_plain_text_entities():
    html = "&lt;script&gt; &amp; &quot;quotes&quot;"
    text = _html_to_plain_text(html)
    assert "<script>" in text
    assert "&" in text
    assert '"quotes"' in text


def test_extract_page_id_from_spaces_url():
    url = "https://company.atlassian.net/wiki/spaces/ENG/pages/12345678/My+Page"
    assert _extract_page_id_from_url(url) == "12345678"


def test_extract_page_id_from_query_param():
    url = "https://company.atlassian.net/wiki/pages/viewpage.action?pageId=9876"
    assert _extract_page_id_from_url(url) == "9876"


def test_extract_page_id_missing():
    assert _extract_page_id_from_url("https://github.com/org/repo") == ""


def test_extract_space_and_title_from_spaces_url():
    url = "https://company.atlassian.net/wiki/spaces/ENG/pages/111/My+Design+Doc"
    space, title = _extract_space_and_title_from_url(url)
    assert space == "ENG"
    assert "My Design Doc" in title


def test_extract_space_and_title_from_display_url():
    url = "https://company.atlassian.net/wiki/display/ENG/My+Page"
    space, title = _extract_space_and_title_from_url(url)
    assert space == "ENG"
    assert "My Page" in title


def test_base_url_normalisation():
    """ConfluenceFetcher should strip /wiki suffix from base URL to avoid double /wiki/wiki."""
    fetcher = ConfluenceFetcher("https://company.atlassian.net/wiki", "u", "t")
    assert not fetcher._base_url.endswith("/wiki")
