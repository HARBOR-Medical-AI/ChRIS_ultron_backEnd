"""
Tests for benchmarks.docker_client — the image record of a workload plugin, and the
stats reads and samples of the resource sampler.
"""

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from docker.errors import APIError, ImageNotFound, NotFound

from benchmarks.docker_client import (JOB_LABEL, ContainerRef, DockerClient, _rfc3339,
                                      counters_went_back, image_record, resource_sample,
                                      short_window)
from benchmarks.tests._frames import frame


def _client(docker):
    client = DockerClient.__new__(DockerClient)          # no Docker connection needed
    client._docker = docker
    return client


def _images(get):
    return _client(SimpleNamespace(images=SimpleNamespace(get=get)))


def _raise(error):
    def get(ref):
        raise error
    return get


def test_image_record_sorts_the_digests_and_keeps_the_architecture():
    image = SimpleNamespace(id="sha256:1c45", attrs={
        "RepoDigests": ["localhost/x@sha256:bb", "fnndsc/x@sha256:aa"],
        "Architecture": "amd64"})

    assert image_record(image) == {  # nosec B101 - pytest assertion
        "image_id": "sha256:1c45",
        "repo_digests": ["fnndsc/x@sha256:aa", "localhost/x@sha256:bb"],
        "architecture": "amd64"}


def test_image_record_of_an_image_without_digests():
    # a local build on overlay2 (on containerd it has a digest of its own)
    record = image_record(SimpleNamespace(id="sha256:e2f4", attrs={}))

    assert record["repo_digests"] == []  # nosec B101 - pytest assertion
    assert record["architecture"] is None  # nosec B101 - pytest assertion


def test_image_info_is_none_without_docker_a_reference_or_the_image():
    client = _images(_raise(ImageNotFound("no such image")))

    assert _client(None).image_info("fnndsc/x") is None  # nosec B101 - pytest assertion
    assert client.image_info(None) is None  # nosec B101 - pytest assertion
    assert client.image_info("fnndsc/x") is None  # nosec B101 - pytest assertion


def test_image_info_reports_an_error_rather_than_an_absent_image():
    # a Docker API failure is not "not present": the caller says what went wrong
    info = _images(_raise(APIError("500 Server Error"))).image_info("fnndsc/x")

    assert "500 Server Error" in info["error"]  # nosec B101 - pytest assertion


def test_image_info_records_the_image_a_reference_resolves_to():
    image = SimpleNamespace(id="sha256:3329", attrs={"RepoDigests": ["fnndsc/x@sha256:35"]})
    client = _images({"fnndsc/x@sha256:35": image}.get)
    record = client.image_info("fnndsc/x@sha256:35")

    assert record["image_id"] == "sha256:3329"  # nosec B101 - pytest assertion


# -- stats reads and samples ---------------------------------------------------------------

SERVICE = ContainerRef("c1", "db", "service", started_at=100.0)


def test_a_services_first_reading_has_no_cpu_window():
    sample = resource_sample(SERVICE, frame(200.0, 5e9, written=4096), None, since=150.0)

    assert (sample.cpu_pct, sample.cpu_window_s) == (None, None)  # nosec B101 - pytest assertion
    assert sample.t == pytest.approx(200.0)  # nosec B101 - pytest assertion
    assert sample.cpu_total_ns == 5_000_000_000  # nosec B101 - pytest assertion
    assert (sample.container, sample.kind, sample.started_at) == (  # nosec B101 - pytest assertion
        "db", "service", 100.0)
    measured = (sample.mem_bytes, sample.mem_anon_bytes, sample.blkio_write_bytes,
                sample.pids)
    assert measured == (100, 50, 4096, 3)  # nosec B101 - pytest assertion


def test_cpu_is_over_the_time_since_the_previous_reading():
    one_core = resource_sample(SERVICE, frame(201.0, 6e9), frame(200.0, 5e9))
    half = resource_sample(SERVICE, frame(203.0, 7e9), frame(201.0, 6e9))

    assert (one_core.cpu_pct, one_core.cpu_window_s) == (  # nosec B101 - pytest assertion
        100.0, 1.0)
    assert (half.cpu_pct, half.cpu_window_s) == (50.0, 2.0)  # nosec B101 - pytest assertion

    # the host's CPU time, which some engines leave out, is not needed
    bare = resource_sample(SERVICE, frame(201.0, 6e9, system=False),
                           frame(200.0, 5e9, system=False))
    assert bare.cpu_pct == 100.0  # nosec B101 - pytest assertion


def test_a_container_born_while_sampling_gets_its_cpu_since_it_started():
    job = ContainerRef("j1", "chris-jid-7", "job", started_at=201.0)
    sample = resource_sample(job, frame(202.5, 0.75e9), None, since=200.0)

    assert (sample.cpu_pct, sample.cpu_window_s) == (50.0, 1.5)  # nosec B101 - pytest assertion

    # one that started before the sampling began has no window of its own yet
    earlier = resource_sample(job, frame(202.5, 0.75e9), None, since=201.5)
    assert earlier.cpu_pct is None  # nosec B101 - pytest assertion


def test_a_window_too_short_gives_no_cpu():
    # the closing tick of a scenario just after a regular one, or a job just started
    closing = resource_sample(SERVICE, frame(200.7, 5.7e9), frame(200.0, 5e9))
    job = ContainerRef("j1", "chris-jid-7", "job", started_at=200.0)
    young = resource_sample(job, frame(200.2, 0.2e9), None, since=150.0)

    assert (closing.cpu_pct, young.cpu_pct) == (None, None)  # nosec B101 - pytest assertion
    assert closing.cpu_total_ns == 5_700_000_000  # nosec B101 - pytest assertion

    # a job's first reading needs less of a window than one between two readings
    started = resource_sample(job, frame(200.3, 0.3e9), None, since=150.0)
    assert started.cpu_pct == 100.0  # nosec B101 - pytest assertion


def test_counters_that_went_backwards_give_no_cpu_or_throttling():
    # a restart in place, which the sampler meets by inspecting the start time again
    before, after = frame(200.0, 5e9, throttled_ns=2e9), frame(201.0, 1e9, throttled_ns=0)
    sample = resource_sample(SERVICE, after, before)

    assert (sample.cpu_pct, sample.cpu_throttled_s) == (None, None)  # nosec B101 - pytest assertion
    assert counters_went_back(after, before)  # nosec B101 - pytest assertion
    assert not counters_went_back(before, frame(199.0, 4e9))  # nosec B101 - pytest assertion


def test_only_a_window_too_short_keeps_its_start_for_the_next_reading():
    job = ContainerRef("j1", "chris-jid-7", "job", started_at=200.0)

    assert short_window(frame(200.7, 5.7e9), frame(200.0, 5e9), None, None)  # nosec B101 - pytest assertion
    assert short_window(frame(200.2, 0.2e9), None, job.started_at, 150.0)  # nosec B101 - pytest assertion
    # a figure, no window of its own, or counters that went backwards
    assert not short_window(frame(201.0, 6e9), frame(200.0, 5e9), None, None)  # nosec B101 - pytest assertion
    assert not short_window(frame(201.0, 6e9), None, None, None)  # nosec B101 - pytest assertion
    assert not short_window(frame(201.0, 1e9), frame(200.0, 5e9), None, None)  # nosec B101 - pytest assertion


def test_throttling_is_over_the_window_just_before_the_reading_when_given():
    # the CPU over the 1.7 s since the reading at 201.3, the throttling since 202.0's
    sample = resource_sample(SERVICE, frame(203.0, 6.4e9, throttled_ns=0.5e9),
                             frame(201.3, 5e9, throttled_ns=0.1e9),
                             last=frame(202.0, 6.4e9, throttled_ns=0.3e9))

    assert (sample.cpu_pct, sample.cpu_window_s) == (82.35, 1.7)  # nosec B101 - pytest assertion
    assert sample.cpu_throttled_s == 0.2  # nosec B101 - pytest assertion


def test_throttling_is_the_time_the_limit_held_the_container_back_in_the_window():
    sample = resource_sample(SERVICE, frame(201.0, 5.5e9, throttled_ns=3.5e9),
                             frame(200.0, 5e9, throttled_ns=3e9))

    assert sample.cpu_throttled_s == 0.5  # nosec B101 - pytest assertion
    first = resource_sample(SERVICE, frame(201.0, 6e9), None)
    assert first.cpu_throttled_s is None  # nosec B101 - pytest assertion


def test_docker_timestamps_read_as_epoch_seconds():
    expected = datetime(2026, 10, 26, 17, 12, 35, tzinfo=timezone.utc).timestamp()

    read = _rfc3339("2026-10-26T17:12:35.429402335Z")
    assert read == pytest.approx(expected + 0.429402335, abs=1e-6)  # nosec B101 - pytest assertion
    assert _rfc3339("2026-10-26T13:12:35-04:00") == expected  # nosec B101 - pytest assertion
    for value in ("0001-01-01T00:00:00Z", None, "", "yesterday"):
        assert _rfc3339(value) is None  # nosec B101 - pytest assertion


def _stats_client(stats):
    return _client(SimpleNamespace(api=SimpleNamespace(stats=stats)))


def _failing(error):
    def stats(container_id, **kwargs):
        raise error
    return stats


def test_read_stats_tells_a_vanished_container_from_a_failed_read():
    calls = []

    def stats(container_id, **kwargs):
        calls.append((container_id, kwargs))
        return frame(200.0, 1e9)

    read = _stats_client(stats).read_stats("c1")
    exited = _stats_client(lambda cid, **kw: {"read": "0001-01-01T00:00:00Z"}).read_stats("c1")
    removed = _stats_client(_failing(NotFound("no such container"))).read_stats("c1")
    failed = _stats_client(_failing(APIError("500 Server Error"))).read_stats("c1")
    # Podman's answer for a container that is no longer running
    stopped = _stats_client(_failing(APIError(
        "409 Conflict", response=SimpleNamespace(status_code=409)))).read_stats("c1")

    assert calls == [("c1", {"stream": False, "one_shot": True})]  # nosec B101 - pytest assertion
    assert read.frame is not None and not read.vanished  # nosec B101 - pytest assertion
    assert exited.vanished and removed.vanished and stopped.vanished  # nosec B101 - pytest assertion
    assert (exited.frame, removed.frame, stopped.frame) == (None, None, None)  # nosec B101 - pytest assertion
    assert failed.frame is None and not failed.vanished  # nosec B101 - pytest assertion
    assert "500 Server Error" in failed.error  # nosec B101 - pytest assertion


class ListingApi:
    """
    The low-level API the listing and the inspect use, answering from canned containers.
    """

    def __init__(self, by_filter, states=None):
        self._by_filter = by_filter
        self._states = states or {}
        self.filters = []

    def containers(self, filters):
        self.filters.append(filters)
        return self._by_filter.get(filters["label"], [])

    def inspect_container(self, container_id):
        if container_id not in self._states:
            raise NotFound("no such container")
        return {"State": {"StartedAt": self._states[container_id]}}


def _listed(cid, name, labels):
    return {"Id": cid, "Names": [f"/{name}"], "Labels": labels}


def test_sampled_containers_names_each_once_and_leaves_out_the_excluded():
    project = "com.docker.compose.project=chris_ultron_backend"
    compose = {"com.docker.compose.project": "chris_ultron_backend"}
    db = _listed("c1", "chris_ultron_backend-db-1", {**compose,
                                                     "com.docker.compose.service": "db"})
    bench = _listed("c2", "chris_ultron_backend-benchmark-run-1",
                    {**compose, "com.docker.compose.service": "benchmark"})
    # a job container whose image carries compose labels is in both listings
    job = _listed("j1", "chris-jid-7", {**compose, "com.docker.compose.service": "benchmark",
                                        "org.chrisproject.miniChRIS": "plugininstance"})
    api = ListingApi({project: [db, bench, job], JOB_LABEL: [job]})
    client = _client(SimpleNamespace(api=api))
    client.project, client.job_label = "chris_ultron_backend", JOB_LABEL
    client.exclude_services = ("benchmark",)

    assert client.sampled_containers() == [  # nosec B101 - pytest assertion
        ContainerRef("c1", "db", "service", None),
        ContainerRef("j1", "chris-jid-7", "job", None)]
    filters = [{"label": project}, {"label": JOB_LABEL}]
    assert api.filters == filters  # nosec B101 - pytest assertion
    assert _client(None).sampled_containers() == []  # nosec B101 - pytest assertion


def test_sampled_containers_without_a_project_take_every_compose_service():
    service = "com.docker.compose.service"
    db = _listed("c1", "other-db-1", {service: "db"})
    api = ListingApi({service: [db]})
    client = _client(SimpleNamespace(api=api))
    client.project, client.job_label, client.exclude_services = None, JOB_LABEL, ()

    assert client.sampled_containers() == [ContainerRef("c1", "db", "service", None)]  # nosec B101 - pytest assertion
    assert api.filters == [{"label": service}, {"label": JOB_LABEL}]  # nosec B101 - pytest assertion


def test_started_at_is_none_when_the_inspect_fails():
    api = ListingApi({}, states={"c1": "2026-10-26T17:12:35Z"})
    client = _client(SimpleNamespace(api=api))

    expected = _rfc3339("2026-10-26T17:12:35Z")
    assert client.started_at("c1") == expected  # nosec B101 - pytest assertion
    assert client.started_at("gone") is None  # nosec B101 - pytest assertion


def test_the_listings_for_others_leave_out_a_container_removed_meanwhile():
    calls = []
    containers = SimpleNamespace(list=lambda **kwargs: calls.append(kwargs) or [])
    client = _client(SimpleNamespace(containers=containers))
    client.project, client.job_label, client.exclude_services = "p", JOB_LABEL, ()

    client.service_containers()
    client.job_containers()

    assert len(calls) == 2  # nosec B101 - pytest assertion
    assert all(call["ignore_removed"] for call in calls)  # nosec B101 - pytest assertion


def test_the_timeout_reaches_the_docker_client(monkeypatch):
    seen = []
    fake = SimpleNamespace(ping=lambda: True)
    monkeypatch.setattr("benchmarks.docker_client.docker.from_env",
                        lambda **kwargs: seen.append(kwargs) or fake)

    DockerClient(timeout=10)
    DockerClient()

    assert seen == [{"timeout": 10}, {}]  # nosec B101 - pytest assertion
