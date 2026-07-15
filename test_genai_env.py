import pytest
from google import genai


def test_uses_google_api_key(monkeypatch):
    monkeypatch.setenv("GOOGLE_API_KEY", "fake-google-key")
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)

    client = genai.Client()
    assert client._api_client.api_key == "fake-google-key"


def test_uses_gemini_api_key(monkeypatch):
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    monkeypatch.setenv("GEMINI_API_KEY", "fake-gemini-key")

    client = genai.Client()
    assert client._api_client.api_key == "fake-gemini-key"


def test_fails_without_api_key(monkeypatch):
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)

    with pytest.raises(ValueError):
        genai.Client()
