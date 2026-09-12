# Private staging monitoring and backup window

This temporary mechanism supports the isolated AWS backend while public DNS/TLS is pending. It does not open application ports, establish public discovery/installation, or replace the full messaging load gate. The 24-hour soak must start against the final deployed service images; earlier diagnostics do not count toward it.

## Bounded host authority

The operator runs `infra/staging/issue_capability.py` using the pinned SDK requirements in that directory. It refuses temporary signing credentials whose lifetime could be shorter than the window. Only the private `capability.json` is installed on the host, mode 0600. No AWS account key is copied to Lightsail.

Each signed S3 POST form expires 48 hours after issuance. The health form can overwrite only `status/health.json`, at most 64 KiB. Five fixed UTC half-day backup slots cover the window, at most 256 MiB each, so distinct retained archive storage from one issuance is bounded at 1.25 GiB. There is no wildcard object-key form, read/list grant, or authority to change ACLs/encryption. Every upload requires S3 AES256 encryption. The S3 bucket remains private, requires TLS and expires the `daily/` prefix after seven days.

`upload.py` validates the private file, bucket hostname, expiry, fixed key, source size and encrypted snapshot format before posting. Archives must already be age-encrypted and less than one hour old; the original archive creation timestamp and SHA-256 enter object metadata. Re-uploading an old snapshot does not reset the monitor's RPO clock. Forms are implemented with the official [Boto3 presigned POST API](https://docs.aws.amazon.com/boto3/latest/reference/services/s3/client/generate_presigned_post.html). They are bearer capabilities: keep forms, signatures and policies out of Git, logs, browser pages and model prompts.

Health uploads have a 180-second program deadline and a two-minute systemd process-group limit. Backup uploads run under `timeout 180`; the backup service has a ten-minute startup limit and a 15-second termination grace. The existing snapshot trap resumes paused application writers on failure. Upload failure leaves the encrypted local archive for operator recovery and causes off-host freshness alarms.

## Independent alarms

The separately reviewed `infra/terraform-stage-monitor` module adds a 128 MiB Lambda with a 30-second limit, a five-minute EventBridge schedule, seven-day logs and three CloudWatch alarms. Its IAM role can read the exact health object and encrypted archive prefix, list only `daily/`, publish only the `ZavliqStaging` metric namespace and write its own log group. It cannot write or delete S3 objects, change the host, or read messaging databases.

The monitor uses one-second connect/two-second read bounds without retries, examines at most four newest backup candidates, and reserves time for metric publication. Missing telemetry fails closed. Alarms notify the existing staging SNS topic after two failed five-minute periods:

- `HostHealthy`: healthy host/app evidence newer than 180 seconds.
- `BackupFresh`: an encrypted snapshot whose source timestamp is at most 24 hours old.
- `CapabilityValid`: a 48-hour or shorter capability with at least one hour remaining.

An unconfirmed SNS email subscription does not deliver alerts. Topic creation, alarm wiring, state transitions and actual receipt must be recorded separately. Capability expiry is deliberately visible in uploaded evidence and alarms; renew deliberately or shut down the temporary stage after verification. This temporary upload arrangement is not the permanent production backup design.

## Durable image-bound soak

`install.sh` installs health, backup and media-retention timers without starting a soak. The health probe stores every observation in a local SQLite journal with full synchronous commits and uploads a content-free report. Backup timers retain the existing twice-daily schedule and run the bounded encrypted upload afterward.

After the final core and Echo images are healthy, run `begin_soak.py --revision EXACT_COMMIT_SHA`. It requires all selected running image tags to match the revision and records their immutable image IDs. It refuses to overwrite an existing marker. Use a fresh evidence directory for a deliberate new soak after repairing a failed build; preserve the old evidence.

Every subsequent sample checks the API endpoints, disk, local backup age, running service health and exact image IDs. The soak passes only after at least 24 hours and 1,440 healthy observations with no gap greater than 90 seconds. Any recorded failed sample or gap remains a failure for that marker. `--without-echo` labels diagnostic scope and cannot claim the final service set. The summary is local evidence until independently retrieved and reviewed; no 24-hour result is claimed merely because the timer started.

The newer, source-only observation implementation reserves a distinct failed/incomplete SQLite row before checking anything. It checkpoints the bounded HTTP result and each service inspection into that same row, then finalizes only that attempt. An invocation interrupted before finalization, or an overlapping invocation, leaves a failed record; a later successful check cannot replace it. Health-row finalization precedes the separate upload, whose outcome is reported separately; a completed health row alone does not prove publication. Evidence includes the actual invocation, health-check and individual inspection start/end times, monotonic elapsed durations, container IDs, exact image IDs and observed running/paused/health state. An inspection's `ok` means its state was read successfully; the unchanged image/health check still rejects paused, stopped, unhealthy or changed-image services. Inspection brackets are neither atomic across services nor exact Docker transition timestamps. No scheduled time is inferred from the invocation clock, and missed/coalesced timer invocations remain visible through the existing continuity rule.

Only the existing five HTTP/disk/backup-age checks and bounded service metadata enter the report. Raw subprocess stderr, response bodies and unknown diagnostic fields are excluded. A timeout or invalid health-check result fails closed and explicitly records an incomplete health observation; partial endpoint results unavailable from the child are not invented. Reports remain below the existing 64 KiB upload limit. The 40-second health subprocess bound, maximum five five-second inspections, uploader and systemd bounds remain unchanged. These operator-source changes have not been installed or used to start a new run; they add evidence and do not coordinate or excuse backup pauses.

Preserve the current failed marker and journal. Any future backup/sampler coordination needs separate review of its durable pause/resume evidence and bounded interruption semantics, followed by an explicitly authorized new full 24-hour run with separate evidence and exact operator/image hashes. Before that new start, verify a separately reviewed bounded upload capability covers the actual start plus 24 hours and an operating margin, preserving the existing scope and object limits. The existing capability must not be assumed to cover a later run; renewal and installation are separate operator actions.

Server restore/rollback, native message ingestion, full 30-minute performance, E2EE and public TLS remain separate tests. Coordinate every backup/restore writer pause with active timing measurements.

## Recorded private-stage verification

On September 12, 2026, the reviewed ten-resource monitoring plan was applied. The live Lambda returned `HostHealthy=1`, `BackupFresh=1` and `CapabilityValid=1`; all three alarms subsequently reached `OK`. Alarm actions reference the staging SNS topic, but its email subscription is still unconfirmed and notification receipt is unverified.

The first age-encrypted S3 archive contained 10,478,264 bytes. Downloaded bytes matched the SHA-256 metadata, and the primary recovery key decrypted it off-server. Snapshot writers paused for 2.287 seconds. A one-use recovery envelope allowed the isolated AWS clone to restore without installing the primary recovery key on Lightsail. Fresh volumes became healthy in 34.54 seconds; all 1,800 diagnostic messages were present in both event tables, two original identities/devices authenticated, historical content was readable, and a new message synced to its recipient. The clone is stopped and the temporary envelope/key were removed. The 34.54-second measurement covers the restore script through service health, excluding transfer and host provisioning.

The application was still commit `5fd35f709cf117fb448c23babe2d7583a1d4c197`, with standard-message diagnostic fixtures. This is a focused backend restore result. The final build's E2EE/attachment recovery, full load test and 24-hour soak remain open. No soak marker was created; the journal retains an initial `backup_age` failure from before the first backup completed.

The current signed capability expires **September 14, 2026 at 03:11:36 UTC**. Keep that expiry visible during handoff and renew deliberately if the final validation window extends beyond it. Full sanitized evidence is in [private-staging-verification-2026-09-12.json](private-staging-verification-2026-09-12.json).
