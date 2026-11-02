from __future__ import annotations

import threading
import time

from harness.proc_tree import (
    PeakSampler,
    ProcInfo,
    parse_posix_ps_output,
    parse_windows_process_json,
    tree_bytes,
)


def test_tree_bytes_nested_children():
    processes = [
        ProcInfo(pid=1, ppid=0, working_set_bytes=100),
        ProcInfo(pid=2, ppid=1, working_set_bytes=200),
        ProcInfo(pid=3, ppid=2, working_set_bytes=300),
        ProcInfo(pid=99, ppid=0, working_set_bytes=9000),
    ]
    assert tree_bytes(1, processes) == 600


def test_tree_bytes_missing_parent_still_counts_reachable_child():
    processes = [
        ProcInfo(pid=10, ppid=999, working_set_bytes=50),
        ProcInfo(pid=11, ppid=10, working_set_bytes=70),
    ]
    assert tree_bytes(10, processes) == 120


def test_tree_bytes_cycle_does_not_loop():
    processes = [
        ProcInfo(pid=1, ppid=2, working_set_bytes=100),
        ProcInfo(pid=2, ppid=1, working_set_bytes=200),
    ]
    assert tree_bytes(1, processes) == 300


def test_parse_windows_process_json_array():
    text = """[
  {"ProcessId": 4, "ParentProcessId": 0, "WorkingSetSize": 1024},
  {"ProcessId": 8, "ParentProcessId": 4, "WorkingSetSize": 2048}
]"""
    rows = parse_windows_process_json(text)
    assert rows == [
        ProcInfo(pid=4, ppid=0, working_set_bytes=1024),
        ProcInfo(pid=8, ppid=4, working_set_bytes=2048),
    ]


def test_parse_windows_process_json_single_object():
    text = '{"ProcessId": 12, "ParentProcessId": 4, "WorkingSetSize": 4096}'
    rows = parse_windows_process_json(text)
    assert rows == [ProcInfo(pid=12, ppid=4, working_set_bytes=4096)]


def test_parse_posix_ps_output():
    text = "  100    1  10\n  200  100  20\n"
    rows = parse_posix_ps_output(text)
    assert rows == [
        ProcInfo(pid=100, ppid=1, working_set_bytes=10 * 1024),
        ProcInfo(pid=200, ppid=100, working_set_bytes=20 * 1024),
    ]


def test_peak_sampler_tracks_maximum_and_stop_is_timely():
    lock = threading.Lock()
    step = {"value": 0}
    peaks = [
        [ProcInfo(pid=1, ppid=0, working_set_bytes=1_000_000_000)],
        [
            ProcInfo(pid=1, ppid=0, working_set_bytes=1_000_000_000),
            ProcInfo(pid=2, ppid=1, working_set_bytes=2_000_000_000),
        ],
        [ProcInfo(pid=1, ppid=0, working_set_bytes=500_000_000)],
    ]

    def fake_lister() -> list[ProcInfo]:
        with lock:
            index = step["value"]
            if index >= len(peaks):
                return peaks[-1]
            step["value"] = index + 1
            return peaks[index]

    interval = 0.05
    sampler = PeakSampler(1, interval, lister=fake_lister)
    sampler.start()
    time.sleep(interval * 3)
    started = time.monotonic()
    peak_gb = sampler.stop()
    elapsed = time.monotonic() - started
    assert peak_gb == 3_000_000_000 / (1024**3)
    assert elapsed < interval * 1.5


def test_peak_sampler_skips_lister_failures():
    calls = {"count": 0}

    def flaky_lister() -> list[ProcInfo]:
        calls["count"] += 1
        if calls["count"] == 1:
            raise RuntimeError("sample failed")
        return [ProcInfo(pid=5, ppid=0, working_set_bytes=1024**3)]

    sampler = PeakSampler(5, 0.02, lister=flaky_lister)
    sampler.start()
    time.sleep(0.08)
    peak_gb = sampler.stop()
    assert peak_gb == 1.0
