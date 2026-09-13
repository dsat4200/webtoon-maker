"""Opt-in process measurements using standard-library operating-system APIs.

This sampler owns no thread. Its owner calls ``sample`` while recording, usually
once per second from the GUI thread. CPU percentages describe the measured
interval; memory is probed at most once per second, and never during construction.
"""
from __future__ import annotations

import ctypes
import os
from pathlib import Path
import sys
import threading
import time
from typing import Any


def _empty_memory(reason: str) -> dict[str, Any]:
    return {
        "rss_bytes": None, "peak_rss_bytes": None, "private_bytes": None,
        "memory_source": None,
        "memory_unavailable_reasons": {
            "rss_bytes": reason, "peak_rss_bytes": reason, "private_bytes": reason,
        },
    }


def _windows_memory() -> dict[str, Any]:
    from ctypes import wintypes

    class ProcessMemoryCountersEx(ctypes.Structure):
        _fields_ = [
            ("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD),
            ("PeakWorkingSetSize", ctypes.c_size_t), ("WorkingSetSize", ctypes.c_size_t),
            ("QuotaPeakPagedPoolUsage", ctypes.c_size_t), ("QuotaPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t), ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
            ("PagefileUsage", ctypes.c_size_t), ("PeakPagefileUsage", ctypes.c_size_t),
            ("PrivateUsage", ctypes.c_size_t),
        ]

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    psapi = ctypes.WinDLL("psapi", use_last_error=True)
    kernel.GetCurrentProcess.argtypes = []
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    psapi.GetProcessMemoryInfo.argtypes = [
        wintypes.HANDLE, ctypes.POINTER(ProcessMemoryCountersEx), wintypes.DWORD,
    ]
    psapi.GetProcessMemoryInfo.restype = wintypes.BOOL
    counters = ProcessMemoryCountersEx()
    counters.cb = ctypes.sizeof(counters)
    if not psapi.GetProcessMemoryInfo(kernel.GetCurrentProcess(), ctypes.byref(counters), counters.cb):
        raise ctypes.WinError(ctypes.get_last_error())
    return {
        "rss_bytes": int(counters.WorkingSetSize),
        "peak_rss_bytes": int(counters.PeakWorkingSetSize),
        "private_bytes": int(counters.PrivateUsage),
        "memory_source": "Windows GetProcessMemoryInfo",
        "private_bytes_semantics": "Private committed bytes (PrivateUsage); not resident memory.",
        "memory_unavailable_reasons": {},
    }


def _posix_memory() -> dict[str, Any]:
    result = _empty_memory("This platform does not expose this measurement through the available standard-library APIs.")
    if sys.platform.startswith("linux"):
        fields = {}
        for line in Path("/proc/self/status").read_text(encoding="ascii").splitlines():
            if line.startswith(("VmRSS:", "VmHWM:")):
                name, value, unit = line.split()
                if unit == "kB":
                    fields[name.rstrip(":")] = int(value) * 1024
        for field, name in (("rss_bytes", "VmRSS"), ("peak_rss_bytes", "VmHWM")):
            if name in fields:
                result[field] = fields[name]
                result["memory_unavailable_reasons"].pop(field, None)
        result["memory_source"] = "Linux /proc/self/status"
    else:
        # ru_maxrss is a high-water mark, not current RSS. macOS reports bytes;
        # other supported Unix implementations generally report KiB.
        import resource
        peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        result["peak_rss_bytes"] = int(peak if sys.platform == "darwin" else peak * 1024)
        result["memory_source"] = "resource.getrusage (peak RSS only)"
        result["memory_unavailable_reasons"].pop("peak_rss_bytes", None)
    return result


def _read_memory() -> dict[str, Any]:
    try:
        return _windows_memory() if sys.platform == "win32" else _posix_memory()
    except Exception as error:
        return _empty_memory(f"{type(error).__name__}: {error}")


class ProcessResourceSampler:
    """Measure one process and, when called by its owner, that thread's CPU."""

    def __init__(self) -> None:
        self._owner_thread_id = threading.get_ident()
        self._wall = time.perf_counter()
        self._process = time.process_time()
        self._thread_cpu = time.thread_time() if hasattr(time, "thread_time") else None
        self._thread_wall = self._wall
        self._memory: dict[str, Any] | None = None
        self._memory_at: float | None = None
        self._logical_cpus: int | None = None
        self._cpu_count_read = False

    def sample(self) -> dict[str, Any]:
        now = time.perf_counter()
        process = time.process_time()
        wall_seconds = now - self._wall
        process_seconds = process - self._process
        self._wall, self._process = now, process
        reasons = {}
        valid = wall_seconds > 0 and process_seconds >= 0
        process_percent = process_seconds / wall_seconds * 100 if valid else None
        if not valid:
            reasons["process_cpu_percent"] = "No positive wall-clock interval or process CPU clock moved backwards."
        if not self._cpu_count_read:
            self._cpu_count_read = True
            self._logical_cpus = os.cpu_count()
        machine_percent = process_percent / self._logical_cpus if process_percent is not None and self._logical_cpus else None
        if machine_percent is None:
            reasons["process_cpu_machine_percent"] = reasons.get("process_cpu_percent", "Logical processor count is unavailable.")
        thread_percent = None
        thread_interval = None
        if threading.get_ident() != self._owner_thread_id:
            reasons["gui_thread_cpu_percent"] = "Sample called from a different thread; owner-thread CPU is unavailable."
        elif self._thread_cpu is None:
            reasons["gui_thread_cpu_percent"] = "time.thread_time is unavailable on this platform."
        else:
            thread_cpu = time.thread_time()
            thread_interval = now - self._thread_wall
            delta = thread_cpu - self._thread_cpu
            if thread_interval > 0 and delta >= 0:
                thread_percent = delta / thread_interval * 100
            else:
                reasons["gui_thread_cpu_percent"] = "No positive owner-thread sample interval or thread CPU clock moved backwards."
            self._thread_cpu, self._thread_wall = thread_cpu, now
        if self._memory_at is None or now - self._memory_at >= 1.0:
            self._memory = _read_memory()
            self._memory_at = now
        # Memory values are primitives apart from this separately copied map.
        memory = dict(self._memory or _empty_memory("No memory sample available."))
        memory["memory_unavailable_reasons"] = dict(memory["memory_unavailable_reasons"])
        return {
            "cpu_interval_seconds": max(0.0, wall_seconds),
            "process_cpu_percent": process_percent,
            "process_cpu_cores": process_percent / 100 if process_percent is not None else None,
            "process_cpu_machine_percent": machine_percent,
            "logical_processor_count": self._logical_cpus,
            "cpu_percent_semantics": "Process CPU / wall time × 100; 100% equals one logical CPU. Machine percent divides by logical processor count.",
            "gui_thread_cpu_percent": thread_percent,
            "gui_thread_cpu_interval_seconds": thread_interval,
            "gui_thread_cpu_semantics": "CPU time of the constructing thread (GUI only when constructed there), not GUI wall time.",
            "cpu_unavailable_reasons": reasons,
            **memory,
            "memory_sample_age_seconds": max(0.0, now - self._memory_at) if self._memory_at is not None else None,
            "memory_sample_interval_seconds": 1.0,
        }
