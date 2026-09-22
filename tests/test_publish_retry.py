"""Mock-only Git publication tests; no repository, remote, or state is accessed."""
import contextlib
import io
import os
from pathlib import Path
import socket
import subprocess
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
with patch.object(os, "environ", {}):
    import monitor as monitor


class FakeGit:
    def __init__(self):
        self.base = "1" * 40
        self.pending = []
        self.changed = False
        self.staged_state = False
        self.commit_hook = None
        self.remote_urls = "https://github.com/synthetic/monitor.git\n"
        self.behind = 0
        self.branch = "refs/heads/main"
        self.upstream = "refs/remotes/origin/main"
        self.remote = "origin"
        self.remote_ref = "refs/heads/main"
        self.ref_output = None
        self.count_output = None
        self.list_output = None
        self.push_results = []
        self.failures = {}
        self.calls = []
        self.commit_count = 0
        self.next_commit = 2

    @property
    def head(self):
        return self.pending[-1]["sha"] if self.pending else self.base

    @property
    def pushes(self):
        return [args for args in self.calls if args[0] == "push"]

    def add_bot_commit(self):
        previous = self.head
        sha = f"{self.next_commit:040x}"
        self.next_commit += 1
        commit = {"sha": sha, "metadata": [previous, "kimp-bot", "bot@kimp-monitor",
                    "kimp-bot", "bot@kimp-monitor", "update state [skip ci]"],
                  "paths": "state.json\0"}
        self.pending.append(commit)
        return commit

    def __call__(self, command, **kwargs):
        assert command[0] == "git"
        assert Path(kwargs["cwd"]) == ROOT
        assert kwargs["timeout"] == 30
        assert kwargs["capture_output"] is True
        assert kwargs["text"] is True
        args = command[1:]
        self.calls.append(args)
        operation = args[4] if args[0] == "-c" else args[0]
        if operation in self.failures:
            error = self.failures[operation]
            if isinstance(error, BaseException):
                raise error
            return SimpleNamespace(returncode=error, stdout="synthetic-secret", stderr="synthetic-secret")
        code, output = 0, ""
        if args == ["diff", "--cached", "--quiet", "--", "state.json"]:
            code = int(self.staged_state)
        elif args == ["ls-files", "--error-unmatch", "--", "state.json"]:
            pass
        elif args[:4] == ["remote", "get-url", "--push", "--all"]:
            assert args[4] == self.remote
            output = self.remote_urls
        elif args == ["diff", "--quiet", "HEAD", "--", "state.json"]:
            code = int(self.changed)
        elif operation == "commit":
            assert args == ["-c", "user.name=kimp-bot", "-c", "user.email=bot@kimp-monitor",
                            "commit", "--only", "-m", "update state [skip ci]", "--", "state.json"]
            assert self.changed
            self.add_bot_commit()
            self.changed = False
            self.commit_count += 1
            if self.commit_hook:
                self.commit_hook(self)
        elif args == ["symbolic-ref", "--quiet", "HEAD"]:
            output = self.branch + "\n"
        elif args[0] == "for-each-ref":
            assert args[2] == self.branch
            output = self.ref_output if self.ref_output is not None else "\0".join(
                [self.branch, self.upstream, self.remote, self.remote_ref]) + "\n"
        elif args[:2] == ["rev-parse", "--verify"]:
            assert args[2] in ("HEAD", self.upstream)
            output = (self.head if args[2] == "HEAD" else self.base) + "\n"
        elif args[:3] == ["rev-list", "--left-right", "--count"]:
            assert args[3] == f"{self.base}...{self.head}"
            output = self.count_output if self.count_output is not None else f"{self.behind}\t{len(self.pending)}\n"
        elif args[:2] == ["rev-list", "--reverse"]:
            assert args[2] == f"{self.base}..{self.head}"
            output = self.list_output if self.list_output is not None else "".join(
                commit["sha"] + "\n" for commit in self.pending)
        elif args[:2] == ["show", "-s"]:
            commit = next(commit for commit in self.pending if commit["sha"] == args[-1])
            output = (commit["metadata"][0] if args[2] == "--format=%P" else
                      "\0".join(commit["metadata"])) + "\n\n"
        elif args[:6] == ["diff-tree", "--no-commit-id", "--name-only", "--no-renames", "-r", "-z"]:
            assert args[-1] == "--"
            commit = next(commit for commit in self.pending if commit["sha"] == args[-2])
            output = commit["paths"]
        elif args[0] == "push":
            code = self.push_results.pop(0) if self.push_results else 0
            if code == 0:
                self.base = self.head
                self.pending = []
        else:
            raise AssertionError(f"Unapproved mock Git operation: {operation}")
        return SimpleNamespace(returncode=code, stdout=output, stderr="synthetic-secret")


class PublicationRetryTests(unittest.TestCase):
    def setUp(self):
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.output = self.stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
        self.stack.enter_context(patch.object(os, "environ", {}))
        self.stack.enter_context(patch.object(socket, "socket", side_effect=AssertionError("network forbidden")))
        self.stack.enter_context(patch.object(monitor, "_requests", side_effect=AssertionError("HTTP forbidden")))
        self.stack.enter_context(patch.object(monitor, "_ticker", side_effect=AssertionError("Yahoo forbidden")))
        self.git = FakeGit()
        self.stack.enter_context(patch.object(subprocess, "run", side_effect=self.git))

    def publish(self):
        monitor.publish_state(monitor.STATE_FILE)

    def assert_blocked(self):
        with self.assertRaises(monitor.StateError) as error:
            self.publish()
        self.assertEqual(self.git.pushes, [])
        self.assertEqual(self.git.commit_count, 0)
        self.assertNotIn("synthetic-secret", str(error.exception) + self.output.getvalue())

    def test_identical_retry_publishes_prior_commit_without_committing_again(self):
        self.git.changed = True
        self.git.push_results = [1, 1, 0]
        with self.assertRaises(monitor.StateError):
            self.publish()
        tip = self.git.head
        self.assertEqual(self.git.commit_count, 1)
        expected = ["push", "--no-follow-tags", "--", "origin", f"{tip}:refs/heads/main"]
        self.assertEqual(self.git.pushes, [expected, expected])

        self.publish()

        self.assertEqual(self.git.commit_count, 1)
        self.assertEqual(self.git.pending, [])
        self.assertEqual(self.git.pushes[-1], ["push", "--no-follow-tags", "--", "origin",
                                             f"{tip}:refs/heads/main"])

    def test_no_change_and_no_pending_commit_does_not_push(self):
        self.publish()
        self.assertEqual(self.git.pushes, [])
        self.assertEqual(self.git.commit_count, 0)
        self.assertTrue(any(args[0] == "rev-list" for args in self.git.calls))

    def test_changed_state_after_user_commit_stops_before_any_index_change(self):
        self.git.changed = True
        self.git.add_bot_commit()["metadata"][1] = "Developer"
        self.assert_blocked()
        self.assertFalse(any(args[0] in {"add", "reset"} for args in self.git.calls))
        self.assertTrue(self.git.changed)

    def test_changed_state_also_requires_branch_upstream_and_linear_history(self):
        for defect in ("detached", "missing_upstream", "behind", "other_path"):
            with self.subTest(defect=defect):
                self.git.__init__()
                self.git.changed = True
                if defect == "detached":
                    self.git.failures["symbolic-ref"] = 1
                elif defect == "missing_upstream":
                    self.git.ref_output = self.git.branch + "\0\0\0\n"
                elif defect == "behind":
                    self.git.behind = 1
                else:
                    self.git.add_bot_commit()["paths"] = "README.md\0"
                self.assert_blocked()

    def test_staged_state_is_preserved_without_commit_or_push(self):
        self.git.changed = self.git.staged_state = True
        self.assert_blocked()
        self.assertTrue(self.git.staged_state)
        self.assertFalse(any(args[0] in {"add", "reset"} for args in self.git.calls))

    def test_new_commit_on_pending_bot_chain_uses_exact_target_and_validates_parent(self):
        self.git.changed = True
        self.git.remote = "state-origin"
        self.git.upstream = "refs/remotes/state-origin/published"
        self.git.remote_ref = "refs/heads/published"
        previous = self.git.add_bot_commit()["sha"]
        self.publish()
        self.assertEqual(self.git.commit_count, 1)
        self.assertEqual(self.git.pushes, [["push", "--no-follow-tags", "--", "state-origin",
                                           f"{self.git.head}:refs/heads/published"]])
        self.assertNotEqual(self.git.head, previous)

    def test_branch_or_upstream_change_during_commit_prevents_push(self):
        for field, value in (("branch", "refs/heads/other"),
                             ("remote_ref", "refs/heads/other"),
                             ("base", "f" * 40)):
            with self.subTest(field=field):
                self.git.__init__()
                self.git.changed = True
                self.git.commit_hook = lambda git: setattr(git, field, value)
                with self.assertRaises(monitor.StateError):
                    self.publish()
                self.assertEqual(self.git.commit_count, 1)
                self.assertEqual(self.git.pushes, [])

    def test_new_commit_with_hook_added_path_is_not_published(self):
        self.git.changed = True
        self.git.commit_hook = lambda git: git.pending[-1].update(paths="state.json\0README.md\0")
        with self.assertRaises(monitor.StateError):
            self.publish()
        self.assertEqual(self.git.pushes, [])

    def test_existing_linear_bot_chain_uses_one_verified_tip_and_upstream_target(self):
        self.git.branch = "refs/heads/state-worker"
        self.git.upstream = "refs/remotes/state-origin/published"
        self.git.remote = "state-origin"
        self.git.remote_ref = "refs/heads/published"
        self.git.add_bot_commit()
        self.git.add_bot_commit()
        tip = self.git.head
        self.publish()
        self.assertEqual(self.git.pushes, [["push", "--no-follow-tags", "--", "state-origin",
                                           f"{tip}:refs/heads/published"]])

    def test_existing_bot_retry_is_bounded_and_uses_the_same_sha(self):
        for codes, succeeds in (([1, 0], True), ([1, 1], False)):
            with self.subTest(codes=codes):
                self.git.pending = []
                self.git.calls = []
                self.git.add_bot_commit()
                self.git.push_results = codes.copy()
                if succeeds:
                    self.publish()
                else:
                    with self.assertRaises(monitor.StateError):
                        self.publish()
                self.assertEqual(len(self.git.pushes), 2)
                self.assertEqual(self.git.pushes[0], self.git.pushes[1])
                self.assertFalse(any(args[0] in ("fetch", "pull", "rebase") for args in self.git.calls))

    def test_detached_head_is_not_a_successful_no_change_run(self):
        self.git.failures["symbolic-ref"] = 1
        self.assert_blocked()

    def test_upstream_query_failure_or_missing_upstream_is_not_success(self):
        self.git.failures["for-each-ref"] = 128
        self.assert_blocked()
        self.git.failures.clear()
        self.git.ref_output = self.git.branch + "\0\0\0\n"
        self.assert_blocked()

    def test_branch_ref_mismatch_and_multiple_ref_results_are_rejected(self):
        for output in ("refs/heads/other\0refs/remotes/origin/main\0origin\0refs/heads/main\n",
                       "unexpected\nmultiple\n", ""):
            with self.subTest(output=output):
                self.git.ref_output = output
                self.assert_blocked()

    def test_invalid_remote_or_non_branch_target_is_rejected(self):
        for remote, upstream, target in ((".", "refs/heads/main", "refs/heads/main"),
                                        ("-option", "refs/remotes/-option/main", "refs/heads/main"),
                                        ("origin", "refs/remotes/other/main", "refs/heads/main"),
                                        ("origin", "refs/remotes/origin/main", "refs/tags/release"),
                                        ("origin", "refs/remotes/origin/main", "refs/heads/")):
            with self.subTest(remote=remote, target=target):
                self.git.remote, self.git.upstream, self.git.remote_ref = remote, upstream, target
                self.assert_blocked()

    def test_behind_and_diverged_branches_do_not_push(self):
        self.git.behind = 1
        self.assert_blocked()
        self.git.add_bot_commit()
        self.assert_blocked()

    def test_invalid_counts_or_revision_count_disagreement_is_rejected(self):
        for counts in ("", "0", "-1 1", "one two", "0 1 extra"):
            with self.subTest(counts=counts):
                self.git.count_output = counts
                self.assert_blocked()
        self.git.add_bot_commit()
        self.git.count_output = "0 0"
        self.assert_blocked()

    def test_pending_list_must_match_count_and_tip(self):
        self.git.add_bot_commit()
        for output in ("", "f" * 40 + "\n", self.git.head + "\n" + self.git.head + "\n"):
            with self.subTest(output=output):
                self.git.list_output = output
                self.assert_blocked()

    def test_every_pending_commit_requires_exact_bot_metadata_and_one_parent(self):
        for index, wrong in ((0, "1" * 40 + " " + "9" * 40), (0, "9" * 40),
                             (1, "Developer"), (2, "developer@example.invalid"),
                             (3, "Developer"), (4, "developer@example.invalid"),
                             (5, "update unrelated work [skip ci]")):
            with self.subTest(index=index, wrong=wrong):
                self.git.pending = []
                first = self.git.add_bot_commit()
                self.git.add_bot_commit()
                first["metadata"][index] = wrong
                self.assert_blocked()

    def test_empty_or_additional_changed_paths_are_not_trusted(self):
        for paths in ("", "monitor.py\0", "state.json\0README.md\0", "old-state.json\0state.json\0"):
            with self.subTest(paths=paths):
                self.git.pending = []
                self.git.add_bot_commit()["paths"] = paths
                self.assert_blocked()

    def test_query_io_failures_are_visible_without_raw_output(self):
        for operation in ("symbolic-ref", "for-each-ref", "rev-parse", "rev-list", "show", "diff-tree"):
            for failure in (1, OSError("synthetic-secret"), subprocess.TimeoutExpired("synthetic-secret", 30),
                            UnicodeDecodeError("utf-8", b"synthetic-secret", 0, 1, "invalid byte")):
                with self.subTest(operation=operation, failure=type(failure).__name__):
                    self.git.failures = {operation: failure}
                    self.git.pending = []
                    self.git.add_bot_commit()
                    self.assert_blocked()

    def test_invalid_state_file_is_rejected_before_git(self):
        with self.assertRaises(monitor.StateError):
            monitor.publish_state(ROOT / "different-state.json")
        self.assertEqual(self.git.calls, [])

    def test_canonical_repository_state_path_from_lock_wrapper_is_allowed(self):
        canonical = os.path.normcase(os.path.realpath(monitor.STATE_FILE))
        monitor.publish_state(Path(canonical))
        self.assertTrue(self.git.calls)
        self.assertEqual(self.git.pushes, [])

    def test_resolved_alias_is_allowed_only_when_it_targets_repository_state(self):
        alias = str(ROOT / "synthetic-state-alias.json")
        realpath = os.path.realpath
        with patch.object(os.path, "realpath", side_effect=lambda path: (
                realpath(monitor.STATE_FILE) if os.fspath(path) == alias else realpath(path))):
            monitor.publish_state(alias)
        self.assertTrue(self.git.calls)


if __name__ == "__main__":
    unittest.main()
