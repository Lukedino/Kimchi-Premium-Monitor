"""Publication credentials never reach collection, package install or Git argv."""
import base64
import contextlib
import io
import os
from pathlib import Path
import subprocess
import unittest
from unittest.mock import patch

from test_publish_retry import FakeGit
import monitor


class PublishOnlyTests(unittest.TestCase):
    def setUp(self):
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.output = self.stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
        self.stack.enter_context(patch.dict(os.environ, {}, clear=True))
        self.stack.enter_context(patch.object(monitor, "_requests", side_effect=AssertionError("provider forbidden")))
        self.stack.enter_context(patch.object(monitor, "_ticker", side_effect=AssertionError("provider forbidden")))
        self.stack.enter_context(patch.object(monitor, "send_telegram", side_effect=AssertionError("delivery forbidden")))

    def test_publish_only_validates_and_publishes_inside_same_lock_without_configuration(self):
        locked = []
        calls = []
        @contextlib.contextmanager
        def lock(path):
            self.assertEqual(path, monitor.STATE_FILE)
            locked.append(True)
            try:
                yield path
            finally:
                locked.clear()
        def phase(name):
            def call(path, **kwargs):
                self.assertTrue(locked)
                self.assertEqual(kwargs, {"required": True} if name == "load" else {})
                calls.append(name)
                return {"history": [], "last_alert": {}}
            return call
        with patch.object(monitor, "state_execution_lock", lock), \
             patch.object(monitor, "load_state", side_effect=phase("load")), \
             patch.object(monitor, "publish_state", side_effect=phase("publish")), \
             patch.object(monitor, "configure", side_effect=AssertionError("configuration forbidden")):
            self.assertEqual(monitor.main(["--publish-only"]), 0)
        self.assertEqual(calls, ["load", "publish"])

    def test_publish_only_invalid_saved_state_prevents_git(self):
        with patch.object(monitor, "state_execution_lock", return_value=contextlib.nullcontext(monitor.STATE_FILE)), \
             patch.object(monitor, "load_state", side_effect=monitor.StateError("invalid saved state")), \
             patch.object(monitor, "publish_state") as publish:
            self.assertEqual(monitor.main(["--publish-only"]), 1)
        publish.assert_not_called()

    def test_publish_only_missing_state_is_not_an_empty_state_or_deletion_commit(self):
        with patch.object(monitor, "state_execution_lock", return_value=contextlib.nullcontext(monitor.STATE_FILE)), \
             patch("builtins.open", side_effect=FileNotFoundError("synthetic missing")), \
             patch.object(monitor, "publish_state") as publish:
            self.assertEqual(monitor.main(["--publish-only"]), 1)
        publish.assert_not_called()

    def test_publish_only_cannot_select_other_path_or_enable_live_collection(self):
        for arguments in (["--publish-only", "--live"], ["--publish-only", "--publish-state"],
                          ["--publish-only", "--state-file", "synthetic-other.json"]):
            with self.subTest(arguments=arguments), contextlib.redirect_stderr(io.StringIO()), \
                 patch.object(monitor, "state_execution_lock", side_effect=AssertionError("unexpected state")), \
                 self.assertRaises(SystemExit):
                monitor.main(arguments)

    def test_token_goes_only_to_fixed_push_subprocess_environment(self):
        token = "SYNTHETIC_PUBLISH_SECRET"
        environment = {"KIMCHI_PUBLISH_TOKEN": token, "GITHUB_ACTIONS": "true",
                       "GITHUB_SERVER_URL": "https://github.com", "GITHUB_REPOSITORY": "synthetic/monitor"}
        fake, observed = FakeGit(), []
        fake.changed = True
        # A remote literally named commit is valid; only argv's subcommand
        # determines whether credentials are needed, not other argument values.
        fake.remote = "commit"
        fake.upstream = "refs/remotes/commit/main"
        def run(command, **kwargs):
            observed.append((command, kwargs["env"]))
            return fake(command, **kwargs)
        with patch.dict(os.environ, environment, clear=True), patch.object(subprocess, "run", run):
            monitor.publish_state(monitor.STATE_FILE)
            self.assertEqual(os.environ, environment)
        encoded = base64.b64encode(("x-access-token:" + token).encode()).decode()
        for command, env in observed:
            self.assertNotIn(token, " ".join(command))
            self.assertNotIn(encoded, " ".join(command))
            self.assertNotIn("KIMCHI_PUBLISH_TOKEN", env)
            if command[1] == "push":
                self.assertEqual(env["GIT_CONFIG_KEY_1"], "http.https://github.com/synthetic/monitor.git.extraheader")
                self.assertEqual(env["GIT_CONFIG_VALUE_1"], "AUTHORIZATION: basic " + encoded)
                self.assertEqual(env["GIT_CONFIG_VALUE_0"], "")
            else:
                self.assertNotIn("GIT_CONFIG_VALUE_1", env)
        self.assertNotIn(token, self.output.getvalue())
        self.assertNotIn(encoded, self.output.getvalue())
        self.assertFalse(any(args[0] == "config" for args in fake.calls))

    def test_credential_context_or_remote_mismatch_stops_before_commit(self):
        good = {"KIMCHI_PUBLISH_TOKEN": "SYNTHETIC_PUBLISH_SECRET", "GITHUB_ACTIONS": "true",
                "GITHUB_SERVER_URL": "https://github.com", "GITHUB_REPOSITORY": "synthetic/monitor"}
        for bad_field in ("GITHUB_ACTIONS", "GITHUB_SERVER_URL", "GITHUB_REPOSITORY", "remote", "multiple_remotes"):
            with self.subTest(bad_field=bad_field):
                environment = dict(good)
                fake = FakeGit()
                fake.changed = True
                if bad_field == "remote":
                    fake.remote_urls = "https://example.invalid/receiver\n"
                elif bad_field == "multiple_remotes":
                    fake.remote_urls += "https://github.com/synthetic/monitor\n"
                else:
                    environment[bad_field] = "INVALID"
                with patch.dict(os.environ, environment, clear=True), \
                     patch.object(subprocess, "run", fake), self.assertRaises(monitor.StateError):
                    monitor.publish_state(monitor.STATE_FILE)
                self.assertEqual(fake.commit_count, 0)
                self.assertEqual(fake.pushes, [])


class OperatingWorkflowTests(unittest.TestCase):
    def test_secret_only_in_publication_step_and_failure_state_is_published(self):
        root = Path(__file__).resolve().parents[1]
        text = (root / ".github/workflows/kimp-monitor.yml").read_text(encoding="utf-8")
        install = text.split("      - name: Install dependencies", 1)[1].split("      - name: Run monitor", 1)[0]
        collect, publish = text.split("      - name: Run monitor", 1)[1].split(
            "      - name: Publish saved state", 1)
        self.assertIn("persist-credentials: false", text)
        self.assertIn("--require-hashes -r locks/runtime-linux.txt", install)
        self.assertNotIn("secrets.", install)
        self.assertIn("python -B monitor.py --live", collect)
        self.assertNotIn("KIMCHI_PUBLISH_TOKEN", collect)
        self.assertNotIn("GITHUB_TOKEN", collect)
        self.assertNotIn("--publish-state", collect)
        self.assertIn("always()", publish)
        self.assertIn("steps.monitor.outcome == 'success'", publish)
        self.assertIn("steps.monitor.outcome == 'failure'", publish)
        self.assertIn("KIMCHI_PUBLISH_TOKEN: ${{ secrets.GITHUB_TOKEN }}", publish)
        self.assertIn("python -B -S monitor.py --publish-only", publish)
        self.assertNotIn("TELEGRAM", publish)
        self.assertEqual(text.count("GITHUB_TOKEN"), 1)
        self.assertIn("cron: '*/15 * * * *'", text)
        self.assertIn("cancel-in-progress: false", text)


if __name__ == "__main__":
    unittest.main()
