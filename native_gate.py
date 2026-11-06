"""Bounded Windows native-CHT admission shared by the Case workers.

Each native solve holds one OS byte-range lock. Windows releases the lock when
its worker process dies, so no stale PID table, scanner or cleanup daemon is
needed. This gate does not alter geometry, mesh, physics or convergence.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

from scheduler import _windows_free_commit_gib
from settings import STATE, policy_from_environment


class NativeSolveLease:
    def __init__(self, case_id: str, *, root: Path | None = None,
                 slots: int | None = None, minimum_commit_gib: float | None = None):
        policy = policy_from_environment()
        self.case_id = case_id
        self.root = Path(root) if root is not None else STATE / "native_cht_slots"
        self.slots = policy.native_solver_slots if slots is None else int(slots)
        self.minimum_commit_gib = (policy.minimum_native_commit_gib
                                   if minimum_commit_gib is None
                                   else float(minimum_commit_gib))
        self.wait_seconds = max(90 * 60, policy.native_timeout_minutes * 60)
        if not 1 <= self.slots <= 24 or self.minimum_commit_gib < 0:
            raise ValueError("Invalid native solver capacity policy")
        self.fd: int | None = None
        self.slot: int | None = None

    def acquire(self) -> None:
        if os.name != "nt":
            raise RuntimeError("Native CHT capacity gate requires Windows")
        import msvcrt

        self.root.mkdir(parents=True, exist_ok=True)
        begun = time.monotonic()
        last_notice = -60.0
        while time.monotonic() - begun < self.wait_seconds:
            free_commit = _windows_free_commit_gib()
            if free_commit is not None and free_commit >= self.minimum_commit_gib:
                for slot in range(self.slots):
                    fd = os.open(self.root / f"slot_{slot:02d}.lock",
                                 os.O_CREAT | os.O_RDWR | os.O_BINARY, 0o666)
                    try:
                        os.lseek(fd, 0, os.SEEK_SET)
                        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                    except OSError:
                        os.close(fd)
                        continue
                    # Another Case may have just acquired a slot and started
                    # allocating Fluent memory. Check measured commit again.
                    latest_commit = _windows_free_commit_gib()
                    if (latest_commit is not None
                            and latest_commit >= self.minimum_commit_gib):
                        self.fd, self.slot = fd, slot
                        print("NATIVE_CHT_SLOT_ACQUIRED", self.case_id,
                              "slot", slot, "free_commit_gib",
                              round(latest_commit, 1), flush=True)
                        return
                    os.lseek(fd, 0, os.SEEK_SET)
                    try:
                        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
                    finally:
                        os.close(fd)
                    break
            elapsed = time.monotonic() - begun
            if elapsed - last_notice >= 60:
                print("NATIVE_CHT_SLOT_WAIT", self.case_id,
                      "elapsed_min", round(elapsed / 60, 1),
                      "free_commit_gib", (round(free_commit, 1)
                                          if free_commit is not None else None),
                      flush=True)
                last_notice = elapsed
            time.sleep(3)
        raise TimeoutError("Native CHT capacity unavailable within the wait budget")

    def release(self) -> None:
        fd, slot = self.fd, self.slot
        self.fd, self.slot = None, None
        if fd is None:
            return
        import msvcrt

        try:
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        except OSError as error:
            print("NATIVE_CHT_SLOT_RELEASE_WARNING", self.case_id,
                  type(error).__name__, flush=True)
        finally:
            os.close(fd)
        print("NATIVE_CHT_SLOT_RELEASED", self.case_id, "slot", slot,
              flush=True)


__all__ = ["NativeSolveLease"]

