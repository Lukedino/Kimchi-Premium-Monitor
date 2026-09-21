"""Keep the test job separate from live monitoring and state publication.

These checks use the workflow's simple, dependency-free YAML layout. They are
not a general YAML parser. Mutations exercise the safety checks so a copied
operating step or a permissions override cannot silently pass as offline CI.
"""
from pathlib import Path
import re
import shlex
import unittest


WORKFLOW = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "offline-tests.yml"
ALLOWED_ACTIONS = {"actions/checkout@v4", "actions/setup-python@v5"}
TEST_COMMAND = ["python", "-B", "-S", "-m", "unittest", "discover", "-s", "tests", "-p", "test_*.py", "-v"]


def active_text(text):
    return "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))


def field_values(text, field):
    pattern = rf"^[ \t]*(?:-[ \t]*)?{re.escape(field)}:[ \t]*([^\n]*)$"
    return [value.strip().strip("\"'") for value in re.findall(pattern, text, re.MULTILINE)]


def run_commands(text):
    lines = text.splitlines()
    for index, line in enumerate(lines):
        match = re.match(r"^(\s*)(?:-\s*)?run:\s*(.*)$", line)
        if not match:
            continue
        command = match.group(2).strip()
        if command in {"|", "|-", "|+", ">", ">-", ">+"}:
            body = []
            for following in lines[index + 1:]:
                if following.strip() and len(following) - len(following.lstrip()) <= len(match.group(1)):
                    break
                body.append(following.strip())
            command = "\n".join(body)
        yield command


def checkout_step(text):
    lines = text.splitlines()
    for index, line in enumerate(lines):
        match = re.match(r"^([ \t]*)(- )?uses:[ \t]*actions/checkout@v4[ \t]*$", line)
        if not match:
            continue
        indent = len(match[1]) + (2 if match[2] else 0)
        block = [line]
        for following in lines[index + 1:]:
            if following.strip() and len(following) - len(following.lstrip()) < indent:
                break
            block.append(following)
        return "\n".join(block)
    return ""


def safety_violations(text):
    """A closed command boundary deliberately permits only stdlib discovery.

    It does not forbid subprocess use *inside* synthetic tests: the lock tests
    need real local child processes on both OSes, without launching the monitor.
    """
    text = active_text(text)
    errors = set()
    if re.search(r"\bsecrets\b|github\s*\.\s*token", text, re.IGNORECASE):
        errors.add("secret context")
    if re.search(r"^\s*(?:env|environment|container|services|token):", text, re.MULTILINE):
        errors.add("implicit runtime or credential injection")
    if not re.search(r"^permissions:\s*\n  contents: read\s*$", text, re.MULTILINE):
        errors.add("explicit read-only token required")
    if any(value for value in field_values(text, "permissions")):
        errors.add("inline permissions override")
    if re.search(r"^\s+[\w-]+:\s*['\"]?(?:write|write-all)['\"]?\s*$", text, re.MULTILINE):
        errors.add("write permission")
    actions = field_values(text, "uses")
    if set(actions) != ALLOWED_ACTIONS or len(actions) != 2:
        errors.add("unreviewed action or reusable job")
    if field_values(checkout_step(text), "persist-credentials") != ["false"]:
        errors.add("checkout credentials retained")
    if any(value != "false" for value in field_values(text, "continue-on-error")):
        errors.add("test failures ignored")
    commands = list(run_commands(text))
    if not commands:
        errors.add("test command missing")
    for command in commands:
        try:
            # Any added shell command, install, live flag, script or publish
            # operation falls outside this single permitted entry point.
            if shlex.split(command) != TEST_COMMAND:
                errors.add("non-offline command")
        except ValueError:
            errors.add("invalid command quoting")
    return errors


class OfflineWorkflowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.workflow = WORKFLOW.read_text(encoding="utf-8")

    def test_job_has_no_operating_capability_or_external_packages(self):
        self.assertEqual(safety_violations(self.workflow), set())

    def test_operating_commands_and_failure_suppression_are_rejected(self):
        original = next(run_commands(self.workflow))
        for command in (
            "python monitor.py", "python monitor.py --live --publish-state",
            "python -m pip install -r requirements.txt", "git push",
            "curl https://example.invalid/prices", "python -m unittest discover",
            original + " && git push", original + " || true",
            "|\n          " + original + "\n          python monitor.py --live",
        ):
            with self.subTest(command=command):
                self.assertIn("non-offline command", safety_violations(self.workflow.replace(original, command)))

    def test_secrets_and_extra_actions_cannot_be_added_to_test_job(self):
        for injected in (
            "    env:\n      TELEGRAM_BOT_TOKEN: ${{ secrets.TELEGRAM_BOT_TOKEN }}\n",
            "    env:\n      GH_TOKEN: ${{ github.token }}\n",
            "    environment: production\n",
            "    services:\n      feed:\n        image: some-image\n",
        ):
            with self.subTest(injected=injected):
                changed = self.workflow.replace("    steps:\n", injected + "    steps:\n")
                self.assertTrue(safety_violations(changed))
        changed = self.workflow + "      - uses: external/action@v1\n"
        self.assertIn("unreviewed action or reusable job", safety_violations(changed))

    def test_job_overrides_cannot_enable_writes_or_keep_credentials(self):
        for changed in (
            self.workflow.replace("contents: read", "contents: write"),
            self.workflow.replace("    steps:\n", "    permissions:\n      contents: write\n    steps:\n"),
            self.workflow.replace("    steps:\n", "    permissions: write-all\n    steps:\n"),
            self.workflow.replace("    steps:\n", "    permissions: {contents: write}\n    steps:\n"),
            self.workflow.replace("persist-credentials: false", "persist-credentials: true"),
            self.workflow.replace("          persist-credentials: false\n", ""),
            self.workflow.replace("          persist-credentials: false\n", "").replace(
                "          python-version:", "          persist-credentials: false\n          python-version:"),
            self.workflow.replace("    steps:\n", "    continue-on-error: true\n    steps:\n"),
        ):
            with self.subTest(changed=changed):
                self.assertTrue(safety_violations(changed))

    def test_matrix_covers_both_lock_platforms_and_supported_python_versions(self):
        text = active_text(self.workflow)
        os_matrix = re.search(r"^\s+os:\s*\[([^\]]+)\]", text, re.MULTILINE)
        python_matrix = re.search(r"^\s+python:\s*\[([^\]]+)\]", text, re.MULTILINE)
        self.assertIsNotNone(os_matrix)
        self.assertIsNotNone(python_matrix)
        self.assertEqual({item.strip() for item in os_matrix[1].split(",")}, {"ubuntu-latest", "windows-latest"})
        self.assertEqual({item.strip().strip("\"'") for item in python_matrix[1].split(",")}, {"3.11", "3.12"})
        self.assertEqual(field_values(text, "runs-on"), ["${{ matrix.os }}"])
        self.assertEqual(field_values(text, "python-version"), ["${{ matrix.python }}"])
        self.assertEqual(field_values(text, "fail-fast"), ["false"])

    def test_ci_events_and_lifetime_do_not_replace_operating_scheduler(self):
        text = active_text(self.workflow)
        trigger = re.search(r"^on:\n((?:[ \t].*\n|\n)+)", text, re.MULTILINE)
        self.assertIsNotNone(trigger)
        events = re.findall(r"^  ([\w_]+):", trigger[1], re.MULTILINE)
        self.assertEqual(set(events), {"pull_request", "push"})
        self.assertEqual(field_values(trigger[1], "branches"), ["[main]", "[main]"])
        timeouts = field_values(text, "timeout-minutes")
        self.assertEqual(len(timeouts), 1)
        self.assertTrue(timeouts[0].isdigit() and 0 < int(timeouts[0]) <= 15)
        self.assertEqual(field_values(text, "cancel-in-progress"), ["true"])
        self.assertEqual(len(field_values(text, "group")), 1)
        self.assertIn("offline-tests-", field_values(text, "group")[0])
        self.assertIn("github.ref", field_values(text, "group")[0])


if __name__ == "__main__":
    unittest.main()
