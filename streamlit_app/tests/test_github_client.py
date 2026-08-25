import os
import sys
from unittest.mock import MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
import github_client
from github_client import GitHubClient, GitHubActionsError


def _client():
    return GitHubClient(token="tok", owner="acme", repo="outreach")


def _fake_response(status_code, json_data=None, text="", content=b"x"):
    resp = MagicMock()
    resp.status_code = status_code
    resp.text = text
    resp.content = content
    resp.json.return_value = json_data or {}
    return resp


# ---------- dispatch_workflow ----------

def test_dispatch_workflow_returns_run_details_on_200(monkeypatch):
    resp = _fake_response(200, {"id": 42, "html_url": "https://github.com/acme/outreach/actions/runs/42"})
    monkeypatch.setattr(github_client.requests, "post", lambda *a, **kw: resp)

    result = _client().dispatch_workflow("send_batch.yml", {"campaign": "Foo"})
    assert result == {"id": 42, "html_url": "https://github.com/acme/outreach/actions/runs/42"}


def test_dispatch_workflow_returns_none_on_204(monkeypatch):
    resp = _fake_response(204, content=b"")
    monkeypatch.setattr(github_client.requests, "post", lambda *a, **kw: resp)

    result = _client().dispatch_workflow("send_batch.yml", {"campaign": "Foo"})
    assert result is None


def test_dispatch_workflow_raises_on_error_status(monkeypatch):
    resp = _fake_response(422, text="Invalid inputs")
    monkeypatch.setattr(github_client.requests, "post", lambda *a, **kw: resp)

    with pytest.raises(GitHubActionsError, match="422"):
        _client().dispatch_workflow("send_batch.yml", {"campaign": "Foo"})


def test_dispatch_workflow_sends_return_run_details_flag(monkeypatch):
    captured = {}

    def fake_post(url, json=None, headers=None, timeout=None):
        captured["json"] = json
        return _fake_response(204, content=b"")

    monkeypatch.setattr(github_client.requests, "post", fake_post)
    _client().dispatch_workflow("send_batch.yml", {"campaign": "Foo"})
    assert captured["json"]["return_run_details"] is True
    assert captured["json"]["inputs"] == {"campaign": "Foo"}


# ---------- get_run ----------

def test_get_run_returns_json_on_200(monkeypatch):
    resp = _fake_response(200, {"status": "completed", "conclusion": "success"})
    monkeypatch.setattr(github_client.requests, "get", lambda *a, **kw: resp)

    run = _client().get_run(42)
    assert run["status"] == "completed"


def test_get_run_raises_on_404(monkeypatch):
    resp = _fake_response(404, text="Not Found")
    monkeypatch.setattr(github_client.requests, "get", lambda *a, **kw: resp)

    with pytest.raises(GitHubActionsError, match="404"):
        _client().get_run(999)


# ---------- find_recent_run ----------

def test_find_recent_run_returns_first_run(monkeypatch):
    resp = _fake_response(200, {"workflow_runs": [{"id": 7}, {"id": 6}]})
    monkeypatch.setattr(github_client.requests, "get", lambda *a, **kw: resp)

    run = _client().find_recent_run("send_batch.yml")
    assert run == {"id": 7}


def test_find_recent_run_returns_none_when_empty(monkeypatch):
    resp = _fake_response(200, {"workflow_runs": []})
    monkeypatch.setattr(github_client.requests, "get", lambda *a, **kw: resp)

    assert _client().find_recent_run("send_batch.yml") is None


# ---------- campaign PR flow ----------

def test_open_campaign_pull_request_calls_in_order(monkeypatch):
    calls = []

    def fake_get(url, headers=None, timeout=None, params=None):
        calls.append(("GET", url))
        return _fake_response(200, {"object": {"sha": "abc123"}})

    def fake_post(url, json=None, headers=None, timeout=None):
        calls.append(("POST", url, json))
        if url.endswith("/git/refs"):
            return _fake_response(201, {"ref": "refs/heads/add-campaign-foo"})
        if url.endswith("/pulls"):
            return _fake_response(201, {"html_url": "https://github.com/acme/outreach/pull/1"})
        return _fake_response(201, {})

    def fake_put(url, json=None, headers=None, timeout=None):
        calls.append(("PUT", url, json))
        return _fake_response(201, {})

    monkeypatch.setattr(github_client.requests, "get", fake_get)
    monkeypatch.setattr(github_client.requests, "post", fake_post)
    monkeypatch.setattr(github_client.requests, "put", fake_put)

    client = _client()
    result = client.open_campaign_pull_request(
        branch_name="add-campaign-foo",
        files=[{"path": "templates/Foo/intro_A.txt", "content": b"Subject: Hi\n\nBody"}],
        pr_title="Add campaign: Foo",
        pr_body="body",
    )

    assert result["html_url"] == "https://github.com/acme/outreach/pull/1"
    # Branch created before file written, file written before PR opened.
    kinds = [c[0] for c in calls]
    assert kinds.index("POST") < len(calls)  # sanity: at least one POST happened
    ref_call_idx = next(i for i, c in enumerate(calls) if c[0] == "POST" and c[1].endswith("/git/refs"))
    put_call_idx = next(i for i, c in enumerate(calls) if c[0] == "PUT")
    pr_call_idx = next(i for i, c in enumerate(calls) if c[0] == "POST" and c[1].endswith("/pulls"))
    assert ref_call_idx < put_call_idx < pr_call_idx


def test_create_branch_raises_if_ref_lookup_fails(monkeypatch):
    monkeypatch.setattr(github_client.requests, "get", lambda *a, **kw: _fake_response(404, text="no ref"))
    with pytest.raises(GitHubActionsError, match="404"):
        _client().create_branch("add-campaign-foo")


def test_create_file_encodes_content_as_base64(monkeypatch):
    captured = {}

    def fake_put(url, json=None, headers=None, timeout=None):
        captured["json"] = json
        return _fake_response(201, {})

    monkeypatch.setattr(github_client.requests, "put", fake_put)
    _client().create_file("templates/Foo/intro_A.txt", b"Subject: Hi\n\nBody", "msg", "branch")

    import base64
    assert base64.b64decode(captured["json"]["content"]) == b"Subject: Hi\n\nBody"
