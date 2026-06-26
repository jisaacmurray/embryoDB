"""Tests for bounded worker parallelism (per-host slot pidfiles).

The single-instance pidfile guard was replaced by N atomic slots so up to
settings.worker_max_slots workers coexist on one host. The point is resilience:
one wedged job (the JIM799 6-day MCR hang) ties up only its own slot instead of
freezing the whole queue. The DB atomic claim still prevents two slots from
running the same job, so slots only add concurrency, never double-execution.
"""

from __future__ import annotations

import os
import time

import pytest

from embryodb.pipeline import worker as w


@pytest.fixture
def slot_dir(tmp_path, monkeypatch):
    # Point slot pidfiles at a private tmp dir and cap slots at 3.
    monkeypatch.setattr(w.settings, "worker_pidfile_dir", tmp_path)
    monkeypatch.setattr(w.settings, "worker_max_slots", 3)
    yield tmp_path


def test_acquire_takes_lowest_free_slot_then_reports_full(slot_dir):
    # Claim all three slots; each returns the next-lowest index.
    assert w.acquire_free_slot() == 0
    assert w.acquire_free_slot() == 1
    assert w.acquire_free_slot() == 2
    # All taken (this live process owns all three) → no slot, queue "full".
    assert w.acquire_free_slot() is None
    assert w.has_free_slot() is False
    assert w.running_worker_count() == 3


def test_stale_slot_is_reclaimed(slot_dir):
    # A slot whose pid is dead must be reusable without a manual sweep.
    dead_pid = _a_dead_pid()
    pf = w._pidfile_for_slot(0)
    pf.write_text(str(dead_pid), encoding="utf-8")

    # running_worker_count cleans the corpse; acquire reclaims slot 0.
    assert w.running_worker_count() == 0
    assert w.acquire_free_slot() == 0
    assert w._slot_pid(pf) == os.getpid()


def test_live_slot_is_not_stolen(slot_dir):
    # A slot held by THIS (live) process is never handed out again.
    pf = w._pidfile_for_slot(0)
    pf.write_text(str(os.getpid()), encoding="utf-8")
    assert w._try_claim_slot(0) is False
    # The next free slot is 1, not 0.
    assert w.acquire_free_slot() == 1


def test_inflight_empty_slot_is_not_stolen(slot_dir):
    # Reproduces the TOCTOU: worker A created the pidfile via O_EXCL but hasn't
    # written its pid yet (empty file, fresh mtime). Worker B must treat it as
    # occupied, NOT reclaim it — otherwise both "own" the slot and parallelism
    # exceeds max_slots.
    pf = w._pidfile_for_slot(0)
    pf.write_text("")  # in-flight: created, pid not yet written
    assert w._slot_holder(pf) == "live"
    assert w._try_claim_slot(0) is False
    # The next free slot is 1, not 0.
    assert w.acquire_free_slot() == 1
    assert w.running_worker_count() == 2  # the in-flight slot 0 counts as held


def test_orphaned_empty_slot_is_reclaimed(slot_dir, monkeypatch):
    # An empty pidfile left by a creator that died mid-claim is reclaimable once
    # it has lingered past the grace window.
    monkeypatch.setattr(w, "_SLOT_INFLIGHT_GRACE", 0.0)
    pf = w._pidfile_for_slot(0)
    pf.write_text("")
    time.sleep(0.01)  # push mtime past the (now zero) grace window
    assert w._slot_holder(pf) == "reclaimable"
    assert w.acquire_free_slot() == 0
    assert w._slot_pid(pf) == os.getpid()


def test_release_slot_frees_it(slot_dir):
    slot = w.acquire_free_slot()
    w._claimed_slot = slot
    assert w.running_worker_count() == 1
    w._release_slot()
    assert w._claimed_slot is None
    assert w.running_worker_count() == 0
    assert w.has_free_slot() is True


def test_spawn_worker_noop_when_remote(slot_dir, monkeypatch):
    monkeypatch.setattr(w.settings, "remote", True)
    assert w.spawn_worker() is None


def test_spawn_worker_noop_when_slots_full(slot_dir, monkeypatch):
    monkeypatch.setattr(w.settings, "remote", False)
    # Fill all slots with live (this-process) pids.
    for i in range(3):
        w._pidfile_for_slot(i).write_text(str(os.getpid()), encoding="utf-8")

    def _boom(*a, **k):
        raise AssertionError("must not fork when all slots are busy")

    monkeypatch.setattr(w.subprocess, "Popen", _boom)
    assert w.spawn_worker() is None


def _a_dead_pid() -> int:
    """Return a pid that is not currently alive."""
    for candidate in range(2**15, 2**15 - 5000, -1):
        if not w._pid_is_alive(candidate):
            return candidate
    raise RuntimeError("could not find a dead pid for the test")


# ---------------------------------------------------------------------------
# Memory-pressure guard (added after the 2026-06-26 penticton OOM)
# ---------------------------------------------------------------------------


def test_memory_guard_ok_when_healthy(monkeypatch):
    monkeypatch.setattr(w.settings, "worker_slab_guard_gib", 30.0)
    monkeypatch.setattr(w.settings, "worker_memfree_floor_mib", 512.0)
    monkeypatch.setattr(
        w, "_read_meminfo_kb",
        lambda: {"MemFree": 8 * 1024 * 1024, "SReclaimable": 2 * 1024 * 1024},
    )
    assert w._memory_pressure_reason() is None


def test_memory_guard_trips_on_huge_slab(monkeypatch):
    monkeypatch.setattr(w.settings, "worker_slab_guard_gib", 30.0)
    monkeypatch.setattr(w.settings, "worker_memfree_floor_mib", 512.0)
    # 64 GiB SReclaimable — the stranded-NFS-inode signature from the incident.
    monkeypatch.setattr(
        w, "_read_meminfo_kb",
        lambda: {"MemFree": 8 * 1024 * 1024, "SReclaimable": 64 * 1024 * 1024},
    )
    reason = w._memory_pressure_reason()
    assert reason is not None and "SReclaimable" in reason


def test_memory_guard_trips_on_low_memfree(monkeypatch):
    monkeypatch.setattr(w.settings, "worker_slab_guard_gib", 30.0)
    monkeypatch.setattr(w.settings, "worker_memfree_floor_mib", 512.0)
    # 325 MiB free — what `free -h` showed during the incident.
    monkeypatch.setattr(
        w, "_read_meminfo_kb",
        lambda: {"MemFree": 325 * 1024, "SReclaimable": 1 * 1024 * 1024},
    )
    reason = w._memory_pressure_reason()
    assert reason is not None and "MemFree" in reason


def test_memory_guard_disabled_with_zero_thresholds(monkeypatch):
    monkeypatch.setattr(w.settings, "worker_slab_guard_gib", 0.0)
    monkeypatch.setattr(w.settings, "worker_memfree_floor_mib", 0.0)
    monkeypatch.setattr(
        w, "_read_meminfo_kb",
        lambda: {"MemFree": 1, "SReclaimable": 999 * 1024 * 1024},
    )
    assert w._memory_pressure_reason() is None


def test_memory_guard_none_when_meminfo_unavailable(monkeypatch):
    monkeypatch.setattr(w, "_read_meminfo_kb", lambda: {})
    assert w._memory_pressure_reason() is None
