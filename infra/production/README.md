# Preparing a public production bundle

`prepare_bundle.py` prepares artifacts only. It does not initialize production, activate containers, register Echo, configure TLS/DNS, publish a release, or change workflows. Its new manifest is deliberately incompatible with `infra/scripts/deploy-release.sh`; do not rename it or translate fields to make that script accept it. The separate `activate.py` path below requires explicit operator execution after its own review.

The inputs must be the operator-reviewed final stage bundle and its exact manifest SHA256, the matching draft Linux native archive, and a separately reviewed pin file for the two cached website base images. The frozen stage bundle records native archive/binary hashes but does not include the native archive itself, so the explicit native input is required. No native executable is compiled or run by this preparer.

The preparer verifies source and Docker archive hashes, the exact archived infrastructure file set, Linux native archive/binary hashes and workflow/source provenance, and Docker archive image configuration hashes, tags, architecture, and Echo provenance labels. Synapse, control, PostgreSQL, and Echo retain their actual stage image IDs. Only the website is built, from the same source archive with `VITE_ECHO_USER_ID=@echo:zavliq.com`. Its generated Dockerfile changes only the two `FROM` references to the reviewed immutable base digests; the original archived source is retained unchanged.

Public source directories inside the extracted workspace are normalized to `0755`, including implicit parent directories. Regular-file modes, including executable bits, are preserved. The outer temporary workspace and output bundle remain private (`0700`). This prevents a restrictive operator umask from creating source directories that Docker would copy into images with inaccessible permissions for unprivileged users.

Use an exclusively assigned native Linux amd64 Docker daemon, its `default` context, and the `default` buildx builder with the `docker` driver. An ARM/emulated daemon, other builder driver, missing/mismatched base image, or conflicting existing stage/public tag fails the preparation. The helper never pulls images or bootstraps a builder. Its one website build passes `--pull=false`, and both bases use immutable digest references. The base images must already be cached on that same daemon. Website dependency installation still uses the archived pnpm lockfile and can require normal package-registry access; this is not an offline dependency build.

The separate base-pin input has this schema. Replace every placeholder with reviewed values; tags alone or the local cache alone are not proof of review. `digest_ref` is the immutable manifest reference that resolves to the given local amd64 image configuration ID; the two hashes need not be equal.

```json
{
  "schema": "zavliq.public-web-bases.v1",
  "architecture": "linux/amd64",
  "images": {
    "node": {
      "ref": "node:24.21.0-bookworm-slim",
      "id": "sha256:REVIEWED_AMD64_IMAGE_CONFIG_SHA256",
      "digest_ref": "docker.io/library/node@sha256:REVIEWED_MANIFEST_SHA256"
    },
    "caddy": {
      "ref": "caddy:2.11.4-alpine",
      "id": "sha256:REVIEWED_AMD64_IMAGE_CONFIG_SHA256",
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

Install the reviewed `activate.py` and `prepare_bundle.py` as root-owned files that other users cannot modify, at `/opt/zavliq/production-tools/`. Transfer the complete bundle to `/opt/zavliq/releases/BUNDLE_ID`, where `BUNDLE_ID` is the 40-character source revision, `-public-`, and the first 16 characters of `public_web.configuration_sha256`. Adopt the transferred files as root and remove group/other write access before invoking activation. The entire bundle is checked for root ownership, regular files/directories, and protected permissions. Do not replace a previous bundle directory or image tag.

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

The activator installs the established health, backup and retention systemd units, plus production drop-ins that route backup and retention through the new pinned wrapper. It enables schedules only after activation and its local backup succeed. Routine operator commands are:

```sh
sudo bash /etc/zavliq/production-compose.sh --profile echo ps
sudo python3 /opt/zavliq/production-tools/activate.py backup
sudo python3 /opt/zavliq/production-tools/activate.py retention
```

Use this wrapper for production maintenance; the archived generic `compose.sh` omits the public image overlay. The wrapper verifies the active manifest, namespace, Compose inputs and overlay before use, refuses build/pull/down and configuration overrides, and always selects `zavliq-production`. It does not print private configuration or credential files.

## Failure and rollback limits

A backup failure before the transition keeps the old ready marker/configuration and restores previously active schedules. Once a transition may have touched state, a failure records `failed` and attempts to stop schedules and writers while preserving images and volumes. Failure to write the active marker does not skip those stop attempts; available evidence records `marker_write_incomplete`. Partial pointer/configuration failures remain operator recovery work. Inspect the evidence and actual container state; do not assume a failed stop completed.

There is no automatic rollback and no rollback subcommand. Ordinary activation refuses a failed/in-progress current marker and any previously attempted bundle ID, including a historical successful release. Neither this tool nor the generic `rollback.sh` should be used to infer database compatibility. Preserve previous artifacts and pre-update backups. A schema-compatible rollback needs a separately reviewed manual recovery procedure with the previous exact image overlay; incompatible or uncertain schemas require restoration into a fresh isolated target followed by original-identity and encrypted-history verification. Do not delete failed-attempt evidence, regenerate secrets, remove volumes, or clear markers to force a retry.

The offline suite uses real local bundle archives, the actual archived backup script and real restrictive-umask extraction; host tools and activation HTTPS are mocked. It covers successful first activation/upgrade, public image/network guards, binary backup streams, pointer collisions, pre-transition backup failure, partial configuration writes, persistent marker-write failure, and refusal to downgrade after a possible migration. A short real Bash subprocess test checks EXIT-trap execution on timeout. A separate local `docker compose config` check accepted all nine generated image IDs/build resets and expected network/port settings without creating containers. No production build, activation, account provisioning, cloud write or live rollback has been performed by these checks.
