# Zavliq echo account

An opt-in onboarding check: invite the operator-owned echo address to a **standard DM**, send original text or JSON, and receive the same literal value as a reply. This is an automated service account, not an independent user or model. It does not execute messages, follow URLs, invoke tools, call models, download files, or send unsolicited invitations.

The responder is **disabled by default** and requires a provisioned runtime directory plus an exact expected owner address. Public signup reserves `echo`; the one-shot operator bootstrap below provisions that fixed account privately. It saves its recovery proof before enrollment and seeds its first device before the native runtime creates its encrypted local database. There is no public bootstrap endpoint. Do not enable the website's live demo until the responder is healthy and its round trip has been verified.

## Operator setup and supervision

Use the normal self-hosted HTTPS deployment and private environment from [the operations runbook](../../docs/operations/README.md). The main Synapse/control stack must already be healthy and the release must include the `zavliq-echo` image. Production uses the configured `ZAVLIQ_SERVER_NAME` and `ZAVLIQ_PUBLIC_URL`. The Compose profiles `echo-setup` and `echo` are both opt-in; an ordinary deployment starts neither.

From the repository root, on the host containing that deployment's private environment:

```sh
# Prepare the fixed @echo account and its durable first device once.
ZAVLIQ_ENVIRONMENT=production bash infra/scripts/compose.sh \
  --profile echo-setup run --rm echo-bootstrap

# Start the independently supervised responder only after setup succeeds.
ZAVLIQ_ENVIRONMENT=production bash infra/scripts/compose.sh \
  --profile echo up -d --wait --wait-timeout 120 echo

# Check its sanitized process health; stop without deleting its volume.
ZAVLIQ_ENVIRONMENT=production bash infra/scripts/compose.sh ps echo
ZAVLIQ_ENVIRONMENT=production bash infra/scripts/compose.sh stop echo
```

The bootstrap receives the existing control database, mounted administrator token and control encryption secret. `bootstrap.sh` reads those secrets privately, drops to UID 1000, and takes the same `runtime.lock` used by the native SDK. `bootstrap.mjs` registers only `@echo:SERVER_NAME`, as a non-admin account. The responder receives none of those administrative mounts or secrets; it runs as UID 1000 with only the `echo` volume, native binary and configured public URL.

That volume contains `/echo/runtime` (private identity, recovery proof, native inbox and encrypted Matrix store) and `/echo/journal` (cursor, deduplication proofs, quotas and sanitized health). Directories are mode 0700 and newly written private files are mode 0600. Setup retries preserve the original proof, device and token; it will not silently log a revoked device back in. An account already claimed outside this workflow is refused. Existing identities must match the configured account and origins exactly. If a device has published keys but its local crypto store is absent, setup requires a complete volume restore or explicit recovery to a new device. **Never copy only `identity.json`, delete it to retry, or run two independent crypto stores for the same device.**

`ZAVLIQ_ECHO_HOMESERVER_URL` and `ZAVLIQ_ECHO_CONTROL_URL` optionally override the responder's public endpoints; normally leave both unset. Account addresses still use `ZAVLIQ_SERVER_NAME`. The default bootstrap profile explicitly supplies `ZAVLIQ_ECHO_BOOTSTRAP_ENABLED=true`, `PUBLIC_HOMESERVER_URL` and `PUBLIC_CONTROL_URL`. Endpoints must use HTTPS; plaintext is accepted only for loopback host development. A container cannot reach the host gateway through its own `localhost`, and plaintext `host.docker.internal` is intentionally unsupported. Use the host-run command below for local fixtures; the Docker recipe is for reachable HTTPS deployments.

The supervisor restarts exited processes. `/echo/journal/health.json` contains only `status`, public `user_id` and Unix-seconds `updated_at`. A successful processing cycle marks it ready; failures mark it retrying, and shutdown marks it stopped. Docker health requires a matching owner, ready status and an update within 90 seconds. A stalled process therefore becomes unhealthy without an extra health HTTP endpoint. Monitor the container health as well as exit/restart events.

Back up the **whole** private echo volume together with the server/control state using the operations backup workflow; stop the responder while snapshotting its stores. Restore retains Echo as disabled until the operator verifies identity and health. Explicit account recovery or additional service devices use the normal native recovery/pairing workflows and must be reviewed before changing the supervised runtime. Bootstrap is only for the initial device and idempotent setup retry.

After a public HTTPS round trip succeeds from a separate user account, build the website with `VITE_ECHO_USER_ID=@echo:YOUR_SERVER_NAME` to show Try Echo. Leave that build argument empty until then. Account registration and website activation are separate operator steps.

## Run

Requires Python 3.11+, the current native `zavliq` binary, and the local Python SDK. From the repository root:

```sh
PYTHONPATH=services/echo:packages/client-python/src python3 -m zavliq_echo.service \
  --enabled \
  --owner '@echo:zavliq.com' \
  --data-dir /PRIVATE/zavliq-echo-runtime \
  --state-dir /PRIVATE/zavliq-echo-journal \
  --control-url https://zavliq.com
```

For an installed service, install both `packages/client-python` and `services/echo` into the same private Python environment and invoke `zavliq-echo`. The equivalent variables are `ZAVLIQ_ECHO_ENABLED=true`, `ZAVLIQ_ECHO_USER_ID`, `ZAVLIQ_DATA_DIR`, `ZAVLIQ_ECHO_STATE_DIR`, `ZAVLIQ_CONTROL_URL`, and `ZAVLIQ_BINARY`. The process does not initialize identities, print tokens, or inspect other account directories. Its identity must match `--owner` before it accepts requests. Use separate persistent private directories and a supervisor that restarts on failure. Both the native store and the responder journal refuse concurrent writers.

Only standard two-member DMs are processed. Stripped invitation state may omit its member summary; immutable DM metadata and the authenticated inviter are checked before joining, and an authoritative two-member count is required afterward. Missing joined-room counts pause processing without advancing the cursor. Groups, channels, encrypted rooms, files, edited messages, replies, self-authored events, invalid JSON, and text/JSON above the 24 KiB echo budget are ignored. Replying to an echo does not echo again: send a new original message for another check. Received content remains untrusted. Existing HTML is never copied into formatted output.

The responder uses the current runtime's `requests`/`conversations` metadata (`kind`, encryption state and member counts), full inbox pages, raw `data_json` transport and normal `send`/`acknowledge` operations. Exact JSON strings preserve decimals and large integers. Its replies relate to the original event. This service accepts invitations only after the sender explicitly invites its advertised address.

## Persistence and limits

Synapse and the native runtime retain messages. The responder's private SQLite journal stores its processed arrival cursor, stable transaction proofs and admission counters; it contains no message bodies or credentials. A SHA-256 transaction ID derived from the original event ID is saved before sending. Lost send/acknowledgement responses are retried with that same ID, including after a restart. The arrival cursor advances only after the reply is accepted and delivered acknowledgement completes, or an input is explicitly ignored. A repeated event ID from backfill is not echoed twice.

Normal server limits still apply. Additional durable demo limits are 20 replies/minute, 500 replies/day, 5 replies/minute and 50 replies/day per counterpart, 10 accepted invitations/minute, 100/day, and 1000 joined rooms total. A full global reply budget pauses the queue; excess input from one counterpart is ignored so it cannot block everyone else's demo. Counters use fixed UTC minute/day windows and survive restarts. Changing the journal directory resets application counters, so production must retain and back up the configured directory. The server independently retains its own quota counters.

Logs contain only readiness/retry events and the public service address. No exception strings, inbox content, credentials, pairing secrets or encryption keys are logged. Startup failures exit with a generic operational action. Standard DMs are readable by the service operator under the normal privacy policy; do not send secrets to a public echo demo.

## Verify

```sh
PYTHONPATH=services/echo:packages/client-python/src python3 -m unittest discover -s services/echo/tests -v
pnpm --filter @zavliq/control build
ZAVLIQ_TEST_BINARY=crates/zavliq-runtime/target/debug/zavliq \
  node --test services/echo/tests/bootstrap.test.mjs
PYTHONPATH=services/echo:packages/client-python/src python3 services/echo/tests/live_echo.py \
  --control-url http://localhost:8080 \
  --binary crates/zavliq-runtime/target/debug/zavliq
```

Bootstrap unit tests use an isolated temporary control database and mock Synapse. With `ZAVLIQ_TEST_BINARY`, an additional offline contract test verifies that the built native binary reads the seeded identity and exposes only public identity fields. The tests do not initialize a public account. The opt-in live test uses two fresh fixture identities on a service you operate. `--fixture-dir` can reuse only a private directory previously created by this test; each run creates fresh logical sends with distinct transaction IDs. It does not claim the reserved production address, activate a permanent service, or log credentials/content. Deployment and production reserved-account bootstrap are separate operator steps.
