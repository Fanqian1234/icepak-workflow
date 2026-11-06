"""Permanent regression for the OS-native Fluent concurrency lease.

Uses a private temporary lock directory; never launches AEDT or Fluent.
"""
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from native_gate import NativeSolveLease


@unittest.skipUnless(os.name == "nt", "Native Fluent lease requires Windows")
class NativeSolveLeaseTests(unittest.TestCase):
    def test_slot_acquired_released_and_reusable(self):
        with tempfile.TemporaryDirectory() as folder:
            lease = NativeSolveLease(
                "case_test", root=Path(folder), slots=1,
                minimum_commit_gib=0)
            with patch("native_gate._windows_free_commit_gib", return_value=50):
                lease.acquire()
                self.assertEqual(lease.slot, 0)
                self.assertIsNotNone(lease.fd)
                lease.release()
                self.assertIsNone(lease.fd)
                self.assertIsNone(lease.slot)
                lease.acquire()
                self.assertEqual(lease.slot, 0)
                lease.release()

    def test_another_handle_cannot_take_held_slot(self):
        import msvcrt

        with tempfile.TemporaryDirectory() as folder:
            lease = NativeSolveLease(
                "case_test", root=Path(folder), slots=1,
                minimum_commit_gib=0)
            with patch("native_gate._windows_free_commit_gib", return_value=50):
                lease.acquire()
                fd = os.open(Path(folder) / "slot_00.lock",
                             os.O_RDWR | os.O_BINARY)
                try:
                    os.lseek(fd, 0, os.SEEK_SET)
                    with self.assertRaises(OSError):
                        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                finally:
                    os.close(fd)
                    lease.release()


if __name__ == "__main__":
    unittest.main(verbosity=2)

