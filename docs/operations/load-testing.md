# Isolated load measurement

`tests/load/run.py` prepares and drives 100 fresh real Rust clients through the Python SDK. Fifty standard DMs pair the identities, and all 100 identities send in round-robin order at ten aggregate messages per second for 30 minutes: 18,000 logical sends, 180 per identity. Only registration admission increases for this isolated fixture setup; the normal message/contact quotas, persistence and gateway remain active. The benchmark uses synthetic messages and does not contact the public network or shared local application.

A full local Docker run on 2026-09-12 failed the delivery and latency gate: 18,000 accepted sends, 17,973 observed after drain, and p95 3.496 seconds. All 18,000 events persisted in Synapse; the 27 missing observations were absent from the recipients' native inboxes. An isolated regression reproduced interruption between the Matrix SDK's state save and Zavliq's inbox commit. The repair and subsequent measurements must pass before any launch claim. Preserve this failed evidence alongside later results.

Unit tests validate target isolation and gate accounting; they are not performance evidence. Local Docker diagnostics and a benchmark on the AWS deployment must be reported separately. No AWS load gate has passed yet.

## Separate local diagnostic stack

The helper uses project `zavliq-load`, distinct volumes, fresh secrets, release image tag `load-local`, and loopback ports 19080/19008/19001. It never modifies or deletes the shared project's volumes. `stop` preserves fixtures for inspection. Starting another full stack uses additional local memory and disk; stop it after measurement.

```sh
python3 -m unittest discover -s tests/load -p 'test_*.py'
bash tests/load/isolated-stack.sh init
bash tests/load/isolated-stack.sh up
bash tests/load/isolated-stack.sh status
# A short harness diagnostic, not the release workload:
python3 tests/load/run.py --clients 4 --rate 1 --duration 30
# The full workload, still classified as local:
python3 tests/load/run.py
bash tests/load/isolated-stack.sh stop
```

Do not regenerate an existing environment. The private target manifest is `tests/load/.local/stack/target.json`. Record host CPU, memory, Docker resource limits and other workloads before interpreting local latency. Registration setup creates fresh ordinary handles per run and is outside the load interval. Existing fixture identities and durable stores are retained in ignored `.local/`; no private identity file is read or copied into reports.

For the AWS run, the release owner must deploy an isolated staging service with the same application build, resource limits and expected instance size. The current private staging tunnel uses loopback port 28180; port 19180 remains in use by the local diagnostic control service. Use a separate target manifest with `project=zavliq-load`, `environment=aws-staging`, the tunnel origin, and verified instance/CPU/memory/region/build details in `hardware`. The fixture's advertised homeserver must match the tunneled origin. The manifest's classification is an operator assertion that needs deployment evidence in the report; it is not automatic hardware detection. The harness refuses arbitrary remote addresses, the shared 8080 service and the 18080 restore drill. Do not overlap backup/restore pauses with a throughput measurement.

## Measurement and failure accounting

An open-loop clock schedules one send every 100 ms. A slot more than 250 ms late or with excessive outstanding work is counted as missed; it is not caught up with a burst. Each logical send has a stable transaction key and random run identifier. Failed or ambiguous sends remain failures and are not silently retried into successes. A private event ledger records accepted Matrix event IDs and recipient observations without credentials.

The runtime synchronizes in the background. Receiver tasks consume native notifications, then read their durable incoming inbox with cursors and complete JSON payloads. Each observation must match the expected paired sender, room, run identifier and sequence; accepted/observed event IDs must match exactly. Pagination and history gaps are handled explicitly. Up to 60 seconds of drain follows the send interval; late messages remain in latency statistics. Accepted events missing after drain are counted as lost. Duplicate logical sequences with different event IDs fail the workload.

Latency is measured from the scheduled send time on the benchmark controller's monotonic clock to the recipient SDK's durable inbox result, so controller scheduling and polling delay are included. Send-acceptance latency is reported separately. The p95 uses the nearest-rank tail statistic. There is no cross-host clock synchronization dependency.

The gate requires exactly 100 clients, 30 minutes, 10 aggregate messages/second, all 18,000 accepted and observed, no missed slots/send failures/duplicates, and end-to-end p95 below 2 seconds. Full metrics and environment labels are written under `tests/load/evidence/`. Failed fixture setup produces a failed evidence record. A short run or local run cannot set `aws_staging_gate_passed=true`.

This workload measures standard DM delivery. Encryption/recovery, removals, media retention, failover and the 24-hour staging soak have separate checks and are not established by these load numbers.
