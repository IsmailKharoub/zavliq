# Manual verification of an existing backup

`infra/github/backups.yml` is an inactive manual template. It checks the metadata of one independently selected existing encrypted archive and reads back its small checksum sidecar. It does not create a snapshot, download an archive, upload a new backup, restore data or schedule future runs. The template remains outside `.github/workflows`; committing it does not activate it.

## Private inputs

Use the protected `production` environment and this repository's `main` branch. Its four existing metadata secrets are `AWS_ACCOUNT_ID`, `AWS_DEPLOY_ROLE_ARN`, `LIGHTSAIL_INSTANCE` and `LIGHTSAIL_INSTANCE_ARN`. The preparer validates their relationship before authentication. It accepts only the fixed production role and the supported production instance names/ARNs in `us-east-1`; the backup bucket is derived as `zavliq-production-backups-ACCOUNT_ID`. Metadata secrets contain no AWS access keys.

Provide three additional environment secrets:

| Secret | Required value |
| --- | --- |
| `BACKUP_EXPECTED_MANIFEST` | Independently reviewed private JSON with exactly `name`, `bytes` and `sha256`, at most 4096 UTF-8 bytes. The snapshot must meet the existing helper's 24-hour freshness check. |
| `BACKUP_JOURNAL_RECIPIENT` | The independently approved native age X25519 public recipient, at most 128 ASCII bytes. |
| `BACKUP_JOURNAL_RECIPIENT_SHA256` | The independently recorded lowercase SHA256 of the exact recipient bytes, including any newline. |

Keep manifest contents, recipient bindings and operator evidence out of source, logs and uploaded artifacts. Establish the recipient binding independently; hashing an unchecked replacement does not approve it. Before first activation, encrypt harmless canary bytes to that exact recipient and actually decrypt them off-host with the existing recovery identity. The private identity must stay off GitHub and AWS. Local checks using disposable identities do not establish this fact.

The selected manifest is written once to an owner-private file. The fetch helper compares all three fields with the host's current latest manifest before S3 access. A changed latest snapshot or a stale pin fails the run. Do not automatically update the pin, choose another object or refresh its timestamp to make the check pass; review the new selection independently.

## Preparation before access

`prepare-ci-backup.py` requires Linux x86_64 and an exclusive new workspace beneath the runner's private temporary directory. It creates private input/dependency/ciphertext directories, then downloads the exact Linux AMD64 asset recorded in [age-v1.3.2.json](../../infra/dependencies/age-v1.3.2.json). It verifies complete archive size and SHA256 before interpreting the tar, reads only the unique regular `age/age` member with its independent size/hash, and writes that executable with mode `0700`. It extracts no archive paths, key generator or plugins.

The supported `ubuntu-24.04` runner provides system curl newer than 8.4.0. Reuse on another runner requires at least that version: earlier curl releases did not enforce `--max-filesize` while streaming a response whose size was initially unknown. The request disables default curl configuration, inherits only system PATH and locale, accepts HTTPS only, allows at most five redirects and caps curl at 75 seconds with an independent 80-second process deadline. A partial or mismatched archive is never executed or uploaded. [curl size-limit behavior](https://curl.se/docs/manpage.html#--max-filesize), [GitHub Ubuntu 24.04 inventory](https://github.com/actions/runner-images/blob/main/images/ubuntu/Ubuntu2404-Readme.md)

Preparation invokes the journal helper's native public-canary check using the pinned executable, version and recipient. Any failure stops before AWS credentials or SSH access are requested. Success reports local encryption readiness with `off_host_decryptability_verified: false`; it cannot open the off-host identity or prove recovery. Upstream dependency signature-verification limits are retained in [journal preservation](ci-journal-preservation.md).

## Restricted session and exact check

The pinned AWS action obtains a 30-minute OIDC session. Its inline policy intersects the existing role policy and permits only caller identity, fixed-region Lightsail instance/operation reads, access-certificate and firewall operations against the exact protected instance ARN, and `daily/*` bucket listing/object reads. It omits S3 writes/deletes, snapshots and unrelated service actions. The broader role cannot restore permissions omitted by this session policy. [AWS session-policy behavior](https://docs.aws.amazon.com/STS/latest/APIReference/API_AssumeRoleWithWebIdentity.html)

Lightsail instance/operation reads require wildcard resource scope with a region condition; the three access/firewall actions use the exact instance ARN. IAM cannot restrict these firewall actions to a particular port or runner CIDR, so the existing access helper verifies and journals the one TCP/22 runner `/32` rule. It preserves borrowed operator access and cleans up against the saved identity. [Lightsail authorization reference](https://docs.aws.amazon.com/service-authorization/latest/reference/list_lightsail.html)

The check combines `--expected-manifest`, `--existing-only` and `--public-output`. Missing or conflicting objects fail without downloading the archive or creating either object. A pass proves matching archive metadata and exact bounded sidecar readback. It leaves `off_host_ciphertext_readback_verified` false and proves neither a new conditional upload nor restoration.

## Cleanup and encrypted evidence

After any attempted access step, whether successful, failed or cancelled, cleanup runs before sealing. Access skipped before starting skips both. A missing journal after attempted access produces an unconfirmed preservation failure; file existence is never used to infer that access succeeded or preservation is complete.

The seal step preserves the journal's exact bytes after cleanup finishes. Only a successful seal enables the pinned artifact action, which uploads exactly `ciphertext/journal.age`, uses a unique run/attempt name, errors if that file is absent and retains it for seven days. Successful cleanup journals are preserved too. Earlier access, verification or cleanup failures remain job failures even if encryption/upload succeeds. No step uses `continue-on-error`, a directory upload or a plaintext fallback.

Step limits total 28 minutes inside a 35-minute job: checkout 2, preparation 3, authentication 3, access 3, check 10, cleanup 3, sealing 2 and upload 2. The conservative authentication-through-upload total is 23 minutes within the 30-minute session. Forced cancellation or runner loss can still prevent cleanup and evidence upload. Preserve available private evidence and reconcile an uncertain rule using the saved journal, role session and CloudTrail; retrieve encrypted artifacts before expiry.

## Remaining live verification

Before installing this source as an active workflow, finish the real approved-recipient canary, independently provision/verify the protected input bindings and check CI for the exact reviewed source. Review the concrete manual run against the currently selected host and existing snapshot. Record authentication, metadata/sidecar results, access ownership and terminal cleanup, exact encrypted artifact retrieval and successful off-host journal decryption. Artifact upload alone is not off-host preservation proof; borrowed access does not prove removal of an owned rule.

Offline preparation, failure, privacy and workflow-ordering tests are included in `infra/tests/test_prepare_ci_backup.py`. Disposable native roundtrips and local Linux AMD64 execution through emulation establish their recorded local scopes only. Native GitHub installation, the real recipient, live cleanup and artifact recovery remain separate unperformed checks until evidence exists.

Full deployment, new backup uploads, a transfer schedule, full ciphertext readback and production recovery remain separate requirements. The full-deployment template still needs its raw-journal path corrected. These source additions do not replace helpers embedded in a frozen application bundle or justify restarting the already deployed website.
