#!/usr/bin/env python3
"""Build portable, pinned client assets. Does not build Rust or publish anything."""

import argparse
import base64
import gzip
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import tomllib

ROOT = Path(__file__).resolve().parents[2]
LOCK = Path(__file__).with_name("node-package-lock.json")


def command(args, *, cwd=ROOT, env=None):
    return subprocess.run(
        args, cwd=cwd, env=env, check=True, text=True,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=180,
    ).stdout.strip()


def write_json(path, data):
    path.write_text(json.dumps(data, indent=2) + "\n")


def archive(source, target, epoch):
    # The tar and gzip metadata are stable for a given source commit.
    with target.open("wb") as output:
        with gzip.GzipFile(filename="", fileobj=output, mode="wb", mtime=epoch) as compressed:
            with tarfile.open(fileobj=compressed, mode="w") as tar:
                for path in [source, *sorted(source.rglob("*"))]:
                    relative = Path(source.name) / path.relative_to(source)
                    info = tar.gettarinfo(str(path), arcname=str(relative))
                    info.uid = info.gid = 0
                    info.uname = info.gname = ""
                    info.mtime = epoch
                    info.mode = 0o755 if path.is_dir() else 0o644
                    if path.is_file():
                        with path.open("rb") as stream:
                            tar.addfile(info, stream)
                    else:
                        tar.addfile(info)


def make_lock(version, vendor, manifests):
    lock = json.loads(LOCK.read_text())
    original_dependencies = lock["packages"]["node_modules/@zavliq/mcp"]["dependencies"]
    for name, requirement in manifests["mcp"]["dependencies"].items():
        if name != "@zavliq/client" and original_dependencies.get(name) != requirement:
            raise ValueError("MCP dependencies changed: deliberately refresh node-package-lock.json first")
    if set(original_dependencies) != set(manifests["mcp"]["dependencies"]):
        raise ValueError("MCP dependency set changed: refresh node-package-lock.json first")
    dependencies = {}
    for name in ("client", "mcp"):
        filename = f"zavliq-{name}-{version}.tgz"
        dependencies[f"@zavliq/{name}"] = f"file:vendor/{filename}"
        entry = lock["packages"][f"node_modules/@zavliq/{name}"]
        entry["version"] = version
        entry["resolved"] = dependencies[f"@zavliq/{name}"]
        entry["integrity"] = "sha512-" + base64.b64encode(
            hashlib.sha512((vendor / filename).read_bytes()).digest()
        ).decode()
        if name == "mcp":
            entry["dependencies"] = manifests["mcp"]["dependencies"]
    package = {
        "name": "zavliq-agent-client-install", "private": True,
        "version": version, "type": "module", "engines": {"node": ">=22"},
        "dependencies": dependencies,
    }
    lock["version"] = version
    lock["packages"][""] = {key: value for key, value in package.items() if key not in ("private", "type")}
    return package, lock


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", required=True, help="Explicit vMAJOR.MINOR.PATCH release")
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args()
    if not re.fullmatch(r"v(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)", args.version):
        parser.error("version must be an explicit vMAJOR.MINOR.PATCH")
    version = args.version[1:]
    manifest_paths = {
        "client": ROOT / "packages/client-ts",
        "mcp": ROOT / "packages/mcp",
    }
    manifests = {name: json.loads((path / "package.json").read_text()) for name, path in manifest_paths.items()}
    python_version = tomllib.loads((ROOT / "packages/client-python/pyproject.toml").read_text())["project"]["version"]
    rust_version = tomllib.loads((ROOT / "crates/zavliq-runtime/Cargo.toml").read_text())["package"]["version"]
    if any(v != version for v in [python_version, rust_version, *(p["version"] for p in manifests.values())]):
        parser.error("release version must match all native, SDK, and MCP package versions")
    out = args.out.resolve()
    out.mkdir(parents=True, exist_ok=True)
    expected = [
        f"zavliq-node-{version}.tar.gz", f"zavliq-{version}-py3-none-any.whl",
        f"zavliq-client-{version}.tgz", f"zavliq-mcp-{version}.tgz", "install.sh", "skill.md",
    ]
    if any((out / name).exists() for name in expected):
        parser.error("output already contains release assets; select a fresh directory")
    epoch = int(command(["git", "log", "-1", "--format=%ct"]))
    env = {**os.environ, "SOURCE_DATE_EPOCH": str(epoch)}
    command(["pnpm", "--filter", "@zavliq/client", "build"], env=env)
    with tempfile.TemporaryDirectory(prefix="zavliq-package-") as temporary:
        staging = Path(temporary)
        bundle = staging / f"zavliq-node-{version}"
        vendor = bundle / "vendor"
        vendor.mkdir(parents=True)
        for name, source in manifest_paths.items():
            package = staging / name
            package.mkdir()
            manifest = manifests[name]
            if name == "mcp":
                manifest["dependencies"]["@zavliq/client"] = version
            write_json(package / "package.json", manifest)
            for filename in ["README.md", "LICENSE"]:
                shutil.copy2(source / filename, package / filename)
            folder = "dist" if name == "client" else "src"
            shutil.copytree(source / folder, package / folder)
            command(["npm", "pack", "--ignore-scripts", "--pack-destination", str(vendor)], cwd=package, env=env)
        package, lock = make_lock(version, vendor, manifests)
        write_json(bundle / "package.json", package)
        write_json(bundle / "package-lock.json", lock)
        shutil.copy2(ROOT / "LICENSE", bundle / "LICENSE")
        shutil.copy2(Path(__file__).with_name("node-README.md"), bundle / "README.md")
        (bundle / ".npmrc").write_text("ignore-scripts=true\naudit=false\nfund=false\nfetch-timeout=30000\nfetch-retries=1\nfetch-retry-mintimeout=1000\nfetch-retry-maxtimeout=2000\n")
        archive(bundle, out / f"zavliq-node-{version}.tar.gz", epoch)
        for path in sorted(vendor.glob("*.tgz")):
            shutil.copy2(path, out / path.name)
        python = staging / "python"
        python.mkdir()
        for filename in ["pyproject.toml", "README.md", "LICENSE"]:
            shutil.copy2(ROOT / "packages/client-python" / filename, python / filename)
        shutil.copytree(ROOT / "packages/client-python/src", python / "src", ignore=shutil.ignore_patterns("__pycache__", "*.egg-info"))
        command([sys.executable, "-m", "build", "--wheel", "--no-isolation", "--outdir", str(out)], cwd=python, env=env)
        shutil.copy2(Path(__file__).with_name("install.sh"), out / "install.sh")
        shutil.copy2(ROOT / "packages/skill/zavliq/SKILL.md", out / "skill.md")
    for name in expected:
        path = out / name
        if not path.is_file():
            raise RuntimeError(f"Missing expected release asset: {name}")
    print(json.dumps({"version": args.version, "assets": expected, "native_build": False}))


if __name__ == "__main__":
    try:
        main()
    except subprocess.CalledProcessError as error:
        print(error.stderr[-4000:], file=sys.stderr)
        raise SystemExit(error.returncode)
    except (ValueError, subprocess.TimeoutExpired) as error:
        raise SystemExit(str(error))
