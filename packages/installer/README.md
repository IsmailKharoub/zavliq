# Native installer

The installer accepts an explicit published GitHub release version, verifies the matching archive against that release's `SHA256SUMS`, and installs only the native executable. It never changes shell configuration, registers an account, or starts a background process. Replacing an existing executable requires `--force`.

Release availability is a launch gate. Until a public release exists, build from source using `crates/zavliq-runtime/README.md`; do not describe an unpublished download as usable.

Once the chosen version is published, download and inspect `install.sh` from that release or the versioned repository, then run:

```sh
sh install.sh --version v0.1.0
# Optional explicit destination:
sh install.sh --version v0.1.0 --install-dir "$HOME/.local/bin"
```

Supported artifacts:

- `zavliq-v0.1.0-aarch64-apple-darwin.tar.gz`: macOS 13 or newer on Apple Silicon.
- `zavliq-v0.1.0-x86_64-unknown-linux-gnu.tar.gz`: Linux x86_64 with glibc 2.35 or newer.

Substitute the selected release's version in each name. Each native archive contains `zavliq`, `LICENSE`, and `README.md`; `SHA256SUMS` contains one standard SHA-256 entry per release asset. The installer requires curl, tar, awk, grep, mktemp, install, and sha256sum or shasum. The download and manifest both come from the same HTTPS release; checksums detect corruption and inconsistent assets, and do not provide independent signature verification.

Use the source build for unsupported platforms.

## SDK, MCP, and skill installation

The release also supplies `zavliq-node-0.1.0.tar.gz`, a portable SDK/MCP package with vendored Zavliq npm tarballs and a lockfile for public dependencies. Verify its checksum before extraction. With Node.js 22 or newer, enter its extracted directory and run `npm ci --ignore-scripts --no-audit --no-fund`. The included README explains importing the SDK and configuring MCP using absolute paths. The native executable is installed separately; Rust and unpublished npm dependencies are not required. The included `.npmrc` bounds registry request timeouts and retries.

For Python 3.11 or newer, verify `zavliq-0.1.0-py3-none-any.whl` and install it into your application's virtual environment:

```sh
python -m pip install --no-deps ./zavliq-0.1.0-py3-none-any.whl
```

The Python wheel has no runtime dependencies. The separate `zavliq-client-0.1.0.tgz` and `zavliq-mcp-0.1.0.tgz` support existing Node applications: install both in one npm command, then preserve the application's resulting lockfile. The portable bundle supplies the release's tested dependency lock.

`skill.md` is an installable agent skill. Save it as `zavliq/SKILL.md` in your agent's supported skill directory after inspection; it supplies instructions, while the CLI or MCP provides the tools. Installation never registers an account or changes shell configuration.

Draft releases and private repositories require authenticated access such as `gh release download v0.1.0 --repo IsmailKharoub/zavliq`. The public curl installer cannot retrieve draft assets. An authenticated draft smoke test does not establish anonymous public availability.

## Build release assets

Use the pinned workspace dependencies (`pnpm install --frozen-lockfile`), Node 24.21.0, pnpm 10.32.1, and Python 3.12 with `build==1.3.0`, `setuptools==80.9.0`, and `wheel==0.45.1`, then run:

```sh
python3 packages/installer/package-release.py --version v0.1.0 --out dist/packages
```

The explicit version must match native, SDK, and MCP manifests. The output directory must not already contain release assets. The script builds TypeScript, uses `npm pack --ignore-scripts` on isolated copies, builds the wheel without dependency downloads, and copies `install.sh` and `skill.md`. It neither compiles Rust nor publishes packages. The release workflow adds native archives and computes the combined `SHA256SUMS`.

`node-package-lock.json` pins public dependency versions and integrity values. The script changes only the vendored Zavliq tarball references and checksums. Refresh this lock deliberately when MCP dependency requirements change, review the diff, and verify `npm ci` and the installed MCP tools again. Tar, gzip, and wheel timestamps derive from the source commit for repeatable artifacts.
