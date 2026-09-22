"""자격 없는 소스 사본에서 의존성만 검증한다. 운영 monitor를 import하지 않는다.

python -I -S -B scripts/dependency_smoke.py --self-check
python -I -S -B scripts/dependency_smoke.py --environment-check
python -I -S -B scripts/dependency_smoke.py --contract-check
실수로 발생하는 I/O를 차단하며 악의적인 native 코드의 OS sandbox는 아니다.
"""
from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
import os
from pathlib import Path
import platform
import socket
import subprocess
import sys
import sysconfig
import tempfile
import zoneinfo


SOURCES = (
    "scripts/dependency_smoke.py", "scripts/dependency_native.py",
    "requirements.txt", "requirements-dev.txt", "constraints.txt",
    "locks/dependency-smoke.txt", "locks/runtime-linux.txt", "locks/runtime-windows.txt",
    ".github/workflows/dependency-smoke.yml", "tests/test_dependency_contract.py",
)

FAILURE_CODES = frozenset({
    "PACKAGE_MISMATCH", "UNLOCKED_PACKAGE", "REQUEST_PREPARATION_FAILED",
    "CERTIFI_TRUST_EMPTY", "CFFI_MEMORY_MISMATCH", "DATAFRAME_RESULT_MISMATCH",
    "NONFINITE_RESULT_MISMATCH", "SYSTEM_TZPATH_ACTIVE", "ZONEINFO_OFFSET_MISMATCH",
    "PANDAS_OFFSET_MISMATCH", "LXML_PARSE_MISMATCH", "BS4_PARSE_MISMATCH",
    "SYSTEM_DATEUTIL_TZPATH_ACTIVE", "DATEUTIL_ZONE_MISSING", "DATEUTIL_OFFSET_MISMATCH",
    "PROTOBUF_ROUNDTRIP_MISMATCH", "SQLITE_ROUNDTRIP_MISMATCH",
    "GUARD_NOT_ENFORCED", "GUARD_COUNT_MISMATCH", "ALLOWED_TEMP_ROUNDTRIP_MISMATCH",
})


def failure_code(error):
    """이미 코드에 정의한 진단만 공개한다. 임의 예외 문자열은 출력하지 않는다."""
    message = error.args[0] if isinstance(error, AssertionError) and error.args else None
    return message if isinstance(message, str) and message in FAILURE_CODES else "UNCLASSIFIED_FAILURE"


def inside(path, root):
    try:
        Path(path).resolve().relative_to(Path(root).resolve())
        return True
    except (OSError, ValueError):
        return False


def no_links(path, root):
    if not inside(path, root):
        raise RuntimeError("SOURCE_OUTSIDE_CHECKOUT")
    current = path
    while current != root.parent:
        info = current.lstat()
        if current.is_symlink() or getattr(info, "st_file_attributes", 0) & 0x400:
            raise RuntimeError("SOURCE_LINK_FORBIDDEN")
        current = current.parent


def package_location():
    # -S가 venv의 prefix를 복원하지 않는 Python 3.11/3.12도 지원한다.
    executable = Path(sys.executable).absolute()
    for root in (executable.parent.parent, executable.parent):
        if (root / "pyvenv.cfg").is_file():
            suffix = Path("Lib/site-packages") if os.name == "nt" else Path(
                f"lib/python{sys.version_info.major}.{sys.version_info.minor}/site-packages")
            return root, root / suffix
    return Path(sys.prefix), Path(sysconfig.get_path("purelib"))


def child(config_path):
    config = json.loads(Path(config_path).read_text(encoding="utf-8"))
    run = Path(config["run"])
    source = run / "source"
    site = Path(config["site"])
    stdlib = Path(sysconfig.get_path("stdlib"))
    sys.prefix = sys.exec_prefix = config["prefix"]
    allowed = (run, site, stdlib, Path(sys.base_prefix) / "DLLs")
    global_sites = (stdlib / "site-packages", stdlib / "dist-packages")
    exact = {Path(p).resolve() for p in sys.path if p.lower().endswith(".zip")}
    sys.path[:] = [str(source / "scripts"), str(site)] + [p for p in sys.path if p and
        (Path(p).resolve() in exact or any(inside(p, root) for root in allowed))]
    sys.dont_write_bytecode = True
    os.chdir(source)
    tempfile.tempdir = str(run / "tmp")
    # -I는 PYTHONTZPATH 환경변수를 무시하므로 public API로 OS 경로를 비운다.
    # 이후 zoneinfo는 선택한 venv의 tzdata 패키지만 사용할 수 있다.
    zoneinfo.reset_tzpath(())
    # Windows stdlib의 OS 정보 subprocess만 guard 전에 한 번 채운다.
    platform.uname()
    blocked = Counter()
    locations = {}

    def reject(code):
        blocked[code] += 1
        if code not in locations:
            frame = sys._getframe(2)
            stack = []
            for _ in range(8):
                if frame is None:
                    break
                stack.append(Path(frame.f_code.co_filename).name + ":" +
                             frame.f_code.co_name + ":" + str(frame.f_lineno))
                frame = frame.f_back
            locations[code] = " <- ".join(stack)
        raise PermissionError("DEPENDENCY_GUARD_" + code)

    def path_check(value, write=False):
        if isinstance(value, int) or not isinstance(value, (str, bytes, os.PathLike)):
            return
        raw = os.fsdecode(value)
        if raw.lower() in {"nul", "\\\\.\\nul", "\\\\?\\nul", "/dev/null"}:
            return
        path = Path(raw).resolve()
        if path.name.lower().startswith(".env") or path.name.lower() in {"state.json", ".netrc", "_netrc"}:
            reject("PRIVATE_FILE")
        if write:
            if not inside(path, run):
                reject("WRITE_OUTSIDE_RUN")
        elif any(inside(path, p) for p in global_sites) and not inside(path, site):
            reject("GLOBAL_SITE")
        elif path not in exact and not any(inside(path, p) for p in allowed):
            reject("READ_OUTSIDE_ALLOWLIST")

    def audit(event, args):
        if event in {"socket.connect", "socket.connect_ex", "socket.getaddrinfo", "socket.gethostbyname",
                     "socket.gethostbyaddr", "socket.sendto", "socket.bind"}:
            reject("NETWORK")
        if event in {"subprocess.Popen", "os.system", "os.posix_spawn", "os.posix_spawnp", "os.spawn",
                     "os.exec", "os.fork", "os.forkpty", "os.startfile", "os.kill", "os.killpg"}:
            reject("PROCESS")
        if event == "open":
            path, mode, flags = args
            writing = isinstance(mode, str) and any(c in mode for c in "wax+") or (
                isinstance(flags, int) and bool(flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT |
                                                         os.O_TRUNC | os.O_APPEND)))
            path_check(path, writing)
        if event in {"os.listdir", "os.scandir"}:
            path_check(args[0] if args and args[0] is not None else os.getcwd())
        if event in {"os.mkdir", "os.remove", "os.rmdir", "os.chmod", "os.utime"}:
            path_check(args[0], True)
        if event in {"os.rename", "os.link", "os.symlink"}:
            path_check(args[0], True)
            path_check(args[1], True)

    sys.addaudithook(audit)
    # urllib3 import의 IPv6 가용성 bind 탐색은 이 무통신 검사에 필요 없다.
    # 네트워크를 허용하는 대신 capability를 끄며 모든 실제 bind 차단은 유지한다.
    socket.has_ipv6 = False
    code = 0
    try:
        if config["mode"] == "self":
            from dependency_native import guard_transports
            requests, curl = guard_transports(reject)
            def bind_probe():
                with socket.socket() as sock:
                    sock.bind(("127.0.0.1", 0))
            checks = (
                lambda: (Path(config["checkout"]) / "requirements.txt").read_bytes(),
                lambda: (Path(config["checkout"]) / "guard-must-not-exist.txt").write_text("blocked"),
                lambda: (run / ".env.synthetic").read_bytes(),
                lambda: (run / "state.json").read_bytes(),
                lambda: socket.getaddrinfo("example.invalid", 443),
                bind_probe,
                lambda: subprocess.run([sys.executable, "-c", "pass"], check=True),
                lambda: requests.sessions.Session.request(None),
                lambda: curl.Curl.perform(None),
                lambda: curl.AsyncCurl.add_handle(None, None),
            )
            for check in checks:
                try:
                    check()
                except PermissionError as exc:
                    if not str(exc).startswith("DEPENDENCY_GUARD_"):
                        raise
                else:
                    raise AssertionError("GUARD_NOT_ENFORCED")
            target = run / "tmp/allowed.txt"
            target.write_text("synthetic", encoding="utf-8")
            assert target.read_text(encoding="utf-8") == "synthetic", "ALLOWED_TEMP_ROUNDTRIP_MISMATCH"
            assert blocked == Counter(READ_OUTSIDE_ALLOWLIST=1, WRITE_OUTSIDE_RUN=1,
                                      PRIVATE_FILE=2, NETWORK=3, PROCESS=1, NATIVE_NETWORK=2), "GUARD_COUNT_MISMATCH"
            print("GUARD_SELF_CHECK=" + json.dumps({"passed": len(checks) + 1, "events": dict(blocked)}))
            blocked.clear()  # 위에서 정확히 기대한 거절만 성공으로 판정했다.
        elif config["mode"] == "contracts":
            import unittest
            suite = unittest.defaultTestLoader.discover(str(source / "tests"), pattern="test_dependency_contract.py")
            code = 0 if unittest.TextTestRunner(verbosity=2).run(suite).wasSuccessful() else 1
        else:
            from dependency_native import verify
            print("NATIVE_RESULT=" + json.dumps(verify(source, site, reject), sort_keys=True))
    except Exception as exc:
        # 경로나 공급자 예외 본문을 CI 공개 로그에 옮기지 않는다.
        print("DEPENDENCY_FAILURE=" + type(exc).__name__)
        print("DEPENDENCY_CODE=" + failure_code(exc))
        code = 1
    print("GUARD_EVENTS=" + json.dumps(dict(blocked), sort_keys=True))
    print("GUARD_LOCATIONS=" + json.dumps(locations, sort_keys=True))
    return 91 if blocked else code


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_mutually_exclusive_group(required=True)
    modes.add_argument("--self-check", action="store_true")
    modes.add_argument("--environment-check", action="store_true")
    modes.add_argument("--contract-check", action="store_true")
    args = parser.parse_args(argv)
    checkout = Path(__file__).resolve().parents[1]
    prefix, site = package_location()
    run = Path(tempfile.mkdtemp(prefix="kimchi-deps-"))
    for name in ("source", "home", "home/local", "home/roaming", "tmp"):
        (run / name).mkdir(exist_ok=True, parents=True)
    hashes = {}
    for name in SOURCES:
        path = checkout / name
        no_links(path, checkout)
        payload = path.read_bytes()
        hashes[name] = hashlib.sha256(payload).hexdigest()
        target = run / "source" / name
        target.parent.mkdir(exist_ok=True, parents=True)
        target.write_bytes(payload)
    config = {"run": str(run), "checkout": str(checkout), "site": str(site), "prefix": str(prefix),
              "mode": "self" if args.self_check else "contracts" if args.contract_check else "native"}
    config_path = run / "config.json"
    config_path.write_text(json.dumps(config), encoding="utf-8")
    env = {k: v for k, v in os.environ.items() if k.upper() in {
        "SYSTEMROOT", "WINDIR", "COMSPEC", "SYSTEMDRIVE", "PATHEXT"}}
    env.update(HOME=str(run / "home"), USERPROFILE=str(run / "home"),
               APPDATA=str(run / "home/roaming"), LOCALAPPDATA=str(run / "home/local"),
               XDG_CACHE_HOME=str(run / "home/local"), XDG_CONFIG_HOME=str(run / "home"),
               TMP=str(run / "tmp"), TEMP=str(run / "tmp"), TMPDIR=str(run / "tmp"),
               PYTHONDONTWRITEBYTECODE="1", PYTHONNOUSERSITE="1", PYTHONUTF8="1", PYTHONTZPATH="",
               PATH=os.pathsep.join([str(Path(sys.executable).parent),
                    str(Path(env.get("SYSTEMROOT", "/")) / "System32")]) if os.name == "nt" else "/usr/bin:/bin",
               PIP_CONFIG_FILE=os.devnull, GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull)
    command = [sys.executable, "-I", "-S", "-B", "-X", "utf8",
               str(run / "source/scripts/dependency_smoke.py"), "--child", str(config_path)]
    try:
        result = subprocess.run(command, cwd=run / "source", env=env, timeout=120)
        code = result.returncode
    except subprocess.TimeoutExpired:
        print("DEPENDENCY_TIMEOUT")
        code = 124
    changed = [name for name, digest in hashes.items()
               if hashlib.sha256((checkout / name).read_bytes()).hexdigest() != digest]
    print("SOURCE_RESULT=" + json.dumps({"count": len(hashes), "changed": changed, "child_exit": code}))
    return 92 if changed else code


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--child":
        raise SystemExit(child(sys.argv[2]))
    raise SystemExit(main())
