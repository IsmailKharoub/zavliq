# Verification and release automation

The verification workflow builds and tests the website, control service, TypeScript/Python clients, MCP adapter, Synapse policy, and native runtime. It receives read-only repository access and no production credentials. Action dependencies are pinned to verified commit hashes; Node, pnpm, Rust, and Synapse versions are pinned.

Deployment and backup templates remain in `infra/github/` until the production environment is provisioned. Do not enable them before the AWS role, instance, repository environment, alerts, and restore drill have been verified.

Runtime release builds target Linux x64 on Ubuntu 22.04 (glibc 2.35) and Apple Silicon on macOS 15 with a macOS 13 deployment target. GitHub's [runner reference](https://docs.github.com/en/actions/reference/runners/github-hosted-runners) documents the hosted platforms. Release archives contain the executable, license, and README; a separate SHA256SUMS covers both archives. Existing tags and draft releases must match the source commit before assets can be replaced. A successful build is not a launch approval: the remaining gates in `docs/launch-requirements.md` still apply.
