# Private CI cleanup evidence

`infra/scripts/seal-ci-journal.py` prepares encrypted cleanup evidence for a later ciphertext-only artifact upload. It is source tooling, not an active workflow or a proof of production recovery. The manual existing-backup template now integrates it through `prepare-ci-backup.py`; both templates remain inactive. The full-deployment template still requires integration changes before use. See [the manual check procedure](existing-backup-check.md) for its exact inputs, limits and remaining live verification.

The raw access journal includes private infrastructure metadata. GitHub repository readers can download workflow artifacts; secret masking applies to logs and does not redact files inside an artifact. Preserve the entire journal encrypted to the independently approved recovery public recipient. Do not upload plaintext, SSH files or the journal directory. [GitHub artifact access](https://docs.github.com/en/actions/how-tos/manage-workflow-runs/download-workflow-artifacts)

## Prepare before requesting access

Use the reviewed platform entry in [age-v1.3.2.json](../../infra/dependencies/age-v1.3.2.json). Download the exact official asset with bounded HTTPS handling, compare its complete size and SHA256, inspect the verified tar and copy only the unique regular `age/age` member into a fresh private dependency directory. Verify that executable's size and hash and set mode `0700`. Pass its absolute path, recorded executable hash and recorded version to the helper. Do not derive the expected pin from an unchecked download or install bundled plugins. Production encryption does not require `age-keygen` or any private identity.

The upstream release is not marked immutable. The committed hashes pin reviewed bytes; the asset IDs identify the observed assets. These hashes matched GitHub's asset digests, but publisher signatures, Sigsum proofs and reproducible builds have not been independently verified. The dependency record preserves that limit. [Upstream verification instructions](https://github.com/FiloSottile/age/blob/b74dce4cdbe35b5e5f66c06d9612b72f89028758/SIGSUM.md)

Provide the existing approved **public** X25519 recipient in a private current-owner regular file, with its independently recorded SHA256. The hash binds the exact file bytes, including any newline. Never derive it from an unchecked replacement or commit private operator configuration. The primary recovery identity stays off GitHub and AWS. Before the first live access run, verify a harmless canary encrypted to that exact recipient can actually be decrypted off-host; the helper cannot assess an identity it never opens.

Create a dedicated output directory owned by the runner and accessible only to that owner. Use an exclusive run/attempt workspace and complete configuration checks before opening SSH:

```sh
python3 infra/scripts/seal-ci-journal.py check \
  --age-binary "$PINNED_AGE_BINARY" \
  --age-sha256 "$REVIEWED_AGE_SHA256" --age-version "$REVIEWED_AGE_VERSION" \
  --recipient-file "$PRIVATE_RECIPIENT_FILE" --recipient-sha256 "$REVIEWED_RECIPIENT_SHA256" \
  --directory "$PRIVATE_CIPHERTEXT_DIRECTORY"
```

This checks private inputs, exact binary and recipient pins, the executable version, and native encryption of a fixed public canary. It removes its own temporary ciphertext. `encryption_ready: true` establishes local encryption readiness only; `off_host_decryptability_verified` remains false. It does not replace the independent off-host canary check or create an SSH rule.

## Preserve after cleanup

Wait for the access cleanup step to finish, whether it succeeds or fails. Prevent concurrent journal/configuration writers through the existing host-access concurrency group and step ordering. Never run sealing in parallel with `close_access`: cleanup atomically replaces the journal as it records progress. File-change checks supplement this ordering; they cannot make concurrent mutation safe.

```sh
python3 infra/scripts/seal-ci-journal.py seal \
  --age-binary "$PINNED_AGE_BINARY" \
  --age-sha256 "$REVIEWED_AGE_SHA256" --age-version "$REVIEWED_AGE_VERSION" \
  --recipient-file "$PRIVATE_RECIPIENT_FILE" --recipient-sha256 "$REVIEWED_RECIPIENT_SHA256" \
  --journal "$PRIVATE_ACCESS_DIRECTORY/state.json" \
  --output "$PRIVATE_CIPHERTEXT_DIRECTORY/journal.age"
```

The journal is required, owner-private, regular, nonempty and at most 4096 bytes, matching the access helper's bound. Symlinks, FIFOs, untrusted file permissions and missing journals fail before encryption. The helper reads the exact bytes once without parsing, redaction or reserialization. Whitespace and unknown fields are preserved. The binary and recipient are checked independently and only a native X25519 recipient is accepted.

Age receives journal bytes on stdin and writes to an exclusive private temporary file. It receives no inherited cloud credentials. The operation has a shared 60-second subprocess budget, with at most five seconds for version checking and a separate five-second allowance to kill and reap an interrupted child process group. Errors and captured subprocess output are never echoed. After successful encryption and file synchronization, the helper rechecks source/configuration identities and the output directory and publishes through an atomic no-overwrite link. Existing final files and concurrent winners survive. It removes only its own temporary name and never modifies or deletes the plaintext journal.

Success stdout contains only `ok`, the encrypted file's SHA256 and size, and `off_host_preservation_verified: false`. It does not expose journal contents, source-file hashes, recipient values or infrastructure identifiers. It proves local ciphertext publication, not successful artifact upload, off-host decryption, SSH cleanup, backup transfer or recovery. A late publication/synchronization failure may leave a final ciphertext file but still reports preservation as unconfirmed; do not infer success from file existence.

## Workflow ordering and remaining integration

The inactive existing-backup template implements the following ordering. Apply and independently review the same requirements when correcting the full-deployment template. Give the access step an explicit ID. Run preservation after cleanup whenever access was attempted, including access or cleanup failure. Skip it only when access never started; do not use journal-file existence as the condition. An early access failure can leave no journal, which must remain an explicit unconfirmed preservation result.

Use the existing pinned artifact action only after the seal step succeeds, including when an earlier cleanup step failed. Give the artifact a unique run-ID/run-attempt name, exactly one completed `.age` path, `if-no-files-found: error` and seven-day retention. Preserve an existing journal after successful cleanup too. Keep cleanup failure visible in the job result; sealing cannot turn failed cleanup into success. Never use a raw journal or partial ciphertext as an upload fallback.

Encryption, publication or upload failure leaves off-host preservation unconfirmed. Preserve available private local evidence and reconcile the exact access rule through the saved journal, workflow role session and CloudTrail records. Hosted-runner termination can prevent final steps or destroy local evidence; this helper does not remove that limitation. Retrieve encrypted incident evidence before artifact expiry.

## Validation

Offline tests cover input/configuration refusal, exact bytes, subprocess failures, interrupted child cleanup, source and output races, publication failures and public output. Native roundtrip tests require explicit `ZAVLIQ_TEST_AGE` and `ZAVLIQ_TEST_AGE_KEYGEN` paths to the reviewed binaries; they create fresh disposable identities and synthetic journals, capture all process output and compare decrypted bytes. No real recovery identity, production journal, SSH rule or cloud account is involved.

```sh
python3 -I -B -W error::ResourceWarning -m unittest discover \
  -s infra/tests -p test_seal_ci_journal.py
```

Native tests are skipped when both explicit testing paths are absent. A separate local Linux ARM64 container also exercised the pinned Linux AMD64 binaries through emulation with fresh disposable keys. That checks compatibility only. Passing local tests does not establish native GitHub runner installation, approved-recipient off-host decryptability, live cleanup or artifact access. Those remain separate verification steps before workflow activation.
