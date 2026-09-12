# Zavliq SDK and MCP

This portable package contains the TypeScript/JavaScript SDK, the local MCP server, and a lockfile for their public npm dependencies. It requires Node.js 22 or newer and a separately installed, checksum-verified native `zavliq` binary. No Rust compiler or npm publication is required.

Verify this archive against the same release's `SHA256SUMS` before extracting it. Inside the extracted directory run:

```sh
npm ci --ignore-scripts --no-audit --no-fund
```

The included `.npmrc` limits registry request timeouts and retries. Installation downloads only the pinned public dependencies and never runs lifecycle scripts. The two Zavliq packages come from the included `vendor` archives. Keep this directory in a persistent location.

For JavaScript, create your application in this directory and import `Zavliq` from `@zavliq/client`. The package README under `node_modules/@zavliq/client/README.md` documents the client API. For a different application, install both `vendor/*.tgz` packages together; its own lockfile then controls dependency resolution.

For MCP, configure your agent with absolute paths:

```json
{
  "mcpServers": {
    "zavliq": {
      "command": "node",
      "args": ["/absolute/path/to/zavliq-node-VERSION/node_modules/@zavliq/mcp/src/index.mjs"],
      "env": {
        "ZAVLIQ_BINARY": "/absolute/path/to/zavliq",
        "ZAVLIQ_DATA_DIR": "/absolute/private/path/to/agent-identity"
      }
    }
  }
}
```

Replace `VERSION` with the selected release number. One identity directory supports one active runtime; use separate directories for separate agents. Credentials and encryption keys remain inside that private directory and must never be included in model prompts. Registration is a separate explicit tool call after installation.

Release assets can be public only after the operator publishes the release. Draft or private assets require authenticated GitHub access; successful authenticated installation is not evidence of anonymous public availability.
