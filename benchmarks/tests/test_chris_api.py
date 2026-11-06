"""
Tests for benchmarks.chris_api — the measured Collection+JSON adapter.
"""

import requests

from benchmarks.chris_api import (ChrisApi, ChrisApiError, _items, _next_link,
                                  _template, _total, _value)


# -- fakes -------------------------------------------------------------------------------

class FakeResponse:
    def __init__(self, status=200, payload=None, text=""):
        self.status_code = status
        self._payload = payload
        self.text = text
        self.content = b"x" if payload is not None else b""

    def json(self):
        if self._payload is None:
            raise ValueError("no body")
        return self._payload


class FakeSession:
    """
    Scripted transport: pops one queued item per call; an Exception item is raised.
    """

    def __init__(self, post=(), responses=()):
        self.headers = {}
        self.auth = None
        self.post_queue = list(post)
        self.response_queue = list(responses)
        self.post_calls = []
        self.request_calls = []          # (method, url, kwargs)

    def post(self, url, **kw):
        self.post_calls.append((url, kw))
        item = self.post_queue.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    def request(self, method, url, **kw):
        self.request_calls.append((method, url, kw))
        item = self.response_queue.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def cj(items=(), total=None, next_href=None):
    """
    Build a minimal Collection+JSON payload.
    """
    coll = {"items": [{"href": f"http://x/{d.get('id')}/",
                       "data": [{"name": k, "value": v} for k, v in d.items()]}
                      for d in items]}
    if total is not None:
        coll["total"] = total
    if next_href:
        coll["links"] = [{"rel": "next", "href": next_href}]
    return {"collection": coll}


def make_api(session, *, token="tok", observer=None):
    api = ChrisApi("http://cube/api/v1", "u", "p", observer=observer)
    api.session = session
    if token:
        api._token = token
        session.headers["Authorization"] = f"Token {token}"
    return api


# -- pure helpers ------------------------------------------------------------------------

def test_template_and_value_serialization():
    t = _template({"previous_id": 7, "sleepLength": 1.5, "groupByInstance": True,
                   "prefix": "x_"})
    data = {d["name"]: d["value"] for d in t["template"]["data"]}
    assert data == {"previous_id": "7", "sleepLength": "1.5",
                    "groupByInstance": "true", "prefix": "x_"}
    assert _value(False) == "false"


def test_items_total_next_link_parsing():
    payload = cj([{"id": 1, "status": "created"}], total=42, next_href="http://x/next/")
    items = _items(payload)
    assert items[0]["id"] == 1 and items[0]["_href"] == "http://x/1/"
    assert _total(payload) == 42
    assert _next_link(payload) == "http://x/next/"
    assert _items(None) == [] and _total(None) == 0 and _next_link({}) is None


# -- token authentication ----------------------------------------------------------------

def test_token_fetched_once_and_used_as_header():
    session = FakeSession(post=[FakeResponse(200, {"token": "abc"})],
                          responses=[FakeResponse(200, cj()), FakeResponse(200, cj())])
    api = make_api(session, token=None)
    api.health()
    api.health()
    assert len(session.post_calls) == 1                      # fetched once
    assert session.headers["Authorization"] == "Token abc"
    assert api.auth_mode == "token"
    # token requests carry no per-request basic auth
    assert all(kw["auth"] is None for _, _, kw in session.request_calls)


def test_token_connection_error_falls_back_to_basic_and_retries():
    session = FakeSession(
        post=[requests.ConnectionError("down"), FakeResponse(200, {"token": "abc"})],
        responses=[FakeResponse(200, cj()), FakeResponse(200, cj())])
    api = make_api(session, token=None)
    api.health()
    assert api.auth_mode == "basic"
    assert session.request_calls[0][2]["auth"] == ("u", "p")  # basic for this call
    api.health()                                              # retried and succeeded
    assert api.auth_mode == "token"
    assert len(session.post_calls) == 2


def test_token_refused_stops_asking():
    session = FakeSession(post=[FakeResponse(401, text="nope")],
                          responses=[FakeResponse(200, cj()), FakeResponse(200, cj())])
    api = make_api(session, token=None)
    api.health()
    api.health()
    assert len(session.post_calls) == 1                      # 4xx -> never re-asked
    assert api.auth_mode == "basic"


# -- measured call -----------------------------------------------------------------------

def test_call_records_error_status_and_observer():
    seen = []
    session = FakeSession(responses=[FakeResponse(500, text="boom")])
    api = make_api(session, observer=seen.append)
    r = api._call("create", "POST", "http://cube/api/v1/x/", body={"a": 1})
    assert not r.ok and r.status_code == 500 and "HTTP 500" in r.error
    assert seen[0].endpoint_class == "create" and seen[0].status_code == 500


def test_call_timeout_is_status_zero():
    seen = []
    session = FakeSession(responses=[requests.Timeout("too slow")])
    api = make_api(session, observer=seen.append)
    r = api._call("list", "GET", "http://cube/api/v1/x/")
    assert not r.ok and r.status_code == 0 and "too slow" in r.error
    assert seen[0].status_code == 0


def test_create_plugin_instance_raises_on_error():
    session = FakeSession(responses=[FakeResponse(400, text="bad param")])
    api = make_api(session)
    try:
        api.create_plugin_instance(5, {"x": 1})
        raise AssertionError("expected ChrisApiError")
    except ChrisApiError as exc:
        assert exc.result.status_code == 400


def test_get_feed_instances_follows_pagination():
    page1 = cj([{"id": 1, "status": "started"}], next_href="http://cube/api/v1/p2/")
    page2 = cj([{"id": 2, "status": "created"}])
    session = FakeSession(responses=[FakeResponse(200, page1), FakeResponse(200, page2)])
    api = make_api(session)
    items = api.get_feed_instances(9)
    assert [i["id"] for i in items] == [1, 2]
    # the next-page request reuses the href (params only on the first request)
    assert session.request_calls[1][1] == "http://cube/api/v1/p2/"
    assert session.request_calls[1][2]["params"] is None


# -- plugin resolution -------------------------------------------------------------------

def test_get_plugin_selects_first_match_and_counts_versions():
    # two installed versions; CUBE returns them -version-ordered, harness takes the first
    payload = cj([
        {"id": 7, "name": "pl-x", "version": "1.0.13", "dock_image": "fnndsc/pl-x"},
        {"id": 6, "name": "pl-x", "version": "1.0.12", "dock_image": "fnndsc/pl-x"},
    ])
    api = make_api(FakeSession(responses=[FakeResponse(200, payload)]))
    assert api.get_plugin("pl-x") == {
        "id": 7, "name": "pl-x", "version": "1.0.13",
        "dock_image": "fnndsc/pl-x", "matches": 2,
        "installed_versions": ["1.0.13", "1.0.12"]}


def test_get_plugin_single_match_and_id_delegates():
    payload = cj([{"id": 5, "name": "pl-y", "version": "2.1.5",
                   "dock_image": "fnndsc/pl-y"}])
    api = make_api(FakeSession(responses=[FakeResponse(200, payload),
                                          FakeResponse(200, payload)]))
    assert api.get_plugin("pl-y")["matches"] == 1
    assert api.get_plugin_id("pl-y") == 5          # delegates to get_plugin


def test_get_plugin_not_found_raises():
    api = make_api(FakeSession(responses=[FakeResponse(200, cj([]))]))
    try:
        api.get_plugin("nope")
        raise AssertionError("expected ChrisApiError")
    except ChrisApiError as exc:
        assert "not found" in (exc.result.error or "")


def test_get_plugin_missing_optional_fields_are_none():
    api = make_api(FakeSession(
        responses=[FakeResponse(200, cj([{"id": 1, "name": "pl-z"}]))]))
    p = api.get_plugin("pl-z")
    assert p["version"] is None and p["dock_image"] is None and p["matches"] == 1
