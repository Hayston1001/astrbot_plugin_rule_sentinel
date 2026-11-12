"""Build an AstrBot upload archive from the current working-tree files."""

import argparse
import io
import os
import re
import sys
import tempfile
import zipfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MAX_ARCHIVE_BYTES = 16 * 1024 * 1024
ROOT_FILES = ("main.py", "metadata.yaml", "_conf_schema.json", "README.md", "CHANGELOG.md", "LICENSE", "logo.png")
TOOL_FILES = ("scripts/evaluate_dry_run.py", "scripts/compare_dry_run.py")
VIEW_SUFFIXES = {".html", ".js", ".css", ".json", ".svg", ".png", ".jpg", ".jpeg", ".webp", ".ico", ".woff", ".woff2"}
IGNORED_PARTS = {"__pycache__", "tests", "node_modules"}


def _safe_file(root: Path, relative: Path) -> Path:
    source = root / relative
    # Refuse symbolic links rather than copying content from outside the tree.
    if any((root / Path(*relative.parts[:index])).is_symlink() for index in range(1, len(relative.parts) + 1)):
        raise ValueError(f"symbolic links are not allowed: {relative.as_posix()}")
    if not source.is_file():
        raise ValueError(f"required file is missing: {relative.as_posix()}")
    return source


def archive_files(root: Path) -> list[Path]:
    root = root.resolve()
    required = [Path(name) for name in (*ROOT_FILES, *TOOL_FILES,
                "sentinel/__init__.py", "views/management/index.html",
                "views/management/app.js", "views/management/style.css",
                "views/management/motion.js", "views/management/select.js")]
    for name in required:
        _safe_file(root, name)
    files = set(required)
    for directory, suffixes in (("sentinel", {".py"}), ("views/management", VIEW_SUFFIXES)):
        base = root / directory
        for source in base.rglob("*"):
            relative = source.relative_to(root)
            if any(part in IGNORED_PARTS or part.startswith(".") for part in relative.parts):
                continue
            if source.suffix.lower() not in suffixes:
                continue
            if source.is_symlink() or source.is_file():
                _safe_file(root, relative)
                files.add(relative)
    return sorted(files, key=lambda path: path.as_posix())


def build_archive(root: Path = ROOT) -> bytes:
    root = root.resolve()
    files = archive_files(root)
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for relative in files:
            source = _safe_file(root, relative)
            info = zipfile.ZipInfo(relative.as_posix(), date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o100644 << 16
            archive.writestr(info, source.read_bytes(), compress_type=zipfile.ZIP_DEFLATED, compresslevel=9)
    result = buffer.getvalue()
    if len(result) > MAX_ARCHIVE_BYTES:
        raise ValueError("archive exceeds the 16 MiB limit")
    return result


def default_output(data: bytes, root: Path = ROOT) -> Path:
    # Read the packaged metadata so the filename matches the archive contents.
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        metadata = archive.read("metadata.yaml").decode("utf-8")
    versions = re.findall(r"(?m)^version\s*:\s*([^\r\n]*)", metadata)
    if len(versions) != 1:
        raise ValueError("metadata.yaml must contain exactly one version")
    value = versions[0].strip()
    if len(value) >= 2 and value[0] in "\"'" and value[-1] == value[0]:
        value = value[1:-1]
    if not re.fullmatch(r"[0-9]+(?:\.[0-9A-Za-z]+)*(?:[-+][0-9A-Za-z]+(?:[.-][0-9A-Za-z]+)*)?", value):
        raise ValueError("metadata.yaml contains an invalid version")
    return root / "dist" / f"astrbot_plugin_rule_sentinel-{value}.zip"


def write_archive(output: Path, data: bytes, *, force: bool = False) -> None:
    if output.suffix.lower() != ".zip":
        raise ValueError("output filename must end in .zip")
    output.parent.mkdir(parents=True, exist_ok=True)
    if not force:
        # Exclusive creation preserves existing user files, including symlinks.
        with output.open("xb") as target:
            try:
                target.write(data)
            except BaseException:
                target.close()
                output.unlink(missing_ok=True)
                raise
        return
    # Force replacement happens only after the new archive is fully written.
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=output.parent, prefix=".plugin-package-", suffix=".tmp", delete=False) as target:
            temporary = Path(target.name)
            target.write(data)
        os.replace(temporary, output)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Build an AstrBot plugin ZIP from the current working tree")
    parser.add_argument("--output", type=Path, help="output ZIP path")
    parser.add_argument("--force", action="store_true", help="replace an existing output file")
    args = parser.parse_args(argv)
    try:
        data = build_archive()
        output = args.output if args.output is not None else default_output(data)
        write_archive(output, data, force=args.force)
    except FileExistsError:
        print("Output already exists; choose --output or explicitly use --force.", file=sys.stderr)
        return 2
    except (OSError, ValueError, zipfile.BadZipFile) as error:
        print(f"Packaging failed: {error}", file=sys.stderr)
        return 2
    print(f"Built {output.resolve()} ({len(data)} bytes).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
