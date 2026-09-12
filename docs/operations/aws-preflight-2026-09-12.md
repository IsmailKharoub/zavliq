# AWS preflight — 2026-09-12 UTC

This preflight used local validation and read-only AWS calls. No hosted service, DNS record, bucket, role, subscription, budget or instance was created or changed.

Terraform formatting and provider validation pass. AWS reports the selected `medium_3_0` Lightsail bundle active in us-east-1: **$24/month, 4 GiB RAM, 80 GiB SSD, 4 TiB monthly transfer**. The `ubuntu_24_04` Linux blueprint is active. One existing GitHub Actions OIDC provider and one Lightsail key pair are available in the account/region. The key-pair private material was not requested. No public `zavliq.com` Route53 hosted zone was returned; domain registration remains a release-owner task.

Commands used:

```sh
terraform -chdir=infra/terraform fmt -check
terraform -chdir=infra/terraform validate
aws lightsail get-bundles --region us-east-1
aws lightsail get-blueprints --region us-east-1
aws iam list-open-id-connect-providers
aws lightsail get-key-pairs --region us-east-1
aws route53 list-hosted-zones-by-name --dns-name zavliq.com
```

Outputs were narrowed to the selected bundle/blueprint, existence counts and exact matching zone. No credentials or unrelated key details were printed. A Terraform plan has not been produced or applied because final deployment inputs are still pending.

## Concrete deployment inputs still needed

- Registered domain and authoritative DNS/hosted-zone choice.
- Operator notification email and acknowledgement of the SNS subscription email after creation.
- Operator SSH source CIDR and the selected existing Lightsail key-pair name.
- Existing GitHub OIDC provider ARN and exact repository/environment binding (`IsmailKharoub/zavliq`, production or staging).
- Separately saved age recovery identity; only its public recipient belongs on the host.
- Clean committed release revision and built/tested Linux amd64 image archive.

The Terraform role trust is restricted to the selected repository's named GitHub environment. Source deployment and off-host backup workflow templates remain under `infra/github/`; they must be installed into `.github/workflows/` with repository environment variables before they run. Existing CI/release workflows do not by themselves deploy the service or upload its backups. Host bootstrap installs packages but does not initialize application identity secrets or start a deployment.

The production Compose profile publishes only Caddy; Synapse, PostgreSQL, control and internal policy surfaces remain behind it. All message/contact/file policy limits remain active. Temporary staging admission allowances must not enter the production override. Final AWS validation still requires initial provisioning, TLS and DNS, an actual off-host backup and fresh restore, rollback, alert delivery, the 100-client load measurement on the selected hardware, and 24-hour staging soak.

## Build and browser headers

The local fresh gateway build exposed that its new public-document sync step required `docs/protocol.md` in the Docker build stage. The Dockerfile now copies that explicit public file. All three Docker build contexts exclude nested `.local` and `.private` directories. Fresh isolated Synapse, control and gateway image builds passed after that correction.

Caddy 2.11.4 validates the restrictive Content Security Policy with same-origin scripts/connections, explicit WebAssembly compilation, Google Fonts styles/fonts, same-origin workers, and blocked frames/objects. HSTS is added only to HTTPS responses whose host is neither localhost nor 127.0.0.1; HTTP local development receives no HSTS. The request matcher follows the [Caddy protocol matcher documentation](https://caddyserver.com/docs/caddyfile/matchers#protocol). Header delivery and compiled browser/crypto behavior still need verification with the next gateway build; running stacks were not restarted merely to apply headers during load preparation.
