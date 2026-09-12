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

Terraform in `infra/terraform` provisions one Lightsail 4 GiB Ubuntu instance, its static IPv4 address and firewall, an optional DNS A record in an existing hosted zone, private S3 backups, GitHub OIDC role, a five-minute external Lambda probe, CloudWatch/SNS alerts, and $50/$75/$90 account-spending notifications. The instance bundle is `medium_3_0` in `us-east-1`. Monthly operating target is under $100; traffic and storage can exceed this target and budgets are alerts, not spending caps.

Review `example.tfvars`, fill the deployment's existing IDs, run `terraform init`, `terraform plan -var-file=production.tfvars`, and apply the reviewed plan. The module does not register a domain or silently create an organization-wide GitHub OIDC provider. Back up Terraform state securely and lock it for shared use before multiple operators apply. Destruction protection is enabled on the instance and backup bucket. The initial application remains private until DNS/TLS, registration limits and release checks are verified.

Cloud-init installs Docker, Compose, age, Python, unattended security updates and a 2 GiB swapfile. It contains no application or AWS secrets. Initialize the instance after uploading the first source/release:

```sh
sudo python3 /opt/zavliq/releases/COMMIT/infra/scripts/init-environment.py \
  --directory /etc/zavliq --production \
  --server-name zavliq.com --public-url https://zavliq.com
```

`server_name` is part of every Matrix address and must never change for an existing network. Staging must have its own identity namespace and independent volumes. All application credentials live in `/etc/zavliq` and private Docker volumes. Synapse's signing key, service token, PostgreSQL password and control encryption key are included in the encrypted backup. End-to-end keys remain with clients and are **not** recoverable from server backups.

Caddy trusts no incoming forwarded headers by default. Control trusts the isolated Docker network because only Caddy can reach it publicly; the local direct ports are for development. Upload routes pass through control for per-identity quota enforcement. Exposing Synapse directly in production bypasses that upload boundary and is unsupported. Synapse's directory search is available to authenticated clients, while the mandatory policy module restricts public listing to channels; private groups and DMs cannot be published.

Creation-time media expiry is enforced every five minutes by the maintenance job and control's upload bookkeeping. Synapse's native retention uses last access time and is only a supplementary cleanup. The maintenance job reads expired local media IDs from PostgreSQL and uses Synapse's administrative deletion API; it never edits Synapse tables directly. It processes up to 1,000 files per run, so monitor backlog before increasing admission limits. Quota reservations stay charged until control confirms deletion. The published thirty-day policy is a retention target with periodic purge delay, not a claim of deletion at an exact millisecond.

## Release and rollback

`infra/scripts/build-release.sh` requires a clean committed checkout and builds immutable `linux/amd64` images named with the full commit SHA. It packages only tracked infrastructure and a SHA-256 verified image archive. Transfer the resulting release folder to `/opt/zavliq/releases/SHA` and run its `deploy-release.sh SHA` as root. No package installation or application build occurs on the production host. Each update requires a successful encrypted pre-release backup, retains the previous release/config, and waits for health checks.

The two workflow templates in `infra/github` are installed as `.github/workflows` by the release owner. Configure the GitHub `production` environment with `AWS_DEPLOY_ROLE_ARN`, `LIGHTSAIL_INSTANCE` and `BACKUP_BUCKET`. The trust policy is tied to that exact repository/environment. Workflows obtain temporary AWS credentials and a temporary Lightsail SSH certificate, validate AWS-supplied host keys, temporarily allow only the runner's IPv4 /32, then close that rule and erase the key. Configure environment/tag protections for deployments. No permanent SSH key or AWS key is stored in GitHub or on Lightsail.

On an unhealthy release, inspect content-free health and service logs. If old images are compatible with current database schemas, run `sudo ZAVLIQ_SCHEMA_ROLLBACK_VERIFIED=yes bash /opt/zavliq/current/infra/scripts/rollback.sh`. When schemas are incompatible, restore the pre-release snapshot into a fresh target. Do not blindly downgrade Synapse after a schema migration. Rollback is complete only after independent clients confirm messaging, retained attachments and encrypted-history access. Never remove older images/releases before a successful backup and rollback check; prune deliberately to maintain disk headroom.

## Monitoring and incident response

Create `/etc/zavliq/operations.env` mode 0600 with `ZAVLIQ_PUBLIC_URL=https://zavliq.com` and `ZAVLIQ_BACKUP_RECIPIENT=age1...` (the public recipient only), then run `install-systemd.sh`. Health checks run every minute, check HTTP, Matrix discovery, 80% disk usage and local backup freshness, and publish content-free JSON at `/_zavliq/health`. Two daily snapshots provide scheduling margin around the 24-hour RPO. CloudWatch alarms fire after two missed/failed five-minute probes, including absent host telemetry and absent/failing S3 backups. Confirm the SNS email subscription and perform an actual test alarm; unconfirmed email subscriptions do not deliver alerts.

Investigate in this order: external probe and TLS, `compose.sh ps`, disk space, recent deploy, database health, then service-specific errors. Pause registration (`REGISTRATION_OPEN=false`) and roll out/recreate control when storage or abuse threatens existing users. Escalate if online delivery p95 exceeds two seconds or pending acknowledgements rise. CPU/load and actual delivery-lag measurement must also be reviewed during load testing; an HTTP 200 alone does not prove message delivery.

Do not enable request/body/access-token logging. Synapse access logs and HTTP client logs are suppressed below warning; Docker logs rotate at three 10 MiB files per service. Caddy has no request access log. Operator exports and support reports must exclude tokens, pairing secrets, encrypted key material and private messages.

## Verification status and launch evidence

Configuration validation, local health and tests are useful evidence but do not prove a public launch. Before release, record the real endpoint, commit, timestamp, load distribution/p95/loss results, a restored backup identity test, a schema-compatible rollback test, alert delivery, certificate renewal, and the 24-hour soak. The broader product gates remain in `docs/launch-requirements.md`; this runbook does not waive any of them.

Upstream references: [Synapse configuration](https://element-hq.github.io/synapse/latest/usage/configuration/config_documentation.html), [Synapse registration setup](https://element-hq.github.io/synapse/latest/setup/installation.html), [Lightsail pricing](https://aws.amazon.com/lightsail/pricing/), [Terraform Lightsail instance](https://registry.terraform.io/providers/hashicorp/aws/latest/docs/resources/lightsail_instance), [GitHub AWS OIDC](https://docs.github.com/en/actions/how-tos/secure-your-work/security-harden-deployments/oidc-in-aws).
