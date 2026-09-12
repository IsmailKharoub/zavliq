# Local verification — 2026-09-12

Eleven unit tests pass with Python against the current SDK contract. Coverage includes literal text and exact JSON, lost send responses across journal restart, lost acknowledgement responses, late repeated event IDs, strict conversation selection, unsupported invite floods, self/reply/file/encrypted inputs, durable global and counterpart limits, identity binding and writer locking, unknown member counts, withdrawn conversations that must not block other messages, and private atomic health status transitions.

The operator bootstrap's isolated Node tests cover proof persistence before server writes, stable device/token retries, interrupted account creation, failure to write the final private session, preexisting account collisions, revoked sessions, incomplete crypto-store restores, identity mismatch and unexpected upstream identity. An optional offline native contract check verifies that the actual binary reads the seeded identity without returning credentials. Setup is source-tested with mock Synapse; no production reserved identity has been provisioned by these tests.

The opt-in live script passed all three scenarios against the local gateway at `http://localhost:8080` using the real native runtime and two explicitly labelled fixture identities:

1. A standard DM invitation is accepted; original text and a raw JSON value with a high-precision decimal and a large integer arrive unchanged as a reply to the original event.
2. A deliberately lost local send response is followed by closing/reopening the native runtime and echo journal. Retrying results in exactly one reply to that event.
3. Reply messages do not create echo loops, and group/E2EE invitations remain unaccepted.

Initial testing exposed absent Matrix member summaries: even joined DMs were reported as zero members. The native client now obtains authoritative joined membership counts. The responder permits an absent prejoin summary only with immutable standard-DM metadata and a known inviter, and requires two members after joining. Unknown joined counts retain the cursor. The real test passed after this correction; the same two fixture identities were reused rather than creating repeated accounts.

After the measured load run ended, the operator bootstrap and supervised profiles were built and exercised on the separate `zavliq-echo-check` project at loopback port 19280. Repeated setup preserved one fixed `@echo:localhost` non-admin account and device; public registration of the reserved handle was refused. The private identity was mode 0600, the native runtime ran as UID 1000, the identity remained unchanged after crypto initialization, and setup refused to run while that native device held its writer lock.

The actual supervised container became healthy and passed literal standard text/exact JSON, a single reply after additional processing cycles, and unaccepted encrypted/group invitations. The local-only test shared the gateway network namespace so HTTP used loopback; production profiles were not changed.

The real echo-aware backup script produced an encrypted snapshot with a **2.216-second** writer pause. The restore script recovered into fresh `zavliq-echo-restore` volumes and reached main-service health in **30.432 seconds**, leaving Echo disabled as designed. Before explicitly starting the restored responder, its identity bytes matched the original, its encrypted Matrix store existed, its journal still marked the original event done, and bootstrap reused the original device. The restored supervised process became healthy and passed the same live reply/refusal checks on port 19380. Both private fixture stacks were then stopped, with their private volumes retained. The measured load stack and shared application stores were not modified.

Focused machine-readable evidence, including tested image identifiers and limitations, is in [verification-2026-09-12.json](./verification-2026-09-12.json). The new `tests/live_service.py` peer harness only accepts the dedicated local check/restore ports and never reads Echo's private identity itself.

This is local ARM64 Docker integration evidence. The permanent public responder remains disabled. Production reserved-account provisioning, public TLS routing, AWS execution, operational monitoring and the wider launch performance/soak gates still require deployment verification.
