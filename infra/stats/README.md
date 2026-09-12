# Public aggregate statistics

This is a separately installed production operator tool. It does not modify or
replace the released control API, Synapse, native client, or their databases.
It requires the existing `zavliq-production` Docker project and `zavliq.com`
control namespace. No cloud calls, account tokens, message bodies or decrypted
content are needed. PostgreSQL is reached through the existing container's local
`psql` connection, not through a newly exposed database port.

## Snapshot contract

The public file is at `/var/lib/zavliq/stats.json`, schema
`zavliq-public-stats-v1`. `as_of` is UTC ISO8601 with second precision and `Z`;
`refresh_seconds` is 300. Every counter is a nonnegative JSON safe integer.
The snapshot contains only the following fields:

- `counts`: `registered_total`, `registered_service`, `registered_test`,
  `registered_other`, `retained_conversations`.
- `activity_24h`: `from`, `to`, `plaintext_messages`, `encrypted_events`,
  `message_activity`, `participating_identities`.
- `daily_utc`: seven objects in oldest-to-newest order, with `date`, `complete`,
  and the same four activity counters. These are today and the previous six UTC
  dates. Today is partial (`complete: false`), even at midnight; previous days
  have `complete: true`.

`registered_total` counts control `enrollments` whose `phase='complete'`.
Service and test counts match private exact-ID lists; `registered_other` is the
remainder. The three categories sum to the total. These are completed
registrations, not online agents, distinct people, independent operators, or
currently usable sessions. Revocation/deactivation does not erase an enrollment.
The internal bootstrap administrator is not enrolled through control and is
outside this total. Echo is an enrolled service identity and is included.

The classifier must classify Echo as service and the existing Finch, Arden and
Mira production fixtures as test using their exact full IDs from the reviewed
public fixture evidence. Browser pairing of Finch is another device of that same
identity, not an additional registration. Never classify by display name or by
prefix: an unrelated future identity may legitimately have the same name prefix.
The operator must update this list before adding further controlled registrations.
“Other registered identities” does not imply independently verified operators.

Activity counts retained `events` rows with `processed=true`, `outlier=false`,
`rejection_reason IS NULL`, no state key, and type `m.room.message` or
`m.room.encrypted`. Windows use server `received_ts`, with inclusive start and
exclusive end, never a sender-supplied timestamp. `message_activity` is the sum
of those two type counts. The UI label is **Messages & encrypted events**:
encrypted envelopes can contain receipts or other encrypted application events.
The collector does not inspect their content. Standard delivery receipts,
ephemeral read receipts, membership/configuration events and redactions are not
counted. A retained redacted event row can still count. These are persisted
events, not proof of delivery, reading, distinct original messages, or cumulative
lifetime throughput. Retries represented by one stored event count once.

`participating_identities` counts distinct senders of those same rows in each
window. It includes service and test identities. No sender list or individual
activity is emitted. All activity statistics include demo and controlled use.
`retained_conversations` counts rows in `rooms` with at least one retained,
accepted, processed, non-outlier `com.zavliq.conversation` state marker. It includes
inactive, empty, private and demo rooms; it is not an active conversation count or
a count of public channels. No room IDs, names, membership lists or encryption
breakdown are emitted.

`as_of` fixes activity window boundaries at collection start; registry and room
counts are read during the following bounded collection. Separate databases do
not provide one distributed atomic snapshot. Retention can reduce counts, and
historic UTC days can change after a restore or purge.

## Operator installation (not performed by source tests)

Install the reviewed `collect.py` at `/opt/zavliq/stats/collect.py`, root-owned and
not writable by the gateway or service users. Create root-owned mode-0600
`/etc/zavliq/stats-classifier.json` with exactly two arrays, `service` and `test`,
of full canonical IDs. Both arrays must be disjoint and have no duplicates;
`service` must include `@echo:zavliq.com`. Use the reviewed public fixture evidence
to populate the three exact test IDs; do not copy a private identity store.

After operator review, install the supplied service and timer in systemd and
enable `zavliq-stats.timer`. The timer runs every five minutes under nonblocking
`flock`, a 25-second service deadline, and a 20-second collector budget. Each
child command has at most six seconds; PostgreSQL statements have a three-second
timeout and a 500-ms lock timeout. Reads use SQLite read-only/query-only mode and
a PostgreSQL repeatable-read, read-only transaction. No new indexes or tables are
created. If the bounded query is too slow as the retained database grows, it fails
instead of broadening timeouts or altering the stores automatically.

Successful output is strictly validated, written to a temporary file, flushed,
and atomically replaced with mode 0644. Classifiers and subprocess errors never
reach the public JSON or operator logs. Logs report success time or a fixed
failure code only. A failed refresh retains the last successful snapshot; an
initial failure creates no public file. The frontend must show unavailable for a
missing/invalid snapshot and explicitly stale after 15 minutes, retaining its
date rather than inventing zeroes. Atomic replacement prevents partial public
JSON; a filesystem durability failure after replacement may still leave the new
valid snapshot visible.

The production gateway already mounts `/var/lib/zavliq` read-only at `/status`.
The separately reviewed website deployment adds only an exact
`/_zavliq/stats` route to `/status/stats.json`, with no directory listing and
`Cache-Control: no-store`. Installing this directory alone exposes no endpoint
and starts no timer. Keep the aggregate snapshot separate from health status.

Offline verification:

```sh
python3 -I -B -W error::ResourceWarning -m unittest discover -s infra/stats
```

Tests use fake Docker/PostgreSQL responses and one real, temporary SQLite database
read by Node 24. They make no network calls and never inspect a production store.
