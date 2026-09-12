# Preparing a public production bundle

`prepare_bundle.py` prepares artifacts only. It does not initialize production, activate containers, register Echo, configure TLS/DNS, publish a release, or change workflows. Its new manifest is deliberately incompatible with `infra/scripts/deploy-release.sh`; do not rename it or translate fields to make that script accept it. The separate `activate.py` path below requires explicit operator execution after its own review.

The inputs must be the operator-reviewed final stage bundle and its exact manifest SHA256, the matching draft Linux native archive, and a separately reviewed pin file for the two cached website base images. The frozen stage bundle records native archive/binary hashes but does not include the native archive itself, so the explicit native input is required. No native executable is compiled or run by this preparer.

The preparer verifies source and Docker archive hashes, the exact archived infrastructure file set, Linux native archive/binary hashes and workflow/source provenance, and Docker archive image IDs, tags, architecture, and Echo provenance labels. Docker's containerd store can report an OCI index digest as the image ID. The verifier binds each exact tag to that digest and follows the selected Linux amd64 manifest, config and layer descriptors, checking sizes, compressed hashes, unpacked layer hashes, and the legacy manifest mapping. Missing blobs are permitted only for unselected platforms or optional metadata; required runnable graphs must be complete. Classic archives retain config-ID verification. Synapse, control, PostgreSQL, and Echo retain their actual stage image IDs. Only the website is built, from the same source archive with `VITE_ECHO_USER_ID=@echo:zavliq.com`. Its generated Dockerfile changes only the two `FROM` references to the reviewed immutable base digests; the original archived source is retained unchanged.

Public source directories inside the extracted workspace are normalized to `0755`, including implicit parent directories. Regular-file modes, including executable bits, are preserved. The outer temporary workspace and output bundle remain private (`0700`). This prevents a restrictive operator umask from creating source directories that Docker would copy into images with inaccessible permissions for unprivileged users.

Use an exclusively assigned native Linux amd64 Docker daemon, its `default` context, and the `default` buildx builder with the `docker` driver. An ARM/emulated daemon, other builder driver, missing/mismatched base image, or conflicting existing stage/public tag fails the preparation. The helper never pulls images or bootstraps a builder. Its one website build passes `--pull=false`, and both bases use immutable digest references. The base images must already be cached on that same daemon. Website dependency installation still uses the archived pnpm lockfile and can require normal package-registry access; this is not an offline dependency build.

The separate base-pin input has this schema. Replace every placeholder with reviewed values; tags alone or the local cache alone are not proof of review. `digest_ref` is the immutable manifest reference that resolves to the given local amd64 image ID. Depending on the Docker image store, that ID can be a configuration or index digest; the two hashes need not be equal.

```json
{
  "schema": "zavliq.public-web-bases.v1",
  "architecture": "linux/amd64",
  "images": {
    "node": {
      "ref": "node:24.21.0-bookworm-slim",
      "id": "sha256:REVIEWED_AMD64_IMAGE_ID_SHA256",
      "digest_ref": "docker.io/library/node@sha256:REVIEWED_MANIFEST_SHA256"
    },
    "caddy": {
      "ref": "caddy:2.11.4-alpine",
      "id": "sha256:REVIEWED_AMD64_IMAGE_ID_SHA256",
      "digest_ref": "docker.io/library/caddy@sha256:REVIEWED_MANIFEST_SHA256"
    }
  }
}
```

Keep bundle inputs and output in an ignored operator directory, such as `infra/.local/public-bundles/`. On the separately authorized builder, use Python 3.12 or later:

```sh
python3 infra/production/prepare_bundle.py \
  --stage-bundle /operator/reviewed-final-stage \
  --stage-manifest-sha256 REVIEWED_STAGE_MANIFEST_SHA256 \
  --native-runtime /operator/reviewed-linux-runtime.tar.gz \
  --web-bases /operator/reviewed-web-bases.json \
  --web-bases-sha256 REVIEWED_WEB_BASES_FILE_SHA256 \
  --output /operator/new-public-bundle \
  --exclusive-builder
```

The command creates a distinct `zavliq-web:COMMIT-public-BUILD_INPUT_HASH` tag and refuses an existing one. It does not overwrite a stage tag. Inputs, output directory, and the Docker builder must remain exclusively assigned until completion; do not run concurrent tagging, image pruning, or builds. A failed attempt may leave its new local web image or partial output directory. Inspect those separately; the helper does not delete or reuse them automatically.

The output includes:

- `production-manifest.json`, written last, with source/native provenance, original stage manifest and image archive hashes, original and resulting image IDs, public configuration changes, base pins, preparer hash, and generated Dockerfile hash.
- `images.tar.gz`, containing the unchanged four backend images and the new public website image, with a verified archive hash.
- The original `source.tar.gz`, copied `infra/` tree, native archive, reviewed stage manifest, reviewed base-pin file, generated `public-web.Dockerfile`, and the exact preparer source.
- `compose.images.yaml`, which pins all application and helper services to exact local image IDs, sets `pull_policy: never`, and removes inherited build definitions with Compose's `!reset` tag. The future activation procedure must validate support for this tag, load the verified image archive, verify image IDs, and merge this overlay last with the archived base and production Compose files.

There is intentionally no `manifest.json` compatible with the existing deploy script. The new manifest sets `activation_supported: false`. It does not establish production namespace readiness or create `@echo:zavliq.com`; the public browser button is compiled for that address, so the separately approved activation must provision and verify the real production Echo identity before exposing it. The production namespace requires fresh private state and the canonical HTTPS origin; do not rename or clone stage identity stores into production. The new public web image also needs public TLS/browser/Echo checks even though the backend image IDs are unchanged.

Offline regression tests mock every subprocess, including Docker. They test hash, native provenance, source/archive, cached-base, architecture/builder, tag-collision and incomplete-output guards, plus the one website build command, resulting manifest, and exact image-pin overlay:

```sh
python3 -W error::ResourceWarning -m unittest discover -s infra/production/tests -v
```

These tests do not establish a successful Docker build or deployment. No existing application, stage, deployment, or workflow file is modified by this source addition.

## Explicit production activation

The production activation tool consumes this schema directly. The preparation manifest's `activation_supported: false` continues to describe the preparer's scope; it is not an authorization or a compatibility flag for the generic deploy script. An operator must separately review and execute the new activator. The tool never downloads a bundle, builds an image or native executable, pulls an image, changes DNS, applies Terraform, or publishes a release.

Install the reviewed `activate.py`, `prepare_bundle.py`, `website_state.py` and `website.py` as root-owned files that other users cannot modify, at `/opt/zavliq/production-tools/`. Transfer the complete bundle to `/opt/zavliq/releases/BUNDLE_ID`, where `BUNDLE_ID` is the 40-character source revision, `-public-`, and the first 16 characters of `public_web.configuration_sha256`. Adopt the transferred files as root and remove group/other write access before invoking activation. The entire bundle is checked for root ownership, regular files/directories, and protected permissions. Do not replace a previous bundle directory or image tag.

Keep the established host conventions: `/etc/zavliq` mode `0700`; its `compose.env`, `operations.env`, and four existing secret files mode `0600`; `/opt/zavliq/current` as the current release symlink. Initialize only a genuinely new production host with the existing `infra/scripts/init-environment.py`, `--production`, `--server-name zavliq.com`, and `--public-url https://zavliq.com`. The activator does not generate or overwrite identity secrets. `compose.env` must name `zavliq-production`, the canonical origin, and `/etc/zavliq`; first deployment must still have `ZAVLIQ_RELEASE=development`. Stage configuration, existing production containers/volumes/networks, an existing current marker, or unowned Zavliq systemd units block the first deployment.

`operations.env` must contain `ZAVLIQ_PUBLIC_URL=https://zavliq.com` and the operator's age **public** backup recipient. Optional environment/path entries must match production and the established `/etc/zavliq/compose.env` and `/var/backups/zavliq` locations. Keep the private age identity off the host. Domain permission, DNS/TLS readiness, the external backup role, production monitor, and SNS confirmation remain independent deployment prerequisites; the activator does not establish them.

For a reviewed fresh deployment:

```sh
sudo python3 /opt/zavliq/production-tools/activate.py activate \
  --bundle-id REVIEWED_BUNDLE_ID \
  --manifest-sha256 REVIEWED_PRODUCTION_MANIFEST_SHA256 \
  --expected-current-manifest-sha256 none
```

For an upgrade, replace `none` with the exact manifest SHA256 of the currently **successful** production deployment. The deployment lock serializes activations. The tool verifies source/native/image/infra provenance again, resolves every Compose profile with the exact-ID overlay last, and refuses inherited builds, pulls, stage volume names, unexpected ports, or backend egress. Only the gateway exposes 80/443. Cached tag collisions also fail before loading images.

An upgrade first stops the previously active schedules and takes the existing consistent encrypted backup. The adapter verifies the archived backup script's hash and the exact occurrence of its compose assignment and environment fallback, then changes only the compose executable path in a temporary copy. The verified production `ZAVLIQ_ENV_FILE` overrides the unchanged fallback. PostgreSQL dump and archive streams remain binary; they are not decoded or buffered by the Compose wrapper. Backup timeout sends TERM to the process group, allowing up to 15 seconds for the existing EXIT trap to resume writers, before a forced termination. A forced termination reports `BACKUP_TIMEOUT_CLEANUP_UNCONFIRMED` and requires inspection of paused containers; runtime/Docker unavailability can still prevent cleanup.

The transition writes an intent record before changing configuration or pointers. It stops the previous gateway/Echo, starts the pinned backend, explicitly provisions the operator-owned ordinary `@echo:zavliq.com` identity through the existing Echo bootstrap, and starts the pinned public gateway and worker. It checks exact running image IDs, container health, certificate-verified HTTPS, security headers and canonical discovery, then takes a post-activation encrypted backup. It preserves the prior release/configuration and records content-free evidence under `/var/lib/zavliq/deployments/`. This is a maintenance transition with a period of unavailability, not a zero-downtime rollout. Container health does not prove an actual Echo reply; independent messaging, browser, backup restore, off-host upload, alert delivery and launch gates remain outstanding until separately verified.

The activator installs the established health, backup and retention systemd units, plus production drop-ins for the reviewed operator tools. Backup and retention use the pinned wrapper. The health command requires a ready deployment and exact running image IDs for all five services, including a healthy Echo worker. It also checks canonical HTTPS/discovery, disk headroom and the timestamped successful local backup. A 35-second total deadline fits inside the service's 45-second limit; failures publish content-free results, while a missing/stale producer remains a monitoring failure. The public health JSON is readable even under the private operator umask. It verifies backup freshness and the checksum record's structure; activation/upload separately hash the archive. Schedules start only after activation and its local backup succeed. Routine operator commands are:

```sh
sudo bash /etc/zavliq/production-compose.sh --profile echo ps
sudo python3 /opt/zavliq/production-tools/activate.py backup
sudo python3 /opt/zavliq/production-tools/activate.py retention
sudo python3 /opt/zavliq/production-tools/activate.py health
```

Use this wrapper for production maintenance; the archived generic `compose.sh` omits the public image overlay. The wrapper verifies the active manifest, namespace, Compose inputs and overlay before use, refuses build/pull/down and configuration overrides, and always selects `zavliq-production`. It does not print private configuration or credential files.

## Failure and rollback limits

A backup failure before the transition keeps the old ready marker/configuration and restores previously active schedules. Once a transition may have touched state, a failure records `failed`, disables and stops owned timers, stops any maintenance services already triggered by those timers, and then stops writers while preserving images and volumes. This also keeps failed deployments from restarting scheduled work after a reboot. Failure to write the active marker does not skip those stop attempts; available evidence records `marker_write_incomplete`. Partial pointer/configuration failures remain operator recovery work. Inspect the evidence and actual container state; do not assume a failed stop completed. Running-image checks also reject paused containers even when Docker retains a cached healthy result.

There is no automatic rollback and no rollback subcommand. Ordinary activation refuses a failed/in-progress current marker and any previously attempted bundle ID, including a historical successful release. Neither this tool nor the generic `rollback.sh` should be used to infer database compatibility. Preserve previous artifacts and pre-update backups. A schema-compatible rollback needs a separately reviewed manual recovery procedure with the previous exact image overlay; incompatible or uncertain schemas require the separately reviewed fresh replacement procedure below, followed by original-identity and encrypted-history verification. Do not delete failed-attempt evidence, regenerate secrets, remove volumes, or clear markers to force a retry.

The offline suite uses real local bundle archives, the actual archived backup script and real restrictive-umask extraction; host tools and activation HTTPS are mocked. It covers successful first activation/upgrade, public image/network guards, binary backup streams, pointer collisions, pre-transition backup failure, partial configuration writes, persistent marker-write failure, and refusal to downgrade after a possible migration. A short real Bash subprocess test checks EXIT-trap execution on timeout. A separate local `docker compose config` check accepted all nine generated image IDs/build resets and expected network/port settings without creating containers. No production build, activation, account provisioning, cloud write or live rollback has been performed by these checks.

## Restore an existing public network on a fresh replacement host

`restore.py` is a separate, explicit recovery path. It imports the reviewed production verifier/activator helpers, restores the existing snapshot format, and adopts the restored state without calling ordinary activation or either identity-provisioning path. It never builds/pulls images, opens firewalls, changes DNS, starts clients, or accepts the primary age identity as a transport credential. Its host operations require separate operator authorization. The offline tests below are not a production recovery drill.

Use an exclusively assigned fresh native amd64 host and the default Docker context/daemon. The local project remains `zavliq-production`; it is isolated by a separate host, not a temporary project followed by volume renaming. The daemon must have no containers, volumes or custom networks. `/etc/zavliq` must contain only the reviewed private `operations.env`, with the original age **public** backup recipient and canonical public URL. Do **not** run environment initialization or start containers first. Existing current/active/previous pointers, unresolved transitions, restore attempts or Zavliq units fail preflight. Upload the matching protected public bundle and install all five reviewed tools (`restore.py`, `activate.py`, `prepare_bundle.py`, `website_state.py`, `website.py`) under `/opt/zavliq/production-tools/`.

The source must be an existing `zavliq.com` production snapshot. A `localhost` stage archive cannot enter this path. The public bundle must be the reviewed exact images that wrote the snapshot, including the original PostgreSQL image/major version. The archive must contain the original four secrets, signing key, service token, policy/control state, media, and complete Echo runtime/inbox/crypto store and journal. Recovery never provisions a replacement Echo device. Server backups do not contain other clients' E2EE keys.

### Prepare transport off AWS

Keep the primary age identity on the operator's trusted recovery machine, outside AWS. Verify the selected encrypted archive against its independently reviewed SHA before decrypting. Use a fresh private directory and a new age transport identity for this one transfer:

```sh
umask 077
# On the trusted OFF-HOST recovery machine only:
age --decrypt --identity /OFFLINE/PRIMARY_BACKUP.key \
  --output /OFFLINE/RECOVERY/snapshot.tar /OFFLINE/RECOVERY/original.tar.age
age-keygen -o /OFFLINE/RECOVERY/transport.key
age-keygen -y /OFFLINE/RECOVERY/transport.key > /OFFLINE/RECOVERY/transport-recipient.txt
age --encrypt --recipient "$(cat /OFFLINE/RECOVERY/transport-recipient.txt)" \
  --output /OFFLINE/RECOVERY/snapshot.transport.age /OFFLINE/RECOVERY/snapshot.tar
```

Record the original ciphertext, transport ciphertext and plaintext tar SHA256 and byte lengths in the input receipt below. Obtain the snapshot `created_at` from the decrypted archive on that trusted machine. Copy only the one-use **transport** key, transport ciphertext, protected receipt and reviewed public bundle to the replacement. Never copy the primary identity or the plaintext tar. Remove the trusted machine's temporary plaintext after transport verification under the operator's secure storage procedure.

The replacement checks the supplied transport identity's derived public recipient against the receipt and refuses the primary recipient or an unrelated key. It verifies transport bytes/hash, age decryption and plaintext bytes/hash before extraction or state creation. It deletes a matched transport key immediately after successful decryption, or on a decryption failure; an unrelated key is refused and left untouched. Its temporary plaintext is removed on exit. This does not promise forensic erasure, protection from an unclean host crash, or secure deletion on underlying storage. Inspect leftover private temporary material after interruption before reusing the replacement.

Input receipt schema (all placeholders must be replaced with operator-reviewed values):

```json
{
  "schema": "zavliq.production-restore-input.v1",
  "restore_id": "recovery-YYYYMMDD-UNIQUE",
  "target_machine_id": "32_LOWERCASE_HEX_FROM_REPLACEMENT_ETC_MACHINE_ID",
  "bundle_id": "REVIEWED_PUBLIC_BUNDLE_ID",
  "manifest_sha256": "REVIEWED_PUBLIC_MANIFEST_SHA256",
  "revision": "EXACT_SOURCE_REVISION",
  "images": "COPY_THE_EXACT_IMAGES_OBJECT_FROM_THE_REVIEWED_PUBLIC_MANIFEST",
  "server_name": "zavliq.com",
  "origin": "https://zavliq.com",
  "source_binding_reviewed": true,
  "primary_identity_off_host": true,
  "transport_key_single_use": true,
  "fresh_host_isolated": true,
  "primary_recipient": "ORIGINAL_AGE_PUBLIC_RECIPIENT",
  "transport_recipient": "NEW_DIFFERENT_AGE_PUBLIC_RECIPIENT",
  "original": {
    "name": "zavliq-YYYYMMDDTHHMMSSZ.tar.age",
    "created_at": "YYYY-MM-DDTHH:MM:SSZ",
    "sha256": "REVIEWED_ORIGINAL_CIPHERTEXT_SHA256",
    "bytes": 12345
  },
  "transport": {"sha256": "TRANSPORT_CIPHERTEXT_SHA256", "bytes": 12345},
  "plaintext": {"sha256": "EXACT_DECRYPTED_TAR_SHA256", "bytes": 12345}
}
```

Use a lowercase restore ID of 8–64 letters/digits/hyphens. `images` must be an object, not the explanatory placeholder string. These statements are **operator attestations**, not cryptographic proof of source history. Existing archives contain a source revision but omit the public manifest hash/image receipt; establish that association from retained deployment evidence and independently reviewed inputs. If the archive-to-manifest association cannot be established, stop. This tool does not change the backup format/uploader or infer the binding from an S3 timestamp. The tool verifies the transport/plaintext hashes locally; the original encrypted archive hash remains the separately verified off-host binding.

Keep the receipt and transport key root-owned `0600`, in protected private directories. Pin the receipt's SHA separately when invoking:

```sh
sudo python3 /opt/zavliq/production-tools/restore.py prepare \
  --restore-id REVIEWED_LOWERCASE_RESTORE_ID \
  --bundle-id REVIEWED_PUBLIC_BUNDLE_ID \
  --manifest-sha256 REVIEWED_PUBLIC_MANIFEST_SHA256 \
  --input-receipt /PRIVATE/input-receipt.json \
  --input-receipt-sha256 REVIEWED_INPUT_RECEIPT_SHA256 \
  --transport /PRIVATE/snapshot.transport.age \
  --transport-key /PRIVATE/transport.key
```

Preparation rejects unsafe/duplicate archive members, excessive expansion, missing original state, wrong namespace/revision, conflicting image tags and inadequate disk headroom. It restores only to empty managed volumes using exact images and network-disabled volume-copy helpers. It preserves private modes and known service ownership. PostgreSQL restores into the newly initialized database; Synapse's same-source config helper regenerates config from original secrets, and its existing bootstrap verifies the restored token. Gateway, Echo and all schedules remain stopped. No Echo-bootstrap entrypoint runs.

The durable record is `/var/lib/zavliq/deployments/restore-RESTORE_ID/restore.json`. A successful preparation records `restored_private`, retains an `activating` active marker and installs the existing pinned maintenance wrapper/units without enabling schedules. A failure after mutation records failure and attempts to stop writers independently of marker-write success; it never deletes volumes, downgrades images, clears evidence or retries a failed target automatically. A new attempt requires separate recovery review, including any leftover plaintext after abrupt interruption.

### Bring up the gateway and restored Echo for verification

The snapshot does **not** include Caddy certificates or ACME account state. There is no automatic verified pre-cutover HTTPS route. First perform private core/metadata checks, then explicitly review source fencing, target ingress restrictions, canonical A/AAAA routing and certificate acquisition. Do not expose the gateway during isolated core restoration.

One possible maintenance cutover is to fence all old writers/Echo, restrict replacement TCP 443 to verification operators and keep UDP 443 closed, point the canonical domain to the replacement, and permit TCP 80 for Caddy's HTTP-01 challenge/HTTPS redirect. Verify the external firewall rules before starting the gateway. That sequence requires separately authorized DNS/firewall changes and successful real ACME issuance. Public TCP/UDP 443 stays restricted until verification/adoption. Full trusted-client verification before changing DNS instead requires an independently supplied valid certificate and canonical-host routing procedure; neither is implemented here. Never bypass certificate validation or rewrite the original client's homeserver URL.

After those prerequisites, use the installed exact-pin route; do not use generic `compose.sh`, ordinary activation or Echo bootstrap:

```sh
sudo bash /etc/zavliq/production-compose.sh \
  up -d --no-build --no-deps --wait --wait-timeout 180 gateway

# Only after old Echo is stopped and this host's canonical HTTPS route reaches
# the replacement, including narrowly reviewed firewall access for the responder:
sudo bash /etc/zavliq/production-compose.sh --profile echo \
  up -d --no-build --no-deps --wait --wait-timeout 180 echo
sudo bash /etc/zavliq/production-compose.sh --profile echo ps
```

The wrapper always merges the exact image-ID overlay last with `pull_policy: never`; it disallows an added `--pull` argument. No new generic recovery Compose API is introduced. Close original verification devices on the old network before using their unchanged private stores against the canonical replacement. Verify retained E2EE text/JSON/file, new E2EE roundtrip, standard messaging/file, seeded membership/policy/quota state, restored Echo identity continuity and a controlled reply. Keep unseeded checks explicitly unverified. The same live crypto identity must never run simultaneously against the old and restored networks.

### Complete adoption

Hash the successful private restore record and make a separate root-owned `0600` verification receipt. It must use schema `zavliq.production-restore-verification.v1`, the exact `restore_id`, `restore_record_sha256`, `target_machine_id`, `manifest_sha256`, and `origin=https://zavliq.com`. Set integer Unix-second `verified_at` after preparation and `expires_at` at most one hour later. Its `operator_attestations` object requires each of these independently reviewed statements to be `true`:

- `source_writers_and_echo_fenced`, `canonical_route_to_replacement`, `public_https_restricted_to_verifiers`, `dns_tls_decisions_reviewed`
- `original_devices_used_without_origin_or_key_changes`, `old_e2ee_text_json_file_verified`, `new_e2ee_roundtrip_verified`, `standard_message_and_file_verified`
- `memberships_policy_quotas_verified`, `restored_echo_identity_and_reply_verified`, `no_identity_or_secret_regeneration`, `replacement_monitor_backup_access_reviewed`

Do not set a statement true to bypass a missing test. Store supporting sanitized evidence separately; no message bodies, credential values or private client/key file hashes belong in these receipts.

```sh
sudo python3 /opt/zavliq/production-tools/restore.py complete \
  --restore-id REVIEWED_LOWERCASE_RESTORE_ID \
  --expected-restore-record-sha256 REVIEWED_PRIVATE_RESTORE_RECORD_SHA256 \
  --verification-receipt /PRIVATE/verification.json \
  --verification-receipt-sha256 REVIEWED_VERIFICATION_RECEIPT_SHA256
```

Completion validates the current receipt/phase, re-verifies the complete public bundle and Compose configuration, checks actual running image IDs/health including Echo, checks certificate-verified canonical HTTPS/discovery/security headers, and takes a new encrypted backup using the production wrapper. Only then does it mark the restored deployment ready and enable existing health/backup/retention schedules. Actual local checks and external operator attestations are reported separately. It does not claim off-host upload succeeded or open public traffic. A failed completion stops writers/schedules and preserves failed state for review; it never restarts an older database image.

Before opening public traffic, independently verify backup transfer, monitor/alert delivery and replacement-host IAM/SSH/static-IP references, then remove temporary access/material. DNS caches, stale AAAA routing and certificate issuance/rate limits can dominate downtime; keep the old writers fenced. Record RPO and end-to-end RTO, including those delays. The private localhost tunnel drill did not establish public DNS/TLS recovery timing. Reverting DNS to the old database after new writes is not a safe rollback.

Focused tests use real local tar archives/private file lifecycles and mocked age, Docker, systemd and HTTPS:

```sh
python3 -W error::ResourceWarning -m unittest discover \
  -s infra/production/tests -p test_restore.py -v
```

No live replacement host, Docker daemon, cloud service, account, fixture or client store is exercised by that suite.
