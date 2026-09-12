# Backup and recovery

The default recovery objectives are at most 24 hours of disaster-related loss and at most two hours to restore a fresh deployment. They are targets until a timed drill passes. Backup retention in S3 is seven days, with AWS lifecycle processing performed asynchronously. Live message/media retention and encrypted client key recovery are separate policies.

## Encryption and scheduling

Generate an age recovery identity **off the server** using `age-keygen -o /PRIVATE/OFFLINE/zavliq-backup.key`. Keep the private file in the operator's backed-up recovery store. Place only the generated public recipient into `/etc/zavliq/operations.env`. Never commit the private key, put it on Lightsail, or print it in a support transcript.

The systemd timer takes snapshots twice daily (02:15 and 14:15 UTC plus a small jitter). `backup.sh` serializes runs with a lock, pauses Synapse/control writers, exports PostgreSQL and copies application volumes and configuration, resumes writers even on failure, encrypts the archive with age, writes a SHA-256 checksum, and keeps seven days of encrypted local retries. Gateway requests can wait briefly during the consistent snapshot. Measure and record that pause under load; the first release must keep it below 30 seconds. If data growth makes that impractical, replace this strategy with a validated coordinated hot-snapshot implementation before increasing public quotas.

The off-host workflow runs at 02:43 and 14:43 UTC. It retrieves only encrypted archives over verified SSH and writes them to private S3 with AES256 server encryption and TLS. Lightsail has no S3 credential; the ephemeral GitHub OIDC role may access only this deployment's backup prefix and temporary SSH APIs. Local success does not mean off-host success: the independent Lambda checks S3 timestamps and alarms if no backup is younger than 24 hours. GitHub schedule delay is expected; the duplicate daily run provides margin but is not an SLA.

## Fresh-target recovery drill

1. Record the timer start, original server-name and source release. Provision an isolated host with the same architecture and PostgreSQL major version. Keep its DNS disconnected from the original network.
2. Download a selected `.tar.age` and checksum from S3 using operator access. Verify SHA-256 before decrypting; retain the exact source release images so schemas match. Never restore an unknown archive.
3. Initialize a separate Compose environment with the **original** Matrix server-name and an isolated public origin. Select a unique Compose project name in its private `compose.env`, and ensure it has no containers or old volumes. Do not run `up` before restoration.
4. Supply the private recovery key through a secure temporary mount/file on that recovery host, then run:

```sh
ZAVLIQ_ENVIRONMENT=production \
ZAVLIQ_ENV_FILE=/PRIVATE/RESTORE/compose.env \
ZAVLIQ_RESTORE_FRESH_ENVIRONMENT=yes \
  bash infra/scripts/restore.sh /PRIVATE/zavliq-TIMESTAMP.tar.age /PRIVATE/zavliq-backup.key
```

5. Verify original identity login, a standard DM and its attachment, group memberships, a pending request and block, quota counters, an offline message, and retained encrypted messages using the original client's encryption keys. A server restore cannot decrypt encrypted history by itself. Rotate the temporary recovery host's SSH access and remove its temporary private age key after validation.
6. Measure elapsed restore time and age of the recovered message. Record actual values against the two-hour RTO/24-hour RPO. In a real incident, stop all writes on the failed host, redirect DNS only after verification, and ensure only one network with the original server-name is active.

`restore.sh` refuses unsafe archive members, absent essential files, mismatched Matrix server-name, and targets with existing containers. It restores credential material, PostgreSQL, Synapse signing/config/media/policy data, control SQLite, and the service token. The recovery environment's origin and private host path remain configurable. A rollback after an incompatible schema migration uses this same procedure with the pre-deploy snapshot.
