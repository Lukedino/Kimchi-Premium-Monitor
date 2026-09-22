"""별도 의존성 CI의 자격·실행 범위와 hash lock 계약. stdlib만 사용한다."""
import ast
from pathlib import Path
import re
import runpy
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github/workflows/dependency-smoke.yml"
COMMANDS = {
    "python -I -m pip --isolated install --index-url https://pypi.org/simple "
    "--only-binary=:all: --require-hashes -r locks/dependency-smoke.txt",
    "python -I -m pip --isolated check",
    "python -I -S -B -X utf8 scripts/dependency_smoke.py --self-check",
    "python -I -S -B -X utf8 scripts/dependency_smoke.py --environment-check",
    "python -I -S -B -X utf8 scripts/dependency_smoke.py --contract-check",
}


def check_workflow(text):
    if re.search(r"secrets\s*\.|github\.token|\b(write|write-all)\b|^\s*(env|environment|services|container|continue-on-error|shell|defaults|working-directory):", text, re.M):
        return False
    actions = re.findall(r"^\s*(?:-\s*)?uses: (.+)$", text, re.M)
    if actions != ["actions/checkout@v4", "actions/setup-python@v5"]:
        return False
    if len(re.findall(r"persist-credentials: false", text)) != 1:
        return False
    if re.findall(r"^\s+contents: (.+)$", text, re.M) != ["read"]:
        return False
    if "os: [ubuntu-latest, windows-latest]" not in text or "python: ['3.11', '3.12']" not in text:
        return False
    if re.findall(r"timeout-minutes: (\d+)", text) != ["10"]:
        return False
    if "pull_request:" not in text or "push:" not in text or re.search(r"schedule:|workflow_dispatch:|pull_request_target:", text):
        return False
    commands = []
    lines = text.splitlines()
    index = 0
    while index < len(lines):
        line = lines[index]
        stripped = line.strip().removeprefix("- ")
        if stripped.startswith("run:"):
            content = stripped[4:].strip()
            if content == ">-":
                indent = len(line) - len(line.lstrip())
                continuation = []
                while index + 1 < len(lines) and len(lines[index + 1]) - len(lines[index + 1].lstrip()) > indent:
                    index += 1
                    continuation.append(lines[index].strip())
                content = " ".join(continuation)
            commands.append(content)
        index += 1
    return len(commands) == 5 and set(commands) == COMMANDS


def pins_and_hashes(text):
    records = {}
    name = None
    for line in text.splitlines():
        if not line.strip() or line.startswith("#"):
            continue
        package = re.fullmatch(r"([a-zA-Z0-9_.-]+)==([^\s\\]+)\s*\\?", line)
        if package:
            name, version = package.groups()
            if name in records:
                raise ValueError("duplicate")
            records[name] = (version, [])
        elif re.fullmatch(r"\s+--hash=sha256:[a-f0-9]{64}\s*\\?", line) and name:
            records[name][1].append(line.strip().split()[0])
        else:
            raise ValueError("unexpected lock input")
    if not records or any(not hashes for _, hashes in records.values()):
        raise ValueError("missing hash")
    return records


class DependencyContractTests(unittest.TestCase):
    def test_workflow_is_separate_and_closed(self):
        self.assertTrue(check_workflow(WORKFLOW.read_text()))

    def test_credential_write_provider_and_unlocked_install_mutations_fail(self):
        source = WORKFLOW.read_text()
        mutations = [
            source.replace("contents: read", "contents: write"),
            source.replace("persist-credentials: false", "persist-credentials: true"),
            source.replace("--require-hashes ", ""),
            source.replace("--only-binary=:all: ", ""),
            source.replace("https://pypi.org/simple", "https://example.invalid/simple"),
            source.replace("--environment-check", "--environment-check; python monitor.py --live"),
            source + "\n      - run: python monitor.py --dry-run\n",
            source + "\n      - uses: actions/upload-artifact@v4\n",
            source + "\n      - name: Unexpected action\n        uses: actions/upload-artifact@v4\n",
            source + "\n        shell: bash --noprofile --norc -e -o pipefail {0}\n",
            source + "\n        env:\n          TOKEN: ${{ secrets.SYNTHETIC }}\n",
            source.replace("pull_request:", "pull_request_target:"),
        ]
        for index, value in enumerate(mutations):
            with self.subTest(index=index):
                self.assertFalse(check_workflow(value))

    def test_complete_hashes_and_exact_public_pins(self):
        wanted = dict(re.findall(r"^([\w.-]+)==([^\s]+)$", (ROOT / "constraints.txt").read_text(encoding="utf-8"), re.M))
        self.assertEqual(len(wanted), 24)
        for filename, size in (("dependency-smoke.txt", 24), ("runtime-windows.txt", 24), ("runtime-linux.txt", 23)):
            with self.subTest(filename=filename):
                found = pins_and_hashes((ROOT / "locks" / filename).read_text())
                self.assertEqual(len(found), size)
                self.assertEqual({k: v for k, (v, _) in found.items()},
                                 {k: v for k, v in wanted.items() if filename != "runtime-linux.txt" or k != "tzdata"})
        declared = dict(re.findall(r"^([\w.-]+)==([^\s]+)$", (ROOT / "requirements.txt").read_text(encoding="utf-8"), re.M))
        self.assertEqual(declared, {"requests": "2.34.2", "yfinance": "1.7.0"})

    def test_unhashed_and_injected_lock_inputs_fail(self):
        for value in ("requests==2.34.2\n", "--extra-index-url https://example.invalid\n",
                      "requests @ https://example.invalid/package.whl\n"):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    pins_and_hashes(value)

    def test_snapshot_does_not_include_operational_code_or_state(self):
        scope = runpy.run_path(str(ROOT / "scripts/dependency_smoke.py"))
        selected = scope["SOURCES"]
        self.assertNotIn("monitor.py", selected)
        self.assertNotIn("state.json", selected)
        self.assertNotIn(".github/workflows/kimp-monitor.yml", selected)
        self.assertTrue(all(not name.startswith(".env") for name in selected))
        # Pure path containment does not need an actual mode-0700 directory
        # (whose Windows ACL differs between runner/sandbox identities).
        base = ROOT / "synthetic-uncreated-path"
        self.assertTrue(scope["inside"](base / "child", base))
        self.assertFalse(scope["inside"](base.parent, base))

    def test_native_source_has_no_product_import_or_provider_call(self):
        module = ast.parse((ROOT / "scripts/dependency_native.py").read_text(encoding="utf-8"))
        imports = [node.module for node in ast.walk(module) if isinstance(node, ast.ImportFrom)]
        imports += [alias.name for node in ast.walk(module) if isinstance(node, ast.Import) for alias in node.names]
        self.assertFalse(any(name and name.split(".")[0] in {"monitor", "state_lock"} for name in imports))
        calls = [node.func.attr for node in ast.walk(module) if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)]
        self.assertFalse(set(calls) & {"Ticker", "history", "download", "perform", "send"})

    def test_isolated_mode_explicitly_disables_system_timezone_paths(self):
        module = ast.parse((ROOT / "scripts/dependency_smoke.py").read_text(encoding="utf-8"))
        resets = [node for node in ast.walk(module) if isinstance(node, ast.Call)
                  and isinstance(node.func, ast.Attribute) and node.func.attr == "reset_tzpath"]
        self.assertEqual(len(resets), 1)
        self.assertEqual(ast.literal_eval(resets[0].args[0]), ())


if __name__ == "__main__":
    unittest.main()
