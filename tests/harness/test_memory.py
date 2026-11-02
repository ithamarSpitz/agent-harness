from __future__ import annotations

import sys

import pytest

from harness import memory


SAMPLE_MEMINFO = """\
MemTotal:       16384000 kB
MemAvailable:    4194304 kB
MemFree:         1000000 kB
"""

SAMPLE_VM_STAT = """\
Mach Virtual Memory Statistics: (page size of 4096 bytes)
Pages free:                               262144.
Pages active:                             100.
Pages inactive:                           262144.
Pages speculative:                        262144.
"""

MALFORMED_MEMINFO = "MemTotal: 100 kB\n"
MALFORMED_VM_STAT = "not vm_stat output\n"
INCOMPLETE_VM_STAT = """\
Mach Virtual Memory Statistics: (page size of 4096 bytes)
Pages free:                               262144.
Pages active:                             100.
"""


def test_linux_parser_sample_and_malformed():
    assert memory.linux_free_memory_gb(meminfo=SAMPLE_MEMINFO) == pytest.approx(4.0)
    assert memory.linux_free_memory_gb(meminfo=MALFORMED_MEMINFO) is None


def test_darwin_parser_sample_and_malformed():
    assert memory.darwin_free_memory_gb(vm_stat=SAMPLE_VM_STAT) == pytest.approx(3.0)
    assert memory.darwin_free_memory_gb(vm_stat=MALFORMED_VM_STAT) is None
    assert memory.darwin_free_memory_gb(vm_stat=INCOMPLETE_VM_STAT) is None


def test_windows_parser_sample_and_malformed():
    five_gib = 5 * 1024**3
    assert memory.windows_free_memory_gb(avail_phys_bytes=five_gib) == pytest.approx(5.0)
    assert memory.windows_free_memory_gb(avail_phys_bytes=-1) is None


def test_windows_parser_uses_injected_query():
    assert memory.windows_free_memory_gb(
        query_avail_phys=lambda: 2 * 1024**3,
    ) == pytest.approx(2.0)
    assert memory.windows_free_memory_gb(query_avail_phys=lambda: None) is None


def test_free_memory_gb_returns_none_when_platform_reader_raises(monkeypatch):
    def boom() -> float | None:
        raise RuntimeError("reader failed")

    monkeypatch.setattr(memory, "linux_free_memory_gb", boom)
    monkeypatch.setattr(sys, "platform", "linux")
    assert memory.free_memory_gb() is None
