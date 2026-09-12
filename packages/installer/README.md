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

Substitute the selected release's version in each name. Each archive contains `zavliq`, `LICENSE`, and `README.md`; `SHA256SUMS` contains one standard SHA-256 entry per archive. The installer requires curl, tar, awk, grep, mktemp, install, and sha256sum or shasum. The download and manifest both come from the same HTTPS release; checksums detect corruption and inconsistent assets, and do not provide independent signature verification.

Use the source build for unsupported platforms. No npm publication is required for the native CLI. TypeScript, Python, MCP, and skill packages live in the public source tree until their own distribution releases are published.
