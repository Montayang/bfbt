"""Absolute process-memory gate for low-memory V2 workers."""

from __future__ import annotations

import os
import resource
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from bfbt.performance.diagnostics import MemoryBudgetExceeded


def process_rss_bytes() -> int:
    """Return current resident memory without allocating a large probe object."""

    try:
        fields = Path("/proc/self/statm").read_text(encoding="utf-8").split()
        return int(fields[1]) * os.sysconf("SC_PAGE_SIZE")
    except (IndexError, OSError, ValueError):
        peak = int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
        return peak if sys.platform == "darwin" else peak * 1_024


def child_process_rss_bytes(pid: int) -> int:
    """Read one Linux worker's current RSS from procfs."""

    if pid <= 0:
        raise ValueError("worker pid must be positive")
    try:
        for line in Path(f"/proc/{pid}/status").read_text(
            encoding="utf-8"
        ).splitlines():
            if line.startswith("VmRSS:"):
                return int(line.split()[1]) * 1_024
    except (OSError, ValueError, IndexError):
        return 0
    return 0


class SupervisedProcess(Protocol):
    pid: int | None
    exitcode: int | None

    def is_alive(self) -> bool: ...

    def terminate(self) -> None: ...

    def kill(self) -> None: ...

    def join(self, timeout: float | None = None) -> None: ...


@dataclass(frozen=True)
class WorkerMemoryResult:
    pid: int
    observed_peak_rss_mib: float
    exitcode: int


class WorkerMemorySupervisor:
    """Poll and terminate one dedicated worker before it exceeds its RSS cap."""

    def __init__(
        self,
        *,
        max_process_rss_mib: int,
        poll_seconds: float = 0.05,
        reader: Callable[[int], int] = child_process_rss_bytes,
    ) -> None:
        if max_process_rss_mib < 256:
            raise ValueError("max_process_rss_mib must be at least 256")
        if poll_seconds <= 0:
            raise ValueError("poll_seconds must be positive")
        self.max_process_rss_mib = max_process_rss_mib
        self.poll_seconds = poll_seconds
        self._reader = reader

    def wait(self, process: SupervisedProcess) -> WorkerMemoryResult:
        if process.pid is None or process.pid <= 0:
            raise ValueError("worker must be started before supervision")
        observed = 0
        limit = self.max_process_rss_mib * 1_024 * 1_024
        while process.is_alive():
            rss = max(0, int(self._reader(process.pid)))
            observed = max(observed, rss)
            if rss > limit:
                process.terminate()
                process.join(timeout=5.0)
                if process.is_alive():
                    process.kill()
                    process.join(timeout=5.0)
                raise MemoryBudgetExceeded(
                    f"worker pid={process.pid} RSS "
                    f"{rss / (1_024 * 1_024):.2f} MiB exceeds "
                    f"max_process_rss_mib={self.max_process_rss_mib}"
                )
            process.join(timeout=self.poll_seconds)
            if process.is_alive():
                time.sleep(self.poll_seconds)
        process.join()
        exitcode = process.exitcode
        if exitcode is None:
            raise RuntimeError("worker exited without an exit code")
        return WorkerMemoryResult(
            pid=process.pid,
            observed_peak_rss_mib=round(observed / (1_024 * 1_024), 6),
            exitcode=exitcode,
        )


@dataclass(frozen=True)
class ProcessMemorySample:
    phase: str
    ordinal: int
    rss_mib: float


class AbsoluteMemoryMonitor:
    """Sample and enforce a process-wide RSS ceiling at safe boundaries.

    A20 will run the V2 chunk worker in a dedicated process and call this gate
    during each chunk. The injectable reader keeps A19 acceptance deterministic
    and avoids allocating memory merely to test the failure path.
    """

    def __init__(
        self,
        *,
        max_process_rss_mib: int,
        reader: Callable[[], int] = process_rss_bytes,
    ) -> None:
        if max_process_rss_mib < 256:
            raise ValueError("max_process_rss_mib must be at least 256")
        self.max_process_rss_mib = max_process_rss_mib
        self._reader = reader
        self._observed_bytes = 0
        self._samples: list[ProcessMemorySample] = []

    def checkpoint(self, *, phase: str, ordinal: int) -> ProcessMemorySample:
        if not phase:
            raise ValueError("memory checkpoint phase cannot be empty")
        if ordinal < 0:
            raise ValueError("memory checkpoint ordinal cannot be negative")
        rss_bytes = int(self._reader())
        if rss_bytes < 0:
            raise ValueError("RSS reader returned a negative value")
        self._observed_bytes = max(self._observed_bytes, rss_bytes)
        sample = ProcessMemorySample(
            phase=phase,
            ordinal=ordinal,
            rss_mib=round(rss_bytes / (1_024 * 1_024), 6),
        )
        self._samples.append(sample)
        if rss_bytes > self.max_process_rss_mib * 1_024 * 1_024:
            raise MemoryBudgetExceeded(
                f"process RSS {sample.rss_mib:.2f} MiB exceeds "
                f"max_process_rss_mib={self.max_process_rss_mib}"
            )
        return sample

    @property
    def observed_peak_rss_mib(self) -> float:
        return round(self._observed_bytes / (1_024 * 1_024), 6)

    @property
    def samples(self) -> tuple[ProcessMemorySample, ...]:
        return tuple(self._samples)
