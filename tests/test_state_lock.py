"""Only synthetic state paths and an isolated stdlib state_lock child process."""
from contextlib import contextmanager
import errno
import os
from pathlib import Path
import queue
import shutil
import subprocess
import sys
import threading
import traceback
import unittest
from unittest.mock import patch
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import state_lock


# This exact program is the subprocess allowlist boundary for the offline runner.
# -I -S prevents startup environment/site hooks; it imports no monitor or client.
_CHILD_CODE = """\
import sys
sys.path.insert(0, sys.argv[1])
from state_lock import state_execution_lock, StateLockBusy, StateLockError
try:
    with state_execution_lock(sys.argv[2]):
        print('LOCKED', flush=True)
        if sys.argv[3] == 'hold':
            sys.stdin.readline()
except StateLockBusy:
    print('BUSY', flush=True)
    sys.exit(23)
except StateLockError:
    print('ERROR', flush=True)
    sys.exit(24)
"""


def _child_args(path, mode):
    return [sys.executable, "-I", "-S", "-B", "-c", _CHILD_CODE,
            str(Path(state_lock.__file__).resolve().parent), str(path), mode]


def _readline_with_deadline(process):
    output = queue.Queue()
    thread = threading.Thread(target=lambda: output.put(process.stdout.readline()), daemon=True)
    thread.start()
    try:
        return output.get(timeout=10).strip()
    except queue.Empty:
        raise AssertionError("Synthetic lock child did not respond within 10 seconds") from None


@contextmanager
def _holding_child(path):
    process = subprocess.Popen(_child_args(path, "hold"), stdin=subprocess.PIPE,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               text=True, shell=False)
    try:
        if _readline_with_deadline(process) != "LOCKED":
            raise AssertionError("Synthetic lock child did not acquire its lock")
        yield process
    finally:
        if process.poll() is None:
            process.terminate()
        process.communicate(timeout=10)


def _attempt_child(path):
    return subprocess.run(_child_args(path, "once"), stdin=subprocess.DEVNULL,
                          capture_output=True, text=True, timeout=10, check=False,
                          shell=False)


class StateLockTests(unittest.TestCase):
    def setUp(self):
        # Inherit the snapshot's Windows ACL; TemporaryDirectory(mode=0700) can
        # exclude the sandbox identity used by our synthetic child process.
        self.directory = ROOT / ("state-lock-test-" + uuid.uuid4().hex)
        self.directory.mkdir()
        self.assertEqual(self.directory.resolve().parent, ROOT.resolve())
        self.addCleanup(shutil.rmtree, self.directory)

    def test_two_processes_contend_and_normal_exit_releases_without_unlink(self):
        path = self.directory / "state.json"
        path.write_text('{"synthetic": true}', encoding="utf-8")
        before = path.read_bytes()
        lock_path = self.directory / ".state.json.execution.lock"
        with _holding_child(path) as holder:
            identity = lock_path.stat().st_ino
            result = _attempt_child(path)
            self.assertEqual((result.returncode, result.stdout.strip()), (23, "BUSY"))
            self.assertEqual(result.stderr, "")
            holder.stdin.write("release\n")
            holder.stdin.flush()
            self.assertEqual(holder.wait(timeout=10), 0)
        result = _attempt_child(path)
        self.assertEqual((result.returncode, result.stdout.strip()), (0, "LOCKED"))
        self.assertTrue(lock_path.exists())
        self.assertEqual(lock_path.stat().st_ino, identity)
        self.assertEqual(lock_path.read_bytes(), b"")
        self.assertEqual(path.read_bytes(), before)

    def test_process_termination_releases_without_pid_or_stale_file_recovery(self):
        path = self.directory / "state.json"
        with _holding_child(path) as holder:
            self.assertEqual(_attempt_child(path).returncode, 23)
            holder.kill()
            holder.wait(timeout=10)
        self.assertEqual(_attempt_child(path).returncode, 0)
        self.assertTrue((self.directory / ".state.json.execution.lock").exists())
        self.assertFalse(path.exists())

    def test_different_state_paths_can_run_at_the_same_time(self):
        with _holding_child(self.directory / "first.json"):
            result = _attempt_child(self.directory / "second.json")
            self.assertEqual((result.returncode, result.stdout.strip()), (0, "LOCKED"))

    def test_relative_and_parent_aliases_share_the_process_lock(self):
        previous = Path.cwd()
        self.addCleanup(os.chdir, previous)
        os.chdir(self.directory)
        (self.directory / "subdir").mkdir()
        with _holding_child(self.directory / "state.json"):
            result = _attempt_child(Path("subdir") / ".." / "state.json")
            self.assertEqual((result.returncode, result.stdout.strip()), (23, "BUSY"))

    @unittest.skipUnless(os.name == "nt", "Windows case-insensitive path identity")
    def test_windows_case_aliases_share_the_process_lock(self):
        with _holding_child(self.directory / "State.JSON"):
            self.assertEqual(_attempt_child(self.directory / "STATE.json").returncode, 23)

    def test_state_symlink_alias_shares_the_process_lock(self):
        path = self.directory / "state.json"
        path.write_text("{}", encoding="utf-8")
        alias = self.directory / "alias.json"
        try:
            alias.symlink_to(path)
        except OSError:
            self.skipTest("OS does not permit creating a synthetic symlink")
        with _holding_child(path):
            self.assertEqual(_attempt_child(alias).returncode, 23)

    def test_exception_inside_context_releases_and_preserves_body_error(self):
        path = self.directory / "new-parent" / "state.json"
        with self.assertRaisesRegex(ValueError, "synthetic body error"):
            with state_lock.state_execution_lock(path) as canonical:
                self.assertEqual(canonical, Path(os.path.normcase(os.path.realpath(path))))
                self.assertFalse(path.exists())
                raise ValueError("synthetic body error")
        with state_lock.state_execution_lock(path):
            pass
        self.assertTrue((path.parent / ".state.json.execution.lock").exists())

    def test_invalid_paths_are_sanitized_explicit_errors(self):
        for bad in ("", "\x00", None, 123, b"bytes-state"):
            with self.subTest(path_type=type(bad).__name__):
                with self.assertRaisesRegex(state_lock.StateLockError, "Invalid state path"):
                    with state_lock.state_execution_lock(bad):
                        self.fail("must not execute")
        self.assertEqual(list(self.directory.iterdir()), [])

    def test_directory_as_state_path_is_rejected(self):
        with self.assertRaises(state_lock.StateLockError):
            with state_lock.state_execution_lock(self.directory):
                self.fail("must not execute")

    def test_unsupported_platform_fails_before_creating_files(self):
        with patch.object(state_lock, "_PLATFORM", "unsupported"):
            with self.assertRaisesRegex(state_lock.StateLockError, "unsupported"):
                with state_lock.state_execution_lock(self.directory / "state.json"):
                    self.fail("must not execute")
        self.assertEqual(list(self.directory.iterdir()), [])

    def test_permission_error_has_no_path_or_os_exception_text(self):
        error = PermissionError(errno.EACCES, "synthetic-private-value", "synthetic-private-path")
        with patch.object(state_lock.os, "open", side_effect=error):
            with self.assertRaises(state_lock.StateLockError) as caught:
                with state_lock.state_execution_lock(self.directory / "state.json"):
                    self.fail("must not execute")
        formatted = "".join(traceback.format_exception(caught.exception))
        self.assertNotIn("synthetic-private", str(caught.exception))
        self.assertNotIn("PermissionError:", formatted)
        self.assertEqual(list(self.directory.iterdir()), [])

    def test_lock_acquisition_errors_close_handle_and_do_not_enter(self):
        for error, expected in ((errno.EACCES, state_lock.StateLockBusy),
                                (errno.EAGAIN, state_lock.StateLockBusy),
                                (errno.EDEADLK, state_lock.StateLockBusy),
                                (errno.EIO, state_lock.StateLockError)):
            with self.subTest(errno=error):
                captured = []

                def fail_acquire(fd):
                    captured.append(fd)
                    raise OSError(error, "synthetic-private-value")

                def unexpected_release(fd):
                    self.fail("never acquired")

                with patch.object(state_lock, "_lock_operations", return_value=(fail_acquire, unexpected_release)):
                    with self.assertRaises(expected) as caught:
                        with state_lock.state_execution_lock(self.directory / "state.json"):
                            self.fail("must not execute")
                self.assertNotIn("synthetic-private", str(caught.exception))
                with self.assertRaises(OSError):
                    os.fstat(captured[0])

    def test_release_error_is_visible_and_still_closes_handle(self):
        acquire, release = state_lock._lock_operations()
        captured = []

        def fail_release(fd):
            captured.append(fd)
            raise OSError(errno.EIO, "synthetic-private-value")

        path = self.directory / "state.json"
        with patch.object(state_lock, "_lock_operations", return_value=(acquire, fail_release)):
            with self.assertRaisesRegex(state_lock.StateLockError, "release") as caught:
                with state_lock.state_execution_lock(path):
                    pass
        self.assertNotIn("synthetic-private", str(caught.exception))
        with self.assertRaises(OSError):
            os.fstat(captured[0])
        with state_lock.state_execution_lock(path):
            pass

    def test_existing_nonregular_lock_path_is_rejected(self):
        (self.directory / ".state.json.execution.lock").mkdir()
        with self.assertRaises(state_lock.StateLockError):
            with state_lock.state_execution_lock(self.directory / "state.json"):
                self.fail("must not execute")


if __name__ == "__main__":
    unittest.main()
