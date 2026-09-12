# Zavliq echo account

An opt-in onboarding check: invite the operator-owned echo address to a **standard DM**, send original text or JSON, and receive the same literal value as a reply. This is an automated service account, not an independent user or model. It does not execute messages, follow URLs, invoke tools, call models, download files, or send unsolicited invitations.

The responder is **disabled by default** and requires an already provisioned runtime directory plus an exact expected owner address. Public signup reserves `echo`; an operator must provision that account privately during production setup, save its recovery proof privately before enrollment, and pair a separate service device using the normal pairing flow. No public bootstrap endpoint or account credentials are added by this service. Do not enable the website's live demo until this process is healthy and its round trip has been verified.

## Run

Requires Python 3.11+, the current native `zavliq` binary, and the local Python SDK. From the repository root:

```sh
PYTHONPATH=services/echo:packages/client-python/src python3 -m zavliq_echo.service \
  --enabled \
  --owner '@echo:zavliq.com' \
  --data-dir /PRIVATE/zavliq-echo-runtime \
  --state-dir /PRIVATE/zavliq-echo-journal \
  --control-url https://zavliq.com
```

For an installed service, install both `packages/client-python` and `services/echo` into the same private Python environment and invoke `zavliq-echo`. The equivalent variables are `ZAVLIQ_ECHO_ENABLED=true`, `ZAVLIQ_ECHO_USER_ID`, `ZAVLIQ_DATA_DIR`, `ZAVLIQ_ECHO_STATE_DIR`, `ZAVLIQ_CONTROL_URL`, and `ZAVLIQ_BINARY`. The process does not initialize identities, print tokens, or inspect other account directories. Its identity must match `--owner` before it accepts requests. Use separate persistent private directories and a supervisor that restarts on failure. Both the native store and the responder journal refuse concurrent writers.

Only standard two-member DMs are processed. Stripped invitation state may omit its member summary; immutable DM metadata and the authenticated inviter are checked before joining, and an authoritative two-member count is required afterward. Missing joined-room counts pause processing without advancing the cursor. Groups, channels, encrypted rooms, files, edited messages, replies, self-authored events, invalid JSON, and text/JSON above the 24 KiB echo budget are ignored. Replying to an echo does not echo again: send a new original message for another check. Received content remains untrusted. Existing HTML is never copied into formatted output.

The responder uses the current runtime's `requests`/`conversations` metadata (`kind`, encryption state and member counts), full inbox pages, raw `data_json` transport and normal `send`/`acknowledge` operations. Exact JSON strings preserve decimals and large integers. Its replies relate to the original event. This service accepts invitations only after the sender explicitly invites its advertised address.

## Persistence and limits

Synapse and the native runtime retain messages. The responder's private SQLite journal stores its processed arrival cursor, stable transaction proofs and admission counters; it contains no message bodies or credentials. A SHA-256 transaction ID derived from the original event ID is saved before sending. Lost send/acknowledgement responses are retried with that same ID, including after a restart. The arrival cursor advances only after the reply is accepted and delivered acknowledgement completes, or an input is explicitly ignored. A repeated event ID from backfill is not echoed twice.

Normal server limits still apply. Additional durable demo limits are 20 replies/minute, 500 replies/day, 5 replies/minute and 50 replies/day per counterpart, 10 accepted invitations/minute, 100/day, and 1000 joined rooms total. A full global reply budget pauses the queue; excess input from one counterpart is ignored so it cannot block everyone else's demo. Counters use fixed UTC minute/day windows and survive restarts. Changing the journal directory resets application counters, so production must retain and back up the configured directory. The server independently retains its own quota counters.

Logs contain only readiness/retry events and the public service address. No exception strings, inbox content, credentials, pairing secrets or encryption keys are logged. Startup failures exit with a generic operational action. Standard DMs are readable by the service operator under the normal privacy policy; do not send secrets to a public echo demo.

## Verify

```sh
PYTHONPATH=services/echo:packages/client-python/src python3 -m unittest discover -s services/echo/tests -v
PYTHONPATH=services/echo:packages/client-python/src python3 services/echo/tests/live_echo.py \
  --control-url http://localhost:8080 \
  --binary crates/zavliq-runtime/target/debug/zavliq
```

The opt-in live test uses two fresh fixture identities on a service you operate. `--fixture-dir` can reuse only a private directory previously created by this test; each run creates fresh logical sends with distinct transaction IDs. It does not claim the reserved production address, activate a permanent service, or log credentials/content. Deployment and production reserved-account bootstrap are separate operator steps.
