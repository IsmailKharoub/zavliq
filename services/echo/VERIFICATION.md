# Local verification — 2026-09-12

Eleven unit tests pass with Python against the current SDK contract. Coverage includes literal text and exact JSON, lost send responses across journal restart, lost acknowledgement responses, late repeated event IDs, strict conversation selection, unsupported invite floods, self/reply/file/encrypted inputs, durable global and counterpart limits, identity binding and writer locking, unknown member counts, withdrawn conversations that must not block other messages, and private atomic health status transitions.

The operator bootstrap's isolated Node tests cover proof persistence before server writes, stable device/token retries, interrupted account creation, failure to write the final private session, preexisting account collisions, revoked sessions, incomplete crypto-store restores, identity mismatch and unexpected upstream identity. An optional offline native contract check verifies that the actual binary reads the seeded identity without returning credentials. Setup is source-tested with mock Synapse; no production reserved identity has been provisioned by these tests.

The opt-in live script passed all three scenarios against the local gateway at `http://localhost:8080` using the real native runtime and two explicitly labelled fixture identities:

1. A standard DM invitation is accepted; original text and a raw JSON value with a high-precision decimal and a large integer arrive unchanged as a reply to the original event.
2. A deliberately lost local send response is followed by closing/reopening the native runtime and echo journal. Retrying results in exactly one reply to that event.
3. Reply messages do not create echo loops, and group/E2EE invitations remain unaccepted.

Initial testing exposed absent Matrix member summaries: even joined DMs were reported as zero members. The native client now obtains authoritative joined membership counts. The responder permits an absent prejoin summary only with immutable standard-DM metadata and a known inviter, and requires two members after joining. Unknown joined counts retain the cursor. The real test passed after this correction; the same two fixture identities were reused rather than creating repeated accounts.

This is local integration evidence. The permanent responder remains disabled. The opt-in operator bootstrap and supervised Docker profiles are implemented in source; they have not been built or started during the ongoing load measurement. Production reserved-account provisioning, its private recovery proof/device, container startup, public TLS round trip, operational monitoring and the wider launch soak/load gates still require deployment verification.
