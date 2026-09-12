# Isolated load measurement

`tests/load/run.py` prepares and drives 100 fresh real Rust clients through the Python SDK. Fifty standard DMs pair the identities, and all 100 identities send in round-robin order at ten aggregate messages per second for 30 minutes: 18,000 logical sends, 180 per identity. Only registration admission increases for this isolated fixture setup; the normal message/contact quotas, persistence and gateway remain active. The benchmark uses synthetic messages and does not contact the public network or shared local application.

A full local Docker run on 2026-09-12 failed the delivery and latency gate: 18,000 accepted sends, 17,973 observed after drain, and p95 3.496 seconds. All 18,000 events persisted in Synapse; the 27 missing observations were absent from the recipients' native inboxes. An isolated regression reproduced interruption between the Matrix SDK's state save and Zavliq's inbox commit. The repair and subsequent measurements must pass before any launch claim. Preserve this failed evidence alongside later results.

Unit tests validate target isolation and gate accounting; they are not performance evidence. Local Docker diagnostics and a benchmark on the AWS deployment must be reported separately. No AWS load gate has passed yet.

A subsequent 180-second private AWS diagnostic received all 1,800 accepted messages without duplicates or missed slots, but failed latency: durable-inbox p95 was 5.828 seconds and send-ack p95 was 3.2243 seconds. The controller ran locally through an SSH tunnel to us-east-1, with about 300 ms per idle HTTP request. The instance had 1.19% burst capacity remaining in a coarse sample during the run. Those are possible contributors, not proof of throttling. The binary included the initial standard-message cancellation repair, before the final encrypted-key recovery journal; this short run establishes neither the full load gate nor final encrypted-runtime correctness. Preserve its metrics and the companion host evidence.

Run `20260912T033422Z-a875d3` then passed the **short diagnostic** with 100 clients, 10 messages/second and all 1,800 accepted messages observed. There were no missing messages, duplicates, failed sends, missed slots or receiver errors. Durable-inbox p95 was **1.4736 seconds**, send-ack p95 **0.8265 seconds**, local inbox RPC p95 **0.0051 seconds**, and send lock-wait p95 zero. This used the final interrupted-sync repair and long-polling runtime, plus explicit notification-driven local reads (`inbox_mode=background`). The pinned binary SHA-256 is recorded in its JSON evidence. The backend was still commit `5fd35f7`, reached through the same private AWS tunnel. This is not the full 30-minute test on the final backend, and it does not set the AWS launch gate to passed. All 100 runtime processes closed after measurement.

The matching coarse AWS sample averaged 7.45% CPU with a 22.43% maximum; burst capacity was still low at 2.22% and recovering. The interval contained 3,645 successful control requests, versus 12,073 in the earlier diagnostic, with control processing p95 20.49 ms. These measurements support reduced work alongside the lower observed latency; they are not a controlled attribution of every improvement. Long-poll disconnect/timeout warnings remain in the companion evidence rather than being hidden or counted as message losses.

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

Do not regenerate an existing environment. The private target manifest is `tests/load/.local/stack/target.json`. Record host CPU, memory, Docker resource limits and other workloads before interpreting local latency. Registration setup creates fresh ordinary handles per run and is outside the load interval. Initialization uses two workers; four independent DM pairs can complete membership validation concurrently. All setup finishes before measurement starts. Existing fixture identities and durable stores are retained in ignored `.local/`; no private identity file is read or copied into reports.

For the AWS run, the release owner must deploy an isolated staging service with the same application build, resource limits and expected instance size. The current private staging tunnel uses loopback port 28180; port 19180 remains in use by the local diagnostic control service. Use a separate target manifest with `project=zavliq-load`, `environment=aws-staging`, the tunnel origin, and verified instance/CPU/memory/region/build details in `hardware`. The fixture's advertised homeserver must match the tunneled origin. The manifest's classification is an operator assertion that needs deployment evidence in the report; it is not automatic hardware detection. The harness refuses arbitrary remote addresses, the shared 8080 service and the 18080 restore drill. Do not overlap backup/restore pauses with a throughput measurement.

## Measurement and failure accounting

An open-loop clock schedules one send every 100 ms. A slot more than 250 ms late or with excessive outstanding work is counted as missed; it is not caught up with a burst. Each logical send has a stable transaction key and random run identifier. Failed or ambiguous sends remain failures and are not silently retried into successes. A private event ledger records accepted Matrix event IDs and recipient observations without credentials.

The runtime synchronizes in the background. Receiver tasks consume native notifications, then read their durable incoming inbox with cursors and complete JSON payloads. Each observation must match the expected paired sender, room, run identifier and sequence; accepted/observed event IDs must match exactly. Pagination and history gaps are handled explicitly. Up to 60 seconds of drain follows the send interval; late messages remain in latency statistics. Accepted events missing after drain are counted as lost. Duplicate logical sequences with different event IDs fail the workload.

Latency is measured from the scheduled send time on the benchmark controller's monotonic clock to the recipient SDK's durable inbox result, so controller scheduling and polling delay are included. Send-acceptance latency is reported separately. The p95 uses the nearest-rank tail statistic. There is no cross-host clock synchronization dependency.

New measurements also report successful send and inbox RPC durations separately from their waits for the per-identity controller lock. These distributions diagnose controller contention and network/runtime work; they do not replace the end-to-end latency gate. Individual percentile values should not be added together.

`--inbox-mode fresh` remains the default: each read requests a fresh sync and polls again after two seconds without a notification. `--inbox-mode background` uses the long-running native RPC process to synchronize and explicitly reads its durable inbox with `sync=false`. It drains every page, then waits on the buffered notification queue without a polling timer. Initial synchronization remains outside the measurement clock. The measured mode is recorded in every result. Background mode requires a runtime that announces changed durable inbox sequences after a successful sync even when earlier synchronization was interrupted; it uses the block list from the last completed sync. Its end-to-end clock, loss accounting and acceptance thresholds are unchanged.

The gate requires exactly 100 clients, 30 minutes, 10 aggregate messages/second, all 18,000 accepted and observed, no missed slots/send failures/duplicates, and end-to-end p95 below 2 seconds. Full metrics and environment labels are written under `tests/load/evidence/`. Failed fixture setup produces a failed evidence record. A short run or local run cannot set `aws_staging_gate_passed=true`.

This workload measures standard DM delivery. Encryption/recovery, removals, media retention, failover and the 24-hour staging soak have separate checks and are not established by these load numbers.
