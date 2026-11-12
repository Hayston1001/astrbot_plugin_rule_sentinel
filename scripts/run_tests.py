"""Run the test suite against PyPI AstrBot versions in isolated uv environments."""

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_ASTRBOT_SPECS = ("astrbot==4.29.3", "astrbot")
NODE_FILES = (Path("views/management/app.js"), Path("views/management/motion.js"), Path("views/management/select.js"))


def _run(command, env):
    print("+ " + " ".join(str(part) for part in command), flush=True)
    subprocess.run(command, cwd=ROOT, env=env, check=True)


def _astrbot_version(uv, python_version, spec, env):
    code = "import importlib.metadata as m; print(m.version('astrbot'))"
    completed = subprocess.run(
        [uv, "run", "--isolated", "--python", python_version, "--with", spec,
         "python", "-c", code],
        cwd=ROOT, env=env, check=True, capture_output=True, text=True)
    return completed.stdout.strip()


def _check_node(env, skip):
    if skip:
        return
    node = shutil.which("node")
    if node is None:
        raise SystemExit("Node.js is required for the page tests; install it or use --skip-node.")
    for relative in NODE_FILES:
        _run([node, "--check", str(relative)], env)


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Run the tests with isolated PyPI AstrBot environments.")
    parser.add_argument(
        "--astrbot", action="append", default=None,
        help="PyPI package spec to test; repeatable. Defaults to 4.29.3 and the latest release.")
    parser.add_argument(
        "--python-version", default="3.12",
        help="Python version requested from uv (default: 3.12).")
    parser.add_argument(
        "--skip-node", action="store_true",
        help="Skip the page script syntax checks.")
    args = parser.parse_args(argv)

    uv = shutil.which("uv")
    if uv is None:
        print("uv is required for the isolated test environments.", file=sys.stderr)
        return 1

    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    env["PYTHONDONTWRITEBYTECODE"] = "1"

    try:
        _check_node(env, args.skip_node)
        for spec in args.astrbot or DEFAULT_ASTRBOT_SPECS:
            label = "latest" if spec == "astrbot" else spec
            print(f"\n=== AstrBot {label} ===", flush=True)
            print(f"AstrBot {_astrbot_version(uv, args.python_version, spec, env)}", flush=True)
            _run(
                [uv, "run", "--isolated", "--python", args.python_version,
                 "--with", spec, "python", "-B", "-m", "unittest",
                 "discover", "-s", "tests", "-v"],
                env)
    except subprocess.CalledProcessError as error:
        return error.returncode or 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
