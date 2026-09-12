# Zavliq operations

Zavliq runs one private Matrix network. The public boundary is Caddy: clients reach the control API and approved Matrix endpoints over HTTPS. PostgreSQL, Synapse administration and the service access token have no public listeners. Production backend containers have no external network; federation, URL previews and push delivery are disabled. The local override permits loopback debugging and container egress.

## Local stack

Requirements: Docker Engine with Compose v2, Python 3, and the committed pnpm lockfile. Commands below run from the repository root.

```sh
python3 infra/scripts/init-environment.py
bash infra/scripts/compose.sh up -d --build --wait
python3 infra/scripts/healthcheck.py --origin http://localhost:8080
```

Initialization writes random secrets into ignored `infra/.local` with private permissions and refuses to overwrite an existing identity. On subsequent starts, omit initialization. The full service is `http://localhost:8080`; direct development endpoints are Matrix `http://localhost:8008` and control `http://localhost:3001`. Use `bash infra/scripts/compose.sh down` to stop without deleting data. Never use `down -v` on retained identities.

For incremental backend development: `bash infra/scripts/compose.sh up -d --build synapse bootstrap control`. Build `synapse-config` when policy code changes because Synapse copies that code into its image. The local override permits 30 registrations per IP per hour and 300 globally per day for multi-client trials; production keeps the control service defaults of three per IP per hour and 50 globally per day. Do not print `docker inspect` environments, full config files, secret files, or account responses when collecting diagnostic evidence.

## Hosting and credentials

Terraform in `infra/terraform` provisions one Lightsail 4 GiB Ubuntu instance, its static IPv4 address and firewall, an optional DNS A record in an existing hosted zone, private S3 backups, GitHub OIDC role, a five-minute external Lambda probe, and CloudWatch/SNS alerts. The shared account budget and $50/$75/$90 spending notifications are managed separately by `infra/terraform-notifications`. The instance bundle is `medium_3_0` in `us-east-1`. Monthly operating target is under $100; traffic and storage can exceed this target and budgets are alerts, not spending caps. A distinctly named, retained recovery host can be added through the explicit [recovery-host procedure](recovery-host.md); the original remains selected by default.

Review `example.tfvars`, fill the deployment's existing IDs, run `terraform init`, `terraform plan -var-file=production.tfvars`, and apply the reviewed plan. The module does not register a domain or silently create an organization-wide GitHub OIDC provider. Back up Terraform state securely and lock it for shared use before multiple operators apply. Destruction protection is enabled on the instance and backup bucket. The initial application remains private until DNS/TLS, registration limits and release checks are verified.

Read `GET /repos/OWNER/REPOSITORY/actions/oidc/customization/sub` before planning the deployment role. Set `github_oidc_subject_prefix` to its exact immutable `sub_claim_prefix`, including the owner and repository IDs; the module appends the selected environment. Zavliq's API setting was verified on September 12, 2026 as `repo:IsmailKharoub@186727527/zavliq@1366801339`. A legacy name-only trust rule does not match this repository. Terraform validates that the prefix belongs to `github_repository`; it does not switch GitHub's claim mode or broaden the trust policy. Recheck the API setting before applying a saved plan. See [GitHub's immutable subject change](https://github.blog/changelog/2026-04-23-immutable-subject-claims-for-github-actions-oidc-tokens/).

Cloud-init installs Docker, Compose, age, Python, unattended security updates and a 2 GiB swapfile. It contains no application or AWS secrets. After uploading and reviewing the first public bundle, initialize only a genuinely new production host:

```sh
sudo python3 /opt/zavliq/releases/BUNDLE_ID/infra/scripts/init-environment.py \
  --directory /etc/zavliq --production \
  --server-name zavliq.com --public-url https://zavliq.com
```

`server_name` is part of every Matrix address and must never change for an existing network. Public production starts a fresh `zavliq-production` project with `server_name=zavliq.com`, `https://zavliq.com`, new secrets and empty managed volumes. The private `localhost` stage keeps its own configuration, credentials and state; do not rename its namespace or copy its identity volumes into public production. Reusing the reviewed backend image bytes does not reuse stage state. All application credentials live in `/etc/zavliq` and private Docker volumes. Synapse's signing key, service token, PostgreSQL password and control encryption key are included in the encrypted backup. End-to-end keys remain with clients and are **not** recoverable from server backups.

Caddy trusts no incoming forwarded headers by default. Control trusts the isolated Docker network because only Caddy can reach it publicly; the local direct ports are for development. Upload routes pass through control for per-identity quota enforcement. Exposing Synapse directly in production bypasses that upload boundary and is unsupported. Synapse's directory search is available to authenticated clients, while the mandatory policy module restricts public listing to channels; private groups and DMs cannot be published.

Creation-time media expiry is enforced every five minutes by the maintenance job and control's upload bookkeeping. Synapse's native retention uses last access time and is only a supplementary cleanup. The maintenance job reads expired local media IDs from PostgreSQL and uses Synapse's administrative deletion API; it never edits Synapse tables directly. It processes up to 1,000 files per run, so monitor backlog before increasing admission limits. Quota reservations stay charged until control confirms deletion. The published thirty-day policy is a retention target with periodic purge delay, not a claim of deletion at an exact millisecond.

## Release and rollback

Use the separate [production bundle and activation procedure](../../infra/production/README.md). `prepare_bundle.py` verifies the reviewed final stage bundle and native archive, preserves the exact Synapse/control/PostgreSQL/Echo images, and builds only the public website from that same source with `@echo:zavliq.com`. Reviewed cached bases, a native amd64 builder and a distinct public web tag are required. The resulting `production-manifest.json` and exact image-ID overlay are a different schema from the legacy release scripts. `build-release.sh`, `deploy-release.sh` and `rollback.sh` do not prepare, activate or safely downgrade these bundles; do not rename manifests or translate fields to force compatibility.

Install the separately reviewed, root-owned tools at `/opt/zavliq/production-tools/`, and transfer the complete bundle to `/opt/zavliq/releases/BUNDLE_ID`, where the ID is `REVISION-public-CONFIG_HASH_PREFIX` as defined in the production procedure. Adopt bundle ownership as root and remove group/other write access. Keep `/etc/zavliq` private and reuse its initialized secret files. For the first deployment:

```sh
sudo python3 /opt/zavliq/production-tools/activate.py activate \
  --bundle-id REVIEWED_BUNDLE_ID \
  --manifest-sha256 REVIEWED_PRODUCTION_MANIFEST_SHA256 \
  --expected-current-manifest-sha256 none
```

An upgrade supplies the exact currently successful production manifest hash instead of `none`. Activation verifies the namespace, archives, image IDs and fully resolved Compose configuration before starting containers. It takes an encrypted pre-upgrade backup, preserves the previous release/configuration, checks the public TLS/discovery endpoints and Echo container, and takes a post-activation backup. It performs no image/native build or pull on the deployment host. The transition includes downtime and does not itself prove messaging, off-host backup delivery or public launch readiness.

The templates in `infra/github` remain inactive until separately installed by the release owner. The deployment template is manual and consumes a pre-uploaded reviewed bundle ID, manifest hash and expected-current hash; it does not rebuild artifacts. Configure the protected GitHub `production` environment with `AWS_ACCOUNT_ID`, `AWS_DEPLOY_ROLE_ARN`, `LIGHTSAIL_INSTANCE`, `LIGHTSAIL_INSTANCE_ARN` and `BACKUP_BUCKET`. The trust policy is tied to that exact repository/environment. Workflows obtain temporary AWS credentials and a temporary Lightsail SSH certificate, validate the exact instance identity and AWS-supplied host keys, temporarily allow only the runner's IPv4 /32, then close that journaled host's rule and erase the key. No permanent SSH key or AWS key is stored in GitHub or on Lightsail. See [CI and host-access procedures](ci.md) before enabling either template.

On failure, inspect `/var/lib/zavliq/deployments/` evidence, `/etc/zavliq/production-active.json`, content-free health and service state. A failure before the transition keeps the old configuration and restores previously active schedules. Once state may have changed, the activator records failure and attempts to stop writers and schedules; it never automatically starts older database images. A failed stop or marker write requires inspection of the actual host state. Keep these recovery cases distinct:

- **Same-image configuration recovery:** correct the identified host/configuration problem while preserving the exact current image IDs, Matrix namespace, secret files and volumes. Reconcile any partially written pointer/configuration/marker through an explicitly reviewed operator procedure. The activator has no automatic retry/resume command; do not clear evidence or mark a failed deployment ready to bypass its guards.
- **Schema-compatible rollback:** first establish that the selected historical images can safely use the current database schemas. A separate reviewed manual procedure must select their exact image overlay and consistent prior configuration. Normal activation refuses previously attempted bundle IDs, and the legacy `rollback.sh` is not compatible with this production schema.
- **Fresh-target restore:** if schemas are incompatible or compatibility is uncertain, use the [production restore tool](../../infra/production/README.md#restore-an-existing-public-network-on-a-fresh-replacement-host) with the reviewed pre-release snapshot and exact matching public bundle. Isolation comes from a fresh replacement host/daemon; the project remains `zavliq-production` and the original namespace/origin remains `zavliq.com`. Keep traffic restricted until original identities, retained messages/attachments and encrypted history pass verification. A `localhost` stage backup cannot become the public network. Keep the primary age identity off AWS; transfer only a single-use re-encrypted archive and its transport key. The generic restore script is not compatible with the public image overlay.

The [backup and recovery runbook](backup-restore.md) describes the snapshot contents and verification requirements. Recovery is complete only after independent clients confirm messaging, retained attachments and encrypted-history access. Never remove previous images/releases, pre-update backups or failed-attempt evidence to force a retry; prune deliberately only after recovery checks and backup requirements are met.

## Monitoring and incident response

Create `/etc/zavliq/operations.env` mode 0600 with `ZAVLIQ_PUBLIC_URL=https://zavliq.com` and `ZAVLIQ_BACKUP_RECIPIENT=age1...` (the public recipient only). Successful production activation installs the established systemd units plus the required backup/retention drop-ins, then enables schedules. The drop-ins select the exact public image overlay; use them instead of separately installing or invoking the legacy unpinned maintenance path. Health checks run every minute, check HTTP, Matrix discovery, 80% disk usage and local backup freshness, and publish content-free JSON at `/_zavliq/health`.

```sh
sudo bash /etc/zavliq/production-compose.sh --profile echo ps
sudo python3 /opt/zavliq/production-tools/activate.py backup
sudo python3 /opt/zavliq/production-tools/activate.py retention
```

The backup command retains the established writer-pause, PostgreSQL dump, volume snapshot and age encryption algorithm while adapting its compose invocation to the verified production wrapper. Two daily snapshots provide scheduling margin around the 24-hour RPO. A successful local archive is not proof of S3 upload or recoverability; verify the separate off-host workflow and fresh-target recovery drill. CloudWatch alarms fire after two missed/failed five-minute probes, including absent host telemetry and absent/failing S3 backups. Confirm the SNS email subscription and perform an actual test alarm; unconfirmed email subscriptions do not deliver alerts.

Investigate in this order: external probe and TLS, the production wrapper's service state, disk space, recent deploy, database health, then service-specific errors. A registration pause or other control configuration change needs an explicitly reviewed change that preserves image/data identity; do not inject unreviewed Compose overrides into the pinned wrapper. Escalate if online delivery p95 exceeds two seconds or pending acknowledgements rise. CPU/load and actual delivery-lag measurement must also be reviewed during load testing; an HTTP 200 alone does not prove message delivery.

Do not enable request/body/access-token logging. Synapse access logs and HTTP client logs are suppressed below warning; Docker logs rotate at three 10 MiB files per service. Caddy has no request access log. Operator exports and support reports must exclude tokens, pairing secrets, encrypted key material and private messages.

## Verification status and launch evidence

Configuration validation, local health and tests are useful evidence but do not prove a public launch. Before release, record the real endpoint, commit, timestamp, load distribution/p95/loss results, a restored backup identity test, a schema-compatible rollback test, alert delivery, certificate renewal, and the 24-hour soak. The broader product gates remain in `docs/launch-requirements.md`; this runbook does not waive any of them.

Upstream references: [Synapse configuration](https://element-hq.github.io/synapse/latest/usage/configuration/config_documentation.html), [Synapse registration setup](https://element-hq.github.io/synapse/latest/setup/installation.html), [Lightsail pricing](https://aws.amazon.com/lightsail/pricing/), [Terraform Lightsail instance](https://registry.terraform.io/providers/hashicorp/aws/latest/docs/resources/lightsail_instance), [GitHub AWS OIDC](https://docs.github.com/en/actions/how-tos/secure-your-work/security-harden-deployments/oidc-in-aws).
