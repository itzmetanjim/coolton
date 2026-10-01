from unittest.mock import Mock

import pytest

from agent.bucky_client import BUCKY_URL, upload_to_bucky


def test_upload_sends_the_file_field_and_returns_buckys_url(monkeypatch):
    sent = {}

    def fake_post(url, files, timeout):
        sent.update(url=url, files=files)
        return Mock(text="https://imgutil.s3.us-east-2.amazonaws.com/abc/report.csv\n", raise_for_status=lambda: None)

    monkeypatch.setattr("agent.bucky_client.requests.post", fake_post)
    assert upload_to_bucky(b"a,b\n1,2\n", "report.csv", "text/csv") == "https://imgutil.s3.us-east-2.amazonaws.com/abc/report.csv"
    assert sent == {"url": BUCKY_URL, "files": {"file": ("report.csv", b"a,b\n1,2\n", "text/csv")}}


def test_a_non_url_reply_is_an_error(monkeypatch):
    monkeypatch.setattr("agent.bucky_client.requests.post",
                        lambda *a, **k: Mock(text="Usage: POST a multipart form ...", raise_for_status=lambda: None))
    with pytest.raises(RuntimeError, match="Bucky upload failed"):
        upload_to_bucky(b"x", "x.txt")
