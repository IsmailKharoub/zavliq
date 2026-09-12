# Final private-stage deployment

This procedure targets only the existing `zavliq-load` deployment: Matrix namespace `localhost`, remote gateway `127.0.0.1:19180`, and operator tunnel origin `http://localhost:28180`. It preserves `/etc/zavliq`, every managed volume and every existing identity. There is no DNS change, public application listener, instance resize or new builder. Root supplies the final commit and explicitly closes the timed workload before any build or service change.

## Immutable source and runtime provenance

The final commit must contain the artifact Dockerfile and deployment scripts before the draft workflow runs. On the operator checkout, prepare the exact full commit SHA, then attach the completed draft workflow's Linux artifact:

```sh
python3 infra/staging/release_bundle.py source \
  --revision FULL_FINAL_COMMIT_SHA --output /PRIVATE/final-source
python3 infra/staging/release_bundle.py attach-runtime \
  --source /PRIVATE/final-source --run-id SUCCESSFUL_DRAFT_WORKFLOW_RUN
```

The source command uses `git archive` of that exact commit, never the working tree. The attachment command verifies repository workflow path, successful completion and exact `head_sha`, then downloads only `runtime-x86_64-unknown-linux-gnu`. Archive and executable SHA-256 values enter the manifest. It refuses an artifact from another revision or a failed workflow. The existing workflow builds on Ubuntu 22.04; the Bookworm Echo image additionally executes `zavliq methods` to verify real loader compatibility.

Transfer the prepared source package through verified SSH. After the short diagnostic finishes, build the four application images serially on the existing amd64 host:

```sh
python3 /PATH/TO/REVIEWED/infra/staging/release_bundle.py build \
  --source /PRIVATE/final-source --output /opt/zavliq/releases/FULL_FINAL_COMMIT_SHA
```

Only the small image packaging steps run on Lightsail. Echo consumes the verified executable through `echo-artifact.Dockerfile`; no Rust compilation runs there. The PostgreSQL image must already be cached with the exact measured image ID, which is checked before any build. It is never pulled or retagged by this procedure. Existing application tags for the target revision cause an early refusal; resolve a partial failed build deliberately instead of silently replacing an immutable tag.

The bundle records source archive hash, image archive hash, all infrastructure file hashes, all five service image references/configuration IDs and the Linux runtime artifact provenance. The website is compiled with exactly `VITE_ECHO_USER_ID=@echo:localhost`. This prepared asset is not served until the operator identity and Echo worker are healthy. Record the final `manifest_sha256` output for the activation command.

Compilation on a credit-depleted 2-vCPU host has no reliable unmeasured duration estimate. Reusing the workflow artifact avoids the dominant native compilation cost. Record CPU/burst capacity before the next performance interval and let packaging finish first; do not resize the host or introduce an extra builder to mask the measured configuration.

## Activation and readiness

```sh
sudo python3 /opt/zavliq/releases/FULL_FINAL_COMMIT_SHA/infra/staging/deploy_bundle.py \
  --revision FULL_FINAL_COMMIT_SHA --manifest-sha256 REVIEWED_MANIFEST_SHA256 \
  --measurement-idle
```

Activation verifies the reviewed manifest and both archives before loading images. It preserves previous private configuration, release pointer and running image IDs under `/var/lib/zavliq/deployments/SHA/`; creates a consistent encrypted pre-release backup; temporarily stops backup/retention schedules; loads only the bundled images; and permits only the existing loopback gateway port. Timers are restored in `finally`. A failure retains the recovery evidence and does not automatically downgrade a database.

The existing stage admission file remains included. The Echo bootstrap uses the same already authorized staging admission ceiling, because the source database contains more than the production daily registration count. Published production limits do not change. Synapse's address login burst must remain restored to 10 during measurement.

Core services update first while the previous gateway stays running. Echo is provisioned idempotently and becomes healthy through that gateway before the new website is served. In this private HTTP namespace only, `compose.echo.yaml` gives Echo `network_mode: service:gateway` and loopback `http://localhost:80`; it does not broaden native URL trust. After switching the gateway, Echo is explicitly recreated so it joins the new gateway network namespace.

Readiness checks exact running image IDs, core health, Matrix discovery, Zavliq discovery, compiled website/CSP and absence of HSTS on HTTP localhost. Deployment evidence clearly leaves real peer messaging and recovery checks pending. Independently verify standard text/exact JSON to Echo, duplicate prevention and its group/E2EE invitation refusal using a separate ordinary fixture. Do not infer a message round trip from an HTTP health check.

The bundle's `native_runtime.binary_sha256` and deployed `echo_native_binary_sha256` describe the **Linux Echo executable**. The recovery fixture's `native_binary_sha256` describes its own operator-side executable; use that actual pinned binary hash rather than copying the Linux value.

## Recovery and same-image rollback drill

Prepare the two-identity E2EE fixture against the final source, following [e2ee-restore-fixture.md](e2ee-restore-fixture.md). Close both clients before the final encrypted snapshot. Restore to fresh `zavliq-recovery-REVISION_PREFIX` volumes at remote loopback19181, with the same source/image manifest and immutable server namespace. Temporarily route local28180 to the clone only while fixture clients are closed; record the operator route attestation. No hostnames or crypto stores are rewritten.

Run the rollback drill on that isolated clone, outside any load/soak interval. First record every image ID and prove healthy readiness. Stop the source Echo worker before enabling its restored copy; never run both copies of its private runtime concurrently. Stop the clone Echo worker before changing its gateway. Then apply a temporary gateway-only Compose override whose `command` is `["caddy", "version"]`. The gateway exits and the bounded HTTP health check must fail. This deliberately failed candidate uses the **same gateway image ID** and touches no database or credentials.

Remove that temporary override and recreate the clone gateway with its original full Compose configuration. Recreate the clone Echo worker against the restored gateway namespace, verify all image IDs are unchanged, and require healthy HTTP plus Echo health. The original E2EE fixture then verifies queued ciphertext, room keys, historical file bytes and new bidirectional messages; this validates retained state after rollback rather than merely checking a container exit code. Preserve failed-check and recovered-check timestamps. This is a same-image configuration rollback drill, not evidence that an older Synapse schema can safely read a newer database.

Stop the clone and its Echo worker after verification, restore local28180 routing to source19180, and resume only the source Echo worker. Preserve the fresh recovery volumes and evidence. The source user's ordinary data and the original failed local-load stores remain untouched. A real incompatible-schema rollback uses the pre-release encrypted snapshot into a fresh target, never a blind image downgrade.

## Starting the 24-hour clock

Start no marker during build, deployment, backup/restore, rollback or active repairs. Before the explicit `begin_soak.py --revision SHA` call, require:

- Exact final core and Echo image IDs healthy, the compiled website's Echo identity correct, and real messaging checks passed.
- Final-source recovery and same-image rollback evidence complete, source routing restored and the recovery clone stopped.
- A fresh verified encrypted off-host backup, healthy minute timer and independent CloudWatch metrics.
- At least **25 hours** remaining on the bounded upload capability; renew it first if necessary. Its initial expiry is September 14, 2026 at 03:11:36 UTC.
- No existing marker; preserve any previous failed marker/journal before deliberately selecting a new evidence directory.

The existing marker tool binds full commit tags and immutable image IDs, including Echo. The minute journal then requires 24 actual hours, at least 1,440 healthy samples and no gap over 90 seconds. Run the final 100-client/10-message-per-second/30-minute workload on this unchanged service set, avoiding snapshot writer pauses during that timed interval. SNS confirmation and actual alert receipt remain separately required; an `OK` alarm alone does not prove notification delivery. Public DNS/TLS, discovery and installation remain separate launch gates.
