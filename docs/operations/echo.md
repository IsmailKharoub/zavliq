# Optional Echo service

Echo is disabled by default. Its bootstrap and responder use separate Compose profiles. The bootstrap alone has access to the control database and administrator secret; the responder receives only its private identity/journal volume and the public HTTPS endpoint.

On the deployed HTTPS service, after the core stack is healthy:

```sh
bash infra/scripts/compose.sh --profile echo-setup run --rm echo-bootstrap
bash infra/scripts/compose.sh --profile echo up -d --no-build --wait echo
```

Use the deployment's existing `ZAVLIQ_ENVIRONMENT` and `ZAVLIQ_ENV_FILE` settings. Bootstrap provisions only the reserved `@echo:SERVER_NAME` account and persists the enrollment proof before enrollment; repeating it reuses the same identity. `/echo/runtime` holds private native state and `/echo/journal` holds the durable deduplication journal. Both belong to UID1000. The normal responder cannot read the administrator token or control database. Its healthcheck requires a matching public owner address and a successful loop within90seconds. Normal startup never opts into either profile.

Docker Echo requires its public homeserver/control origins to be reachable from the container and accepted by native URL validation. Use HTTPS for deployment. Localhost in a container addresses that container, and plaintext `host.docker.internal` is deliberately rejected by the runtime. Perform local checks using the host-native invocation in `services/echo/README.md`; do not weaken URL validation for a demo.

Verify an explicit invitation and exact text/JSON round trip with a separate ordinary identity. Only afterward build the website with `VITE_ECHO_USER_ID='@echo:YOUR_HOST'`. `build-release.sh` records that public address in its manifest. The variable is empty by default, so an unconfigured website makes no live Echo promise. Compiling the button does not provision or start the account.

Snapshots include the Echo volume when present and briefly pause an active responder alongside the other application writers. Fresh restore restores the private volume but leaves the responder disabled; verify its owner, retained state and deduplication before enabling it. Existing installations that enable Echo must explicitly include its profile when updating that container to a new release. Stop it with `compose.sh --profile echo stop echo`; preserve the volume so retries and budgets survive.

The isolated local container build and Echo-aware backup/restore passed on September 12, 2026. The encrypted backup paused writers for 2.216 seconds; the fresh recovery stack became healthy in 30.432 seconds. The restored identity, crypto store and processed-event journal survived, Echo stayed disabled until explicitly started, and the supervised responder then passed text/JSON, duplicate prevention and invitation-scope checks. See `services/echo/verification-2026-09-12.json` for the recorded evidence. AWS deployment of Echo and verification against the final release remain separate gates.
