"""Resource usage: the arithmetic, the ring buffer, and the endpoint.

The arithmetic is the part that can be quietly wrong — a CPU percentage that
ignores core count is off by a factor of eight on the wrong machine, and a
memory figure that counts page cache reports an idle app as nearly full. Both
are tested against payloads shaped like Docker's, because the alternative is a
number that looks plausible and isn't.
"""

import pytest

from hangar import config, metrics, store
from hangar.metrics import History, Sample, read_sample
from hangar.store import App, AppStatus

MB = 1024 * 1024


def stats(
    *,
    cpu=0,
    precpu=0,
    system=0,
    presystem=0,
    cores=1,
    usage=0,
    limit=512 * MB,
    inactive_file=0,
) -> dict:
    """A Docker stats payload, shaped like the real one."""
    return {
        "cpu_stats": {
            "cpu_usage": {"total_usage": cpu},
            "system_cpu_usage": system,
            "online_cpus": cores,
        },
        "precpu_stats": {
            "cpu_usage": {"total_usage": precpu},
            "system_cpu_usage": presystem,
        },
        "memory_stats": {
            "usage": usage,
            "limit": limit,
            "stats": {"inactive_file": inactive_file},
        },
    }


# --------------------------------------------------------------------------
# CPU
# --------------------------------------------------------------------------


def test_a_fully_busy_single_core_reads_as_100_percent():
    sample = read_sample(stats(cpu=100, precpu=0, system=100, presystem=0, cores=1))
    assert sample.cpu_percent == 100.0


def test_cpu_percent_is_scaled_by_core_count():
    """Docker's delta is a fraction of *all* cores; without this it reads low.

    One core fully busy on an 8-core box is 1/8 of the system delta, which
    without the multiplier would be reported as 12.5% rather than 100%.
    """
    sample = read_sample(stats(cpu=100, precpu=0, system=800, presystem=0, cores=8))
    assert sample.cpu_percent == 100.0


def test_an_idle_app_reads_as_zero():
    sample = read_sample(stats(cpu=50, precpu=50, system=1000, presystem=0))
    assert sample.cpu_percent == 0.0


def test_the_first_reading_has_no_previous_sample_to_diff():
    """Docker reports zeroed precpu_stats on a just-started container."""
    sample = read_sample(stats(cpu=500, precpu=0, system=0, presystem=0))
    assert sample.cpu_percent == 0.0


def test_a_counter_reset_does_not_report_a_negative():
    sample = read_sample(stats(cpu=10, precpu=100, system=200, presystem=0))
    assert sample.cpu_percent == 0.0


def test_an_incomplete_payload_is_no_sample_rather_than_a_wrong_one():
    """Docker returns this for a container that stopped mid-request."""
    assert read_sample({}) is None
    assert read_sample({"cpu_stats": {}, "memory_stats": {}}) is None


# --------------------------------------------------------------------------
# Memory
# --------------------------------------------------------------------------


def test_memory_is_reported_in_megabytes_against_the_limit():
    sample = read_sample(stats(usage=128 * MB, limit=512 * MB))
    assert sample.memory_mb == 128.0
    assert sample.memory_limit_mb == 512.0
    assert sample.memory_percent == 25.0


def test_page_cache_is_excluded_from_memory_use():
    """Cache is reclaimable, so counting it reports an idle app as nearly full.

    An app that has read a large file shows most of its cap as `usage`, but
    none of it would cause an OOM kill. `docker stats` subtracts it and so
    does this.
    """
    sample = read_sample(stats(usage=400 * MB, inactive_file=380 * MB, limit=512 * MB))
    assert sample.memory_mb == 20.0


def test_memory_percent_survives_a_missing_limit():
    """An unlimited container reports limit 0; dividing by it would crash."""
    sample = read_sample(stats(usage=64 * MB, limit=0))
    assert sample.memory_percent == 0.0


# --------------------------------------------------------------------------
# History
# --------------------------------------------------------------------------


def sample(at: float = 0.0) -> Sample:
    return Sample(at=at, cpu_percent=1.0, memory_mb=1.0, memory_limit_mb=512.0)


def test_history_keeps_samples_in_order():
    history = History(depth=10)
    for i in range(3):
        history.record("a", sample(at=float(i)))

    assert [s.at for s in history.samples("a")] == [0.0, 1.0, 2.0]


def test_history_is_bounded_so_it_cannot_grow_without_end():
    history = History(depth=3)
    for i in range(10):
        history.record("a", sample(at=float(i)))

    assert [s.at for s in history.samples("a")] == [7.0, 8.0, 9.0]


def test_apps_do_not_share_a_series():
    history = History()
    history.record("a", sample(at=1.0))
    history.record("b", sample(at=2.0))

    assert [s.at for s in history.samples("a")] == [1.0]
    assert history.latest("b").at == 2.0


def test_an_app_with_no_samples_reads_as_empty():
    assert History().samples("nobody") == []
    assert History().latest("nobody") is None


# --------------------------------------------------------------------------
# Collection
# --------------------------------------------------------------------------


def test_only_running_apps_are_sampled(db, monkeypatch):
    """Asking Docker about a stopped container costs a second and yields nothing."""
    asked: list[str] = []

    def fake_stats(app_id: str):
        asked.append(app_id)
        return sample()

    monkeypatch.setattr(metrics, "_stats_for", fake_stats)

    with store.session() as sess:
        running = App(name="up", source_ref="/s", status=AppStatus.RUNNING)
        asleep = App(name="down", source_ref="/s", status=AppStatus.SLEEPING)
        store.save(sess, running, asleep)
        running_id = running.id

    history = History()
    assert metrics.collect(history) == 1
    assert asked == [running_id]


def test_a_container_that_vanishes_mid_pass_is_skipped(db, monkeypatch):
    monkeypatch.setattr(metrics, "_stats_for", lambda app_id: None)
    with store.session() as sess:
        store.save(sess, App(name="up", source_ref="/s", status=AppStatus.RUNNING))

    history = History()
    assert metrics.collect(history) == 0
    assert history.samples("anything") == []


# --------------------------------------------------------------------------
# Configuration and lifecycle
# --------------------------------------------------------------------------


def test_metrics_are_on_by_default():
    assert config.settings().metrics_enabled


def test_metrics_can_be_switched_off(monkeypatch):
    monkeypatch.setenv("HANGAR_METRICS_INTERVAL", "0")
    assert not config.settings().metrics_enabled
    assert metrics.Collector().start() is False


def test_the_window_is_derived_from_interval_and_depth(monkeypatch):
    monkeypatch.setenv("HANGAR_METRICS_INTERVAL", "15")
    monkeypatch.setenv("HANGAR_METRICS_HISTORY", "120")
    assert config.settings().metrics_window_minutes == 30.0


# --------------------------------------------------------------------------
# The endpoint
# --------------------------------------------------------------------------


@pytest.fixture
def app_id(db):
    with store.session() as sess:
        app = App(name="notes", source_ref="/s", status=AppStatus.RUNNING)
        store.save(sess, app)
        return app.id


def test_metrics_endpoint_reports_the_caps_even_with_no_samples(
    client, app_id, monkeypatch
):
    """"0 of 512MB" needs the limit, which comes from config, not a reading."""
    monkeypatch.setenv("HANGAR_APP_MEMORY_MB", "512")
    metrics.HISTORY.clear()

    body = client.get(f"/apps/{app_id}/metrics").json()
    assert body["memory_limit_mb"] == 512.0
    assert body["current"] is None
    assert body["samples"] == []


def test_metrics_endpoint_returns_recorded_samples(client, app_id):
    metrics.HISTORY.clear()
    metrics.HISTORY.record(
        app_id, Sample(at=1.0, cpu_percent=12.5, memory_mb=64.0, memory_limit_mb=512.0)
    )

    body = client.get(f"/apps/{app_id}/metrics").json()
    assert body["current"]["cpu_percent"] == 12.5
    assert body["current"]["memory_percent"] == 12.5
    assert len(body["samples"]) == 1


def test_metrics_for_an_unknown_app_are_a_404(client, db):
    assert client.get("/apps/nope/metrics").status_code == 404


def test_deleting_an_app_forgets_its_history(client, app_id, fake_backend):
    metrics.HISTORY.record(app_id, sample())
    client.delete(f"/apps/{app_id}")

    assert metrics.HISTORY.samples(app_id) == []
