# Final-candidate encrypted-history restore fixture

`tests/recovery/verify.py` has two explicit phases, `prepare` and `verify`. It uses two ordinary identities and their original persistent native device stores. It performs no cloud, backup, deployment or tunnel operations. Do not execute either phase until the final candidate's backend images and native binary have been authorized and pinned. Source-only tests do not establish an AWS recovery pass.

The only accepted origin is **`http://localhost:28180`**, through the operator's private staging tunnel. Prepare uses source Compose project `zavliq-load` at remote loopback port `19180`. Verify uses a fresh-volume clone named `zavliq-recovery-<first 12 characters of revision>` at remote loopback port `19181`. Both preserve Matrix server-name `localhost`. Operations changes the existing local forwarding route while both fixture clients are closed; the runner never rewrites a stored homeserver, copies a device identity or starts a second copy of a device store.

## Private target and fixture state

Operations produces a private target JSON from the immutable final bundle and verified deployment, based on `infra/.local/staging-plan/final-deployment.json`. The phase-specific target contains:

```json
{
  "environment": "aws-staging",
  "final_candidate": true,
  "phase": "prepare",
  "origin": "http://localhost:28180",
  "server_name": "localhost",
  "project": "zavliq-load",
  "remote_port": 19180,
  "revision": "EXACT_40_CHARACTER_COMMIT_SHA",
  "source_archive_sha256": "SOURCE_ARCHIVE_SHA256",
  "native_binary_sha256": "LOCAL_EXECUTABLE_SHA256",
  "images": {
    "synapse": { "ref": "IMMUTABLE_REFERENCE", "id": "sha256:IMAGE_ID" },
    "control": { "ref": "IMMUTABLE_REFERENCE", "id": "sha256:IMAGE_ID" },
    "gateway": { "ref": "IMMUTABLE_REFERENCE", "id": "sha256:IMAGE_ID" },
    "postgres": { "ref": "IMMUTABLE_REFERENCE", "id": "sha256:IMAGE_ID" }
  },
  "route_project": "zavliq-load",
  "route_remote_port": 19180,
  "route_checked_at": "TIME_WITH_UTC_OFFSET"
}
```

Replace all placeholders with recorded values. Map the bundle's `web` image to the Compose `gateway` service; include `echo` if it is in the source deployment. Image IDs, source archive SHA-256 and native binary SHA-256 must match exactly in both phases. The runner hashes the actual executable before starting it. `route_checked_at` must be no more than 30 minutes old and include a timezone. Project/port are an **operator attestation** after inspecting the SSH forwarding operation and probing health: identical restored services cannot prove their Compose project through an HTTP health response.

Keep target JSON and the fixture directory private and ignored. The runner creates `tests/recovery/.local/<run-id>/{a,b}` with private permissions, an atomic `state.json` journal, synthetic payload/download files and a fixture lock. It checks native `runtime.lock` files before transitions. Do not manually open these identities in another client, edit the journal, remove an inbox, delete keys or copy the stores to another location. The private stores are needed for the second phase and should remain protected until the evidence is accepted.

## Prepare, snapshot and route

Choose a run ID `recovery-<revision first 12 characters>-<8 random lowercase hex characters>`. Use the exact reviewed native executable; there is no implicit `PATH` fallback. From the repository root, after final-build authorization:

```sh
python3 tests/recovery/verify.py prepare \
  --target /PRIVATE/source-recovery-target.json \
  --binary /ABSOLUTE/PINNED/zavliq \
  --revision EXACT_40_CHARACTER_COMMIT_SHA \
  --run-id recovery-REVISION12-RANDOMHEX8 \
  --execute
```

Prepare registers exactly two ordinary identities, `arden-<suffix>` and `mira-<suffix>`, establishes a private E2EE DM and authenticates each peer's device fingerprint from the other original device store. The recovery label remains in the run ID and fixture metadata. It verifies a B-to-A greeting, then **closes B before A sends its first encrypted message**. A sends the retained text, exact JSON (including a large integer and high-precision decimal) and an encrypted file while B is offline. This also exercises queued room-key delivery during restore. It closes A and confirms the selected event IDs are absent from B's local inbox before declaring `prepared`.

Only after `prepared` and the process has exited should operations snapshot the source, upload and checksum the encrypted archive, restore it into the dedicated clone's fresh volumes, and temporarily point the same local `28180` origin at the clone's `19181` port. Preserve the original Matrix server-name and all pinned image IDs. See [backup-restore.md](backup-restore.md) for the server restore procedure. Never run the fixture clients against both deployments concurrently.

Prepare refuses to run twice against an existing journal or identity directory. An interrupted prepare preserves any created accounts/stores and accepted event IDs for operator investigation; it does not silently create another room or replace a device. A new fixture requires a separate approved run ID.

## Verify on the clone

Operations produces the verify target with `phase: "verify"`, the exact clone project, `remote_port` and `route_remote_port` set to `19181`, a fresh matching route attestation, and two additional fields:

```json
{
  "encrypted_backup_sha256": "EXACT_ENCRYPTED_ARCHIVE_SHA256",
  "restore_completed_at": "TIME_WITH_UTC_OFFSET"
}
```

The restore timestamp must follow the fixture's recorded preparation. Then run the same executable, revision and run ID:

```sh
python3 tests/recovery/verify.py verify \
  --target /PRIVATE/clone-recovery-target.json \
  --binary /ABSOLUTE/PINNED/zavliq \
  --revision EXACT_40_CHARACTER_COMMIT_SHA \
  --run-id recovery-REVISION12-RANDOMHEX8 \
  --execute
```

Before either original runtime starts, verify checks both unchanged identity files and confirms B still has none of the historical events. It durably records this condition before the first restore sync. The original recipient must decrypt the old text and exact JSON, inspect an encrypted attachment descriptor and download the old file into a fresh path whose SHA-256 matches the source. Native attachment download bypasses its media cache. Both original devices must then exchange new encrypted text and JSON using their existing trust and crypto state, without pairing, key import or re-verification.

A failed verification retains its journal and original stores. It may resume against the **same recorded clone and encrypted backup** after operations renews the route attestation. It retains the initial empty-inbox proof, uses stable send transaction IDs, and downloads the attachment into another new path. An already completed verification refuses to run again. After both clients close and the fixture exits, operations returns the local forwarding route to source `19180`; the runner never changes that route.

## Evidence and scope

Successful phases write `tests/recovery/evidence/<run-id>-prepare.json` and `-verify.json`. These contain build hashes, public user/device and event IDs, expected content hashes, timestamps and check results. They exclude message bodies, raw file encryption descriptors/keys, tokens, registration proofs and private identity-file hashes.

An enabled execution that fails also writes a durable `tests/recovery/evidence/<run-id>-<phase>-failed-<unique attempt ID>.json` and exits unsuccessfully. The file contains only a classified error code, timestamps, validated run/revision metadata, the executable hash when available, and build provenance after target validation. It never overwrites an earlier failure or successful phase record and leaves the private phase journal intact. Raw exception messages, target URLs, message content, keys and identity-file hashes are excluded. An invalid run ID is labeled `invalid-run`; omitting `--execute` performs no operation and creates no attempted-run evidence.

`verification_seconds` includes interruptions between the first verify start and successful completion. Operations measures the complete restore RTO and recovered-data age separately; this focused fixture does not measure backup duration or establish the broader retention/RPO, standard-room policy, load or browser gates. Its pass proves that the recorded restore can serve previously unseen encrypted history and attachment bytes to the original device stores, plus a new E2EE roundtrip. It does not prove recovery after losing those client keys.

Run the offline guards and driver-ordering tests without any service or executable:

```sh
python3 -m unittest discover -s tests/recovery -v
```

The fake SDK in these unit tests checks orchestration and failure behavior only. Live encrypted-history evidence exists only after both explicit phases pass against the pinned AWS candidate and fresh restore.
