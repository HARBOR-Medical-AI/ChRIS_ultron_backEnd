"""
Thin, measured Collection+JSON HTTP adapter for CUBE.

This is the harness's transport layer and the seam a future Locust layer would share.
Every call is timed and tagged by endpoint class and a ``RequestRecord`` is emitted to
an injected observer (default no-op) — the client library swallows latency and HTTP
status codes which the benchmark must capture. No orchestration logic lives here.

Authentication: a DRF token is obtained once from ``auth-token/`` and sent as a header
on every subsequent request. Per-request HTTP Basic would make CUBE run its
deliberately-slow password hasher on *every* call — ~100 ms of artificial latency in
every measurement, plus SUT CPU load that scales with the harness's own traffic
(including poll traffic, which grows with the feeds axis). A token endpoint that
refuses makes the adapter fall back to Basic and report it via ``auth_mode`` so the
run's environment manifest records the degraded measurement mode.

URL patterns (verified in ``core/api.py``), relative to the ``/api/v1/`` base:
  * obtain auth token    POST   auth-token/
  * create instance      POST   plugins/<id>/instances/
  * feed instance search GET    plugins/instances/search/?feed_id=<id>
  * feed detail (delete) DELETE <feed_id>/
  * plugins search       GET    plugins/search/?name_exact=<name>
"""

from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Optional

import requests
from requests.adapters import HTTPAdapter

from .models import RequestRecord


CJ = "application/vnd.collection+json"
Observer = Callable[[RequestRecord], None]


class ChrisApiError(Exception):
    def __init__(self, result: "HttpResult"):
        self.result = result
        super().__init__(result.error or f"HTTP {result.status_code}")


@dataclass
class HttpResult:
    ok: bool
    status_code: int
    latency_ms: float
    payload: Optional[dict]
    items: list[dict] = field(default_factory=list)
    error: Optional[str] = None


def _template(data: dict) -> dict:
    """
    Wrap a flat dict as a Collection+JSON write template.
    """
    return {"template": {"data": [{"name": k, "value": _value(v)}
                                  for k, v in data.items()]}}


def _value(v):
    if isinstance(v, str):
        return v
    if isinstance(v, bool):
        return "true" if v else "false"   # CUBE/JSON expects lowercase booleans
    return str(v)


def _total(payload: Optional[dict]) -> int:
    """
    Read the non-standard ``total`` count CUBE adds to a Collection+JSON list.
    """
    return int(((payload or {}).get("collection", {}) or {}).get("total", 0) or 0)


def _items(payload: Optional[dict]) -> list[dict]:
    coll = (payload or {}).get("collection", {})
    out = []

    for item in coll.get("items", []):
        d = {desc["name"]: desc.get("value") for desc in item.get("data", [])}
        d["_href"] = item.get("href")
        out.append(d)
    return out


def _next_link(payload: Optional[dict]) -> Optional[str]:
    coll = (payload or {}).get("collection", {})

    for link in coll.get("links", []):
        if link.get("rel") == "next":
            return link.get("href")
    return None


class ChrisApi:
    def __init__(self, base_url: str, username: str, password: str, *,
                 timeout: float = 30.0, observer: Optional[Observer] = None,
                 pool_maxsize: int = 128):
        self.base = base_url.rstrip("/") + "/"
        self.timeout = timeout
        self._observe = observer or (lambda r: None)
        self._username = username
        self._password = password
        self.session = requests.Session()

        # the feeds axis fans out one thread per feed against this single host; urllib3's
        # default pool keeps 10 connections and discards any beyond that so the reconnect
        # churn shows up as fake latency at exactly the high-concurrency levels
        adapter = HTTPAdapter(pool_connections=4, pool_maxsize=pool_maxsize)

        self.session.mount("http://", adapter)
        self.session.mount("https://", adapter)
        self._token: Optional[str] = None
        self._token_refused = False
        self._auth_lock = threading.Lock()

    @property
    def auth_mode(self) -> str:
        """
        ``token`` once a DRF token is held; ``basic`` while falling back.
        """
        return "token" if self._token else "basic"

    # -- authentication ---------------------------------------------------------------

    def _ensure_token(self) -> None:
        """
        Fetch the DRF token once (thread-safe). Connection errors are retried on the
        next call (the stack may still be starting); a 4xx means token auth is refused,
        so stop asking and stay on Basic for the rest of the run.
        """
        if self._token or self._token_refused:
            return
        
        with self._auth_lock:
            if self._token or self._token_refused:
                return
            
            t0 = time.perf_counter()
            status, error = 0, None
            
            try:
                resp = self.session.post(self.base + "auth-token/",
                                         json={"username": self._username,
                                               "password": self._password},
                                         timeout=self.timeout)
                status = resp.status_code

                if 200 <= status < 300:
                    self._token = resp.json()["token"]
                    self.session.headers["Authorization"] = f"Token {self._token}"
                else:
                    error = f"HTTP {status}: {resp.text[:200]}"
                    if 400 <= status < 500:
                        self._token_refused = True
            except requests.RequestException as exc:
                error = str(exc)

            self._observe(RequestRecord("auth", "POST", status,
                                        (time.perf_counter() - t0) * 1000.0,
                                        self._token is not None, time.time(), error))

    # -- core measured request -------------------------------------------------------

    def _call(self, endpoint_class: str, method: str, url: str, *,
              body: Optional[dict] = None, params: Optional[dict] = None) -> HttpResult:
        self._ensure_token()
        headers = {"Accept": CJ}
        data = None

        if body is not None:
            headers["Content-Type"] = CJ
            data = json.dumps(_template(body))

        t0 = time.perf_counter()
        status, error, payload = 0, None, None

        try:
            resp = self.session.request(method, url, data=data, params=params,
                                        headers=headers, timeout=self.timeout,
                                        auth=(None if self._token else
                                              (self._username, self._password)))
            status = resp.status_code
            ok = 200 <= status < 300

            if not ok:
                error = f"HTTP {status}: {resp.text[:200]}"
            elif resp.content:
                payload = resp.json()
        except requests.RequestException as exc:
            ok, status, error = False, 0, str(exc)

        latency_ms = (time.perf_counter() - t0) * 1000.0
        self._observe(RequestRecord(endpoint_class, method, status, latency_ms, ok,
                                    time.time(), error))
        return HttpResult(ok, status, latency_ms, payload, _items(payload), error)

    # -- public API ------------------------------------------------------------------

    def health(self) -> bool:
        return self._call("health", "GET", self.base + "users/").ok

    def feed_count(self) -> int:
        return _total(self._call("list", "GET", self.base, params={"limit": 1}).payload)

    def instance_count(self) -> int:
        return _total(self._call("list", "GET", self.base + "plugins/instances/",
                                 params={"limit": 1}).payload)

    def file_count(self) -> int:
        return _total(self._call("list", "GET", self.base + "userfiles/",
                                 params={"limit": 1}).payload)

    def get_plugin(self, name: str) -> dict:
        """
        Resolve a plugin by name and return the selected version's provenance.

        Searches ``plugins/search/?name_exact=`` and selects the first match — CUBE
        orders plugins by ``('meta', '-version')`` so this is the highest-sorting
        version *string* for the name. ``matches`` is how many versions the name
        resolved to and ``installed_versions`` lists them: the runner starts only when
        each workload plugin has exactly one since CUBE itself resolves
        ``pl-topologicalcopy`` by name too.
        """
        r = self._call("list", "GET", self.base + "plugins/search/",
                       params={"name_exact": name})

        if not r.ok or not r.items:
            raise ChrisApiError(r if not r.ok else HttpResult(
                False, 404, r.latency_ms, None, [], f"plugin not found: {name}"))

        sel = r.items[0]
        return {"id": int(sel["id"]), "name": sel.get("name", name),
                "version": sel.get("version"), "dock_image": sel.get("dock_image"),
                "matches": len(r.items),
                "installed_versions": [item.get("version") for item in r.items]}

    def get_plugin_id(self, name: str) -> int:
        return self.get_plugin(name)["id"]

    def create_plugin_instance(self, plugin_id: int, params: dict) -> dict:
        url = self.base + f"plugins/{plugin_id}/instances/"
        r = self._call("create", "POST", url, body=params)

        if not r.ok or not r.items:
            raise ChrisApiError(r)
        return r.items[0]

    def get_feed_instances(self, feed_id: int, *, limit: int = 200, **filters) -> list[dict]:
        """
        All plugin instances of a feed (follows pagination). Tagged as ``poll``.
        """
        params = {"feed_id": feed_id, "limit": limit, **filters}
        url = self.base + "plugins/instances/search/"
        items: list[dict] = []

        while url:
            r = self._call("poll", "GET", url, params=params)
            params = None  # subsequent 'next' hrefs already carry the query
            
            if not r.ok:
                break
            
            items.extend(r.items)
            url = _next_link(r.payload)
        return items

    def delete_feed(self, feed_id: int) -> bool:
        return self._call("delete", "DELETE", self.base + f"{feed_id}/").ok
