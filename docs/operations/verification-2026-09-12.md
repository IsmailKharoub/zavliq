# Local operations verification — 2026-09-12 UTC

These are observed local results. No AWS resources, domain records, public infrastructure or external notifications were created by this verification.

## Configuration and service boundary

- Terraform 1.x validation and formatting checks passed with AWS provider 6.64.0 and archive provider 2.8.1. Provider selections are locked in the infrastructure directory.
- Compose and workflow YAML parsed. Shell scripts passed Bash syntax checks; Python scripts compiled.
- Fresh environment initialization produced mode-0600 secrets, a 64-character control key, no secret-bearing output, refused to overwrite existing identity credentials, and rejected localhost/HTTP for production initialization.
- Local images built and ran with Synapse 1.160.0, PostgreSQL 17.11, Node 24.21.0 and Caddy 2.11.4. This run used Linux/ARM64 containers under Docker Desktop; production Linux/AMD64 image execution remains a separate release check.
- The gateway returned 200 for the landing page, agent skill, control health, Matrix versions, and both discovery endpoints. Administrative, federation and federation-key paths returned 404 after fixing explicit Caddy directive ordering.
- A real control registration returned 201; the resulting token validated the same Matrix user and device. Credentials were kept out of the transcript.
- The creation-time retention maintenance job connected to PostgreSQL and completed with zero expired objects in this new test network. An actual expired-media fixture deletion remains to be tested.
- External-monitor tests confirmed that unavailable HTTP, stale telemetry and a newly re-uploaded old backup produce failing metrics. No actual AWS alarm delivery was exercised.

The host health probe correctly flagged this Mac's disk usage above the 80% threshold (observed 93.1%). This is a local host condition, not a claim about a deployed AWS instance. No unrelated disk cleanup was performed.

## Encrypted backup and fresh-target restore

The drill used two explicitly named fixture identities and their private client stores. The fixture created a standard conversation/message, an encrypted conversation/message, an encrypted attachment and a pending request. The original encrypted event was confirmed to contain ciphertext without the fixture plaintext.

Observed source backup at **2026-09-12 00:28:35 UTC**:

- Writer pause: **1.663 seconds**.
- Total backup time: **1.946 seconds**.
- Encrypted archive: **1,495,592 bytes**.
- Original service returned health 200 immediately afterward.

The first fresh-target attempt exposed an ownership defect: Python's safe extraction deliberately discards archive UID/GID, leaving the restored signing key owned by root. The script now assigns only the known Synapse/control service owners after extracting into their fresh managed volumes. Only that failed isolated clone was removed; the source network was preserved. The same encrypted archive was then restored again from a fresh project with the corrected script.

The successful fresh restore reached healthy services in **27.987 seconds**. Verification then passed all of:

1. Both original user IDs and device IDs were preserved.
2. Copied client inbox databases were removed before verification, forcing history retrieval from the restored server.
3. The original standard message was recovered.
4. The original encrypted message was fetched and decrypted with the original fixture client keys.
5. The encrypted attachment was downloaded, decrypted and matched its original bytes.
6. The pending contact request survived.
7. A new message was sent and delivered on the restored deployment.

The source network remained at localhost:8080. The clone used a distinct Compose project and ports 18080/18008/13001. After validation its containers were stopped, with its volumes retained for inspection. All private fixture stores, age key, archive and recovery configuration remain under ignored `infra/.local`; they are not publication artifacts and must not be added to Git.

## Repeating the drill

The reproducible fixture helper is `infra/scripts/drill-fixture.py`; run it with `PYTHONPATH=packages/client-python/src`, an explicit private directory, origin and runtime binary. `seed` creates dedicated identities; `verify` clones only those fixture stores, changes their configured origin, removes their inbox cache, and checks the restored service. Never point the helper at someone else's client state.

For systems without Linux backup tools, build `infra/docker/operations.Dockerfile` and run the helper image with the repository mounted at its original absolute path and access to the local Docker socket. Generate the age key privately, run `backup.sh`, initialize a new environment with a distinct project/ports, and run `restore.sh` with the exact backup and key. This Docker helper is an operator tool and is not exposed as an application service.

## What this does not prove

This local drill proves a working encrypted snapshot and fresh-volume recovery for the exercised fixture data. It does not prove the scheduled 24-hour disaster RPO, S3 access/lifecycle, production architecture, DNS/TLS renewal, operator alert receipt, schema-incompatible recovery, immutable release rollback, production load target or 24-hour staging soak. Those gates require their own current-state evidence before launch.
