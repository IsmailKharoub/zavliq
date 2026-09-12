# Anonymous installation readiness

`install_smoke.py` performs a fresh anonymous public-release download and local CLI, Python SDK, Node SDK and MCP readiness check. It creates no account, sends no message, and calls no model or production service. It does not satisfy the separate ten-model onboarding, messaging, recovery or soak gates. The published release and repository must actually be public before execution is authorized; a draft/private download failure remains a failed attempt.

The version is fixed to `v0.1.0`, source `2f76469b507c8745d4be5ef311f32774021143aa`, and reviewed SHA256SUMS digest `f81baae19d3bd9b5c32484afe2f225d509f769e2b77ffa73f995bf67d4887cf8`. There is no token argument, authenticated download fallback, alternate release, build or checkout package fallback. Native binary, Python module and MCP schema hashes are also pinned to the verified artifacts. Both platform results must come from real execution on their respective hosts; a mocked offline platform test is not installation evidence.

Prerequisites supplied by the operator:

- macOS 13+ on Apple Silicon, or Linux x86_64 with glibc 2.35+.
- Python 3.11+ with `venv` and bundled pip available; Node.js 22+ and npm.
- The installer's ordinary shell tools: curl, tar, awk, mktemp, install, and sha256sum or shasum.
- Outbound HTTPS to GitHub release assets and the public npm registry. No service account or credentials are needed.

The driver resolves the caller's existing `node` once and validates that its canonical directory also contains executable `npm`. Only that directory is added to the restricted child PATH; the caller's full PATH and environment are not inherited. `--node-tools-dir /absolute/canonical/bin` selects an explicit installation, including nvm. This is prerequisite discovery, not a downloaded Zavliq fallback.

Run with Python isolated mode. The driver and sibling `release.py` are the only verification source files needed on Linux; they are stdlib-only and never import the model harness. Transfer both reviewed files together and record their SHA256 hashes. No Zavliq application source is needed there.

After explicit publication/execution authorization, choose a new absolute install root whose parent already exists. Never reuse a failed root. For example, on the Mac from the repository:

```sh
python3 -I -B tests/onboarding/install_smoke.py run \
  --root /Users/ismailkharoub/Dev/ventures/zavliq/tests/onboarding/.local/anonymous-macos-v010-20260912 \
  --version v0.1.0 \
  --sha256sums-sha256 f81baae19d3bd9b5c32484afe2f225d509f769e2b77ffa73f995bf67d4887cf8 \
  --node-tools-dir /Users/ismailkharoub/.nvm/versions/node/v24.16.0/bin \
  --execute --release-ready
```

On a Linux x86_64 verification host, from the directory containing the same two driver files:

```sh
python3 -I -B install_smoke.py run \
  --root /var/tmp/zavliq-anonymous-linux-v010-20260912 \
  --version v0.1.0 \
  --sha256sums-sha256 f81baae19d3bd9b5c32484afe2f225d509f769e2b77ffa73f995bf67d4887cf8 \
  --execute --release-ready
```

Downloads and child processes share a five-minute deadline. The reviewed `release.py` helpers constrain HTTPS release redirects, download sizes, extraction and child process groups. The native installer verifies its second manifest download against the same pin. Each installation uses a fresh HOME, npm cache, user/global npm configuration, virtual environment and native destination. Python installs only the downloaded wheel with `--no-index --no-deps`; npm installs the portable bundle's locked dependencies with `--ignore-scripts`.

The isolated installed-wheel worker checks an empty local inbox and the expected `NOT_INITIALIZED` identity error. The Node probe performs the same SDK check, initializes the installed MCP server, verifies all 17 exact schemas and checks its identity error. Every native runtime is configured with the unused loopback origin `http://127.0.0.1:9`; only local operations are invoked. No `init`, pairing or send operation is invoked. The driver verifies absence of identity files and release of all three runtime locks before passing.

The new root retains downloads, installed packages, temporary runtime directories, private worker configuration and immutable `result.json` on success or failure. Standard output and `result.json` contain sanitized hashes, versions, readiness, duration and scope; errors omit subprocess output and exception details. Copy each result to the release evidence after review. `ok: true` means this platform's anonymous installation and local readiness passed, while `full_onboarding_gate_passed` and `production_recovery_gate_passed` remain false.

Source-only tests, with no downloads, package installs, native processes, accounts or model calls:

```sh
python3 -I -B -W error::ResourceWarning -m unittest discover \
  -s tests/onboarding -p 'test_install_smoke.py' -v
```
