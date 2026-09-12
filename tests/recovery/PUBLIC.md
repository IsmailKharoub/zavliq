# Fixed public HTTPS verification

These separate drivers preserve the existing private staging drivers and evidence. They accept only `https://zavliq.com`, Matrix namespace `zavliq.com`, the exact `2f76469b507c8745d4be5ef311f32774021143aa` public bundle, its five pinned images, and the frozen macOS native executable with the installed release wheel. They never create a host, back up a server, change DNS or forwarding, start an application, or import/copy a client identity. Source tests do not establish a live public pass.

The `--execute` flag is mandatory for every phase. Before opening any runtime, each execution checks actual native and installed Python module bytes, a fresh operator target, normal certificate/hostname-verified HTTPS, HSTS, and exact Matrix/control discovery. Redirects, alternate origins and proxy environment variables are not followed by the preflight. Initial `prepare` uses normal public access; restricted verification access is required only for replacement `verify`.

## Three ordinary signups

Run Echo preparation once: it registers `finch-<eight-hex-suffix>`, verifies exact text/JSON, stable send retry and three individual Echo replies across sequential processing cycles. Encrypted/group invitations must remain unaccepted. Run recovery preparation once: it registers `arden-<suffix>` and `mira-<suffix>`. This uses exactly three ordinary registration calls, fitting the default three new admissions per source IP per rolling hour if no earlier signup used that allowance. The global daily cap still applies. A reservation may consume capacity even if an upstream response fails; neither driver replaces an interrupted prepare with new accounts.

Browser QA can pair the Finch account through an explicitly approved request, without a fourth signup. Use a separate QA conversation. Preserve the driver's Finch/Echo conversation so additional Echo replies from browser QA cannot invalidate its exact accounting. Do not pair, open or modify Arden/Mira before recovery verification: the offline recipient and unchanged original stores are part of the proof. Pairing has its own limits (ten starts per IP/hour and device limits) and does not call account enrollment.

## Exact interpreter and executable

From `/Users/ismailkharoub/Dev/ventures/zavliq`, use these existing verified artifacts. No Rust or package build is needed:

```sh
ZAVLIQ_CHECK_PYTHON='/Users/ismailkharoub/Dev/ventures/zavliq/packages/.local/draft-a06594895f87-run34671918085/relocated install/python venv/bin/python'
ZAVLIQ_CHECK_BINARY='/Users/ismailkharoub/Dev/ventures/zavliq/packages/.local/draft-2f76469b507c-run34675091452/frozen/aarch64-apple-darwin/zavliq'
ZAVLIQ_CHECK_REVISION='2f76469b507c8745d4be5ef311f32774021143aa'
```

The interpreter's isolated `-I` mode is required; `-B` avoids generated Python caches. The original installation directory's earlier source name is historical: exact extracted payload equality and installed module bytes were mapped to the final `2f` draft in `docs/releases/private-artifact-verification-2f76469b507c.json`. Native SHA-256 must be `e999ba1d7721b9e026b9a914e57ce766c252501d93e636cca2cba8524b73f5ac`. This is authenticated private-artifact verification, not anonymous installation.

## Operator target

Create a mode-0600 ignored target only after inspecting the actual deployment and canonical route. No token, private key or credential belongs in it. `images` must contain exactly the five `id` values in `docs/releases/production-manifest-2f76469b507c.json`; the drivers pin those values independently. An optional image `ref` is documentation only. The required prepare shape is:

```json
{
  "environment": "aws-production",
  "final_candidate": true,
  "public_verification": true,
  "phase": "prepare",
  "origin": "https://zavliq.com",
  "server_name": "zavliq.com",
  "project": "zavliq-production",
  "revision": "2f76469b507c8745d4be5ef311f32774021143aa",
  "source_archive_sha256": "2a96f82d9413c3adc8193e4335f6703f7f000923b709f11a24a04e03546a0c5e",
  "production_manifest_sha256": "55f0bccfdf7f7a267377fd44ce115603c2e7f8ea383502236f390cee387fc39c",
  "native_binary_sha256": "e999ba1d7721b9e026b9a914e57ce766c252501d93e636cca2cba8524b73f5ac",
  "images": {
    "synapse": {"id": "sha256:ad076f133f0795ec665d3419c0d2d8b4645853f80f64e27cee1dc00061dfe2d7"},
    "control": {"id": "sha256:5f53d7fbef3b59b417716f3798243b48199f9a629c24ec10ad0c1ead711cf0e5"},
    "gateway": {"id": "sha256:1a368a99a18db87f8d1794c4bfc15f777774978c639ea43d4c644671f94f945f"},
    "postgres": {"id": "sha256:18cfe3ef5e6815560c98237d6216d1e5119702fb0f3894c8785dd58b8bbe5d73"},
    "echo": {"id": "sha256:1cb8800c5984b0e4d900a916df57450fb9ab158087c9e9a45764b8c33de51b2b"}
  },
  "target_machine_id": "ACTUAL_32_LOWERCASE_HEX_MACHINE_ID",
  "route_machine_id": "SAME_ACTUAL_MACHINE_ID",
  "route_checked_at": "ACTUAL_TIME_WITH_UTC_OFFSET"
}
```

Replace the three placeholders with observed values. The route attestation expires after 30 minutes. HTTPS proves the certificate and service origin, while the root-owned machine/route inspection attests which physical host serves that origin; an HTTP health response alone cannot distinguish identical restored services.

For `verify`, retain the exact build/images/origin, change `phase` to `verify`, and supply the replacement's distinct `target_machine_id` and matching `route_machine_id`. Add `source_machine_id` equal to the prepared fixture's original machine, `source_writers_fenced: true`, `verification_access_restricted: true`, the original `encrypted_backup_sha256`, and `restore_completed_at` with a timezone after preparation. Never set these attestations before performing the corresponding operation. Both original devices must stay closed while root changes the canonical route.

## Commands after authorization

Choose each unique run ID once and retain it. The examples below use placeholders deliberately; replace `EIGHTHEX` with eight lowercase hexadecimal characters and select actual private target paths before execution.

```sh
ZAVLIQ_ECHO_RUN='public-echo-2f76469b507c-EIGHTHEX'
ZAVLIQ_RECOVERY_RUN='public-recovery-2f76469b507c-EIGHTHEX'
ZAVLIQ_SOURCE_TARGET='/ABSOLUTE/PRIVATE/public-source-target.json'
ZAVLIQ_RESTORE_TARGET='/ABSOLUTE/PRIVATE/public-restored-target.json'

"$ZAVLIQ_CHECK_PYTHON" -I -B tests/recovery/public_echo.py prepare \
  --target "$ZAVLIQ_SOURCE_TARGET" --binary "$ZAVLIQ_CHECK_BINARY" \
  --revision "$ZAVLIQ_CHECK_REVISION" --run-id "$ZAVLIQ_ECHO_RUN" --execute

"$ZAVLIQ_CHECK_PYTHON" -I -B tests/recovery/public_recovery.py prepare \
  --target "$ZAVLIQ_SOURCE_TARGET" --binary "$ZAVLIQ_CHECK_BINARY" \
  --revision "$ZAVLIQ_CHECK_REVISION" --run-id "$ZAVLIQ_RECOVERY_RUN" --execute
```

After both preparations succeed and all fixture processes exit, root owns the production snapshot, checksum/off-host retrieval, fresh-machine restore, source fencing and canonical DNS/TLS routing. Keep the primary age identity off the host; use the separate production restore procedure. Then:

```sh
"$ZAVLIQ_CHECK_PYTHON" -I -B tests/recovery/public_recovery.py verify \
  --target "$ZAVLIQ_RESTORE_TARGET" --binary "$ZAVLIQ_CHECK_BINARY" \
  --revision "$ZAVLIQ_CHECK_REVISION" --run-id "$ZAVLIQ_RECOVERY_RUN" --execute

"$ZAVLIQ_CHECK_PYTHON" -I -B tests/recovery/public_echo.py verify \
  --target "$ZAVLIQ_RESTORE_TARGET" --binary "$ZAVLIQ_CHECK_BINARY" \
  --revision "$ZAVLIQ_CHECK_REVISION" --run-id "$ZAVLIQ_ECHO_RUN" --execute
```

Verification makes no registration calls. Echo reuses Finch and checks unchanged public Echo device fingerprints, all prior replies, and two fresh sequential replies. The local Finch runtime must be closed before browser pairing approval:

```sh
"$ZAVLIQ_CHECK_BINARY" --data-dir "tests/recovery/.local/$ZAVLIQ_ECHO_RUN/peer" \
  --control-url https://zavliq.com pair inspect ACTUAL_PAIRING_ID
"$ZAVLIQ_CHECK_BINARY" --data-dir "tests/recovery/.local/$ZAVLIQ_ECHO_RUN/peer" \
  --control-url https://zavliq.com pair approve ACTUAL_PAIRING_ID --code ACTUAL_BROWSER_CODE
```

Approve only the browser session the operator explicitly started. The ID/code are public verification values; credentials remain inside the browser/native stores.

## Recovery assertions and remaining scope

Preparation creates seven rooms: standard DM, a separate encrypted baseline DM, the retained E2EE DM, one joined group owned by each account, a public channel with the peer subscribed, and an unaccepted encrypted Arden-to-Echo invitation. Mira blocks Echo. The separate baseline room prevents automatic encrypted delivery receipts from establishing the retained room's outbound session early. Mira closes before Arden's first retained E2EE send. Eight retained messages cover standard/E2EE text, exact JSON and files plus group/channel text, all absent from Mira's inbox before snapshot.

On the replacement, the original user/device and identity files must match. The first empty historical-cache check is persisted before any restore sync. Both files download through the native cache-disabled event path into fresh files and must match the source hash; encrypted inbox output must have the exact redacted public MXC descriptor. The driver checks joined room modes/counts, pending invitation counts, the persisted block, and distinct policy refusals when the relevant room owner attempts another pending or blocked Echo invitation. New bidirectional standard and E2EE text/JSON must work without importing keys, re-pairing or re-verifying devices. Both groups also exchange new messages in both directions, and the channel publishes another received message, so cached room membership alone cannot establish those passes.

Daily message/contact quota counters are compared exactly against a nonzero baseline before new verification mutations. Finish that proof within the same UTC day; crossing a reset returns `QUOTA_PROOF_WINDOW_EXPIRED` rather than claiming persistence from a fresh bucket. A successful baseline comparison is journaled so an interrupted later verification can resume without comparing counters after its own new sends.

Not covered here: exhaustive quota exhaustion, minute-window persistence, media quota accounting, publisher-role changes, bans, directory visibility, device-key-loss recovery, complete RTO/RPO, backup/restore automation, alerts, load/soak, anonymous installation or browser QA. The restored Echo pass is separate from the recovery pass. Do not set an operator attestation for any untested requirement merely because these focused checks pass.

Private stores/journals and source/download files remain under `tests/recovery/.local/<run-id>`. Content-free success and immutable per-attempt failure records go to `tests/recovery/evidence/`, with driver/helper and artifact hashes. Failed prepare never resets its accounts or stores. Verify may resume only on the same recorded replacement and encrypted backup, retaining its initial empty-cache proof and stable transaction IDs. Completed phases cannot run again.

Offline validation with no network or native processes:

```sh
"$ZAVLIQ_CHECK_PYTHON" -I -B -W error::ResourceWarning \
  -m unittest discover -s tests/recovery -p 'test_public.py' -v
```
