from __future__ import annotations

import threading

from comic_editor.core import performance_resources as resources


def _memory():
    return {"rss_bytes": 1024, "peak_rss_bytes": 2048, "private_bytes": 4096,
            "memory_source": "test", "memory_unavailable_reasons": {}}


def test_constructor_does_not_probe_memory_or_cpu_count(monkeypatch):
    def forbidden():
        raise AssertionError("probe at construction")

    monkeypatch.setattr(resources, "_read_memory", forbidden)
    monkeypatch.setattr(resources.os, "cpu_count", forbidden)
    sampler = resources.ProcessResourceSampler()
    assert sampler._memory is None


def test_interval_cpu_equivalents_and_memory_cache(monkeypatch):
    values = {"wall": 10.0, "process": 2.0, "thread": 1.0}
    calls = []
    monkeypatch.setattr(resources.time, "perf_counter", lambda: values["wall"])
    monkeypatch.setattr(resources.time, "process_time", lambda: values["process"])
    monkeypatch.setattr(resources.time, "thread_time", lambda: values["thread"])
    monkeypatch.setattr(resources.os, "cpu_count", lambda: 8)
    monkeypatch.setattr(resources, "_read_memory", lambda: calls.append(1) or _memory())
    sampler = resources.ProcessResourceSampler()
    values.update(wall=12.0, process=5.0, thread=1.5)
    sample = sampler.sample()
    assert sample["process_cpu_percent"] == 150
    assert sample["process_cpu_cores"] == 1.5
    assert sample["process_cpu_machine_percent"] == 18.75
    assert sample["gui_thread_cpu_percent"] == 25
    assert sample["cpu_interval_seconds"] == 2
    assert sample["rss_bytes"] == 1024
    values.update(wall=12.5, process=5.5, thread=1.75)
    sampler.sample()
    assert calls == [1]
    values.update(wall=13.1, process=5.8, thread=2.0)
    sampler.sample()
    assert calls == [1, 1]


def test_missing_values_are_null_with_explanations_and_cache_is_detached(monkeypatch):
    monkeypatch.setattr(resources.os, "cpu_count", lambda: None)
    monkeypatch.setattr(resources, "_read_memory", lambda: resources._empty_memory("unavailable for test"))
    sample = resources.ProcessResourceSampler().sample()
    assert sample["process_cpu_machine_percent"] is None
    assert "count" in sample["cpu_unavailable_reasons"]["process_cpu_machine_percent"]
    for field in ("rss_bytes", "peak_rss_bytes", "private_bytes"):
        assert sample[field] is None
        assert sample["memory_unavailable_reasons"][field] == "unavailable for test"


def test_wrong_thread_does_not_report_its_cpu_as_gui_cpu(monkeypatch):
    monkeypatch.setattr(resources, "_read_memory", _memory)
    sampler = resources.ProcessResourceSampler()
    results = []
    worker = threading.Thread(target=lambda: results.append(sampler.sample()))
    worker.start()
    worker.join(timeout=1)
    assert not worker.is_alive()
    assert results[0]["gui_thread_cpu_percent"] is None
    assert "different thread" in results[0]["cpu_unavailable_reasons"]["gui_thread_cpu_percent"]


def test_memory_failure_returns_null_instead_of_crashing(monkeypatch):
    def broken():
        raise OSError("memory probe denied")

    target = "_windows_memory" if resources.sys.platform == "win32" else "_posix_memory"
    monkeypatch.setattr(resources, target, broken)
    result = resources._read_memory()
    assert result["rss_bytes"] is None
    assert "memory probe denied" in result["memory_unavailable_reasons"]["rss_bytes"]


def test_current_platform_memory_sanity():
    result = resources._read_memory()
    for field in ("rss_bytes", "peak_rss_bytes", "private_bytes"):
        if result[field] is None:
            assert result["memory_unavailable_reasons"][field]
        else:
            assert isinstance(result[field], int) and result[field] >= 0
    if result["rss_bytes"] is not None and result["peak_rss_bytes"] is not None:
        assert result["peak_rss_bytes"] >= result["rss_bytes"]
