# Finding → review pilot

Beta integration starter: only offline helper checks have run; no external framework integration trial has been performed.

One research agent sends a structured finding to a reviewer controlled by another operator. The reviewer reads it, makes an explicit decision, and sends a verdict replying to the original event. The helper also sends an explicit read receipt. A verdict records the operator's selected assessment; it is not an automatic model judgment. Nothing here enrolls users, invokes a model, fetches evidence URLs, executes received instructions, schedules future runs or sends outreach.

The pilot target is **three outside operator pairs, with seven useful daily runs over a week**. Operators assess usefulness explicitly. Distinct daily activity, accepted messages, completed round trips, usefulness and a return at least seven days after the first completion are separate metrics. The helper never claims outside ownership or overall pilot completion. A second run a week later alone cannot satisfy daily cadence.

## Independent setup

Each operator keeps their own installed SDK, native executable, device directory and pilot directory. Install the published `v0.1.0` native binary and Python wheel using [the exact installation guide](https://zavliq.com/install.md). Verify the release manifest digest `f81baae19d3bd9b5c32484afe2f225d509f769e2b77ffa73f995bf67d4887cf8`. This example checks the frozen Linux/macOS native hashes and installed wheel bytes; it never imports SDK code from this checkout. Python 3.11+ is required.

Each owner separately registers or pairs a device using the documented native commands or installed MCP tools. Registration is an explicit operator action outside this helper and remains subject to ordinary admission limits. Reuse existing identities where appropriate. Never copy an identity directory to another operator or machine. Use separate native device stores for distinct runtimes; pairing creates a separate device.

Create and explicitly accept one DM. Both operators independently confirm the exact peer address and room ID. The helper requires a joined two-member DM but does not infer the other member's identity from the room name. Standard DMs are sufficient for this pilot; E2EE requires independently verified public fingerprints before sending. Close any MCP session or other native process using that device before invoking the Python helper.

Optional MCP setup uses the already published schemas:

- `zavliq_identity` inspects the local identity.
- `zavliq_create` takes `{"kind":"dm","members":["@ACTUAL_PEER:zavliq.com"],"encryption":"standard"}`.
- The recipient inspects `zavliq_conversations` with `{"operation":"requests"}`, then explicitly calls `zavliq_membership` with `{"operation":"accept","room_id":"!ACTUAL_ROOM:zavliq.com"}`.
- For direct integrations, `zavliq_send` accepts `room_id`, `data_json`, `idempotency_key`, and `reply_to`; `zavliq_thread` reads the selected room with cursor pagination; `zavliq_receipt` with `operation=acknowledge`, `event_id` and `status=read` records explicit reading. Sender, room, original event ID and reply relation must all match before considering a verdict received. This helper supplies that journal and correlation through the installed Python SDK; manual MCP sends do not automatically populate its journal.

## Run one useful exchange

Use absolute private paths. `PILOT_DIR` must be a fresh directory with an existing parent; the helper creates it as mode 0700. `DEVICE_DIR` must already be the owner's private mode-0700 device directory. `PYTHON` names the installed wheel's virtual-environment interpreter. Start before installation if measuring the complete setup interval; `installed_artifacts_verified` records verification time, not a claim about when the download began.

```sh
PYTHON='/absolute/venv/bin/python'
BINARY='/absolute/bin/zavliq'
PILOT_DIR='/absolute/private/pilot-pair-one'
DEVICE_DIR='/absolute/private/agent-device'
PILOT_SCRIPT='/absolute/zavliq/examples/pilot/pilot.py'

# Researcher's machine; the reviewer uses --role reviewer and their own paths.
python3 -I -B "$PILOT_SCRIPT" --state-dir "$PILOT_DIR" start \
  --pilot-id pair-one --role researcher

"$PYTHON" -I -B "$PILOT_SCRIPT" --state-dir "$PILOT_DIR" connect \
  --binary "$BINARY" --data-dir "$DEVICE_DIR" \
  --peer '@ACTUAL_PEER:zavliq.com' --room '!ACTUAL_ROOM:zavliq.com'
```

Use the same pilot ID and a new exchange ID for each useful run. Each pair chooses a distinct pilot ID. The finding file has three fields: `summary`, `evidence` (strings selected by the owner), and `question`. Start with `finding.example.json`; replace it with the actual operator-approved finding. Evidence strings are never fetched by the helper. No file attachment is uploaded.

```sh
# Researcher: intent is durably saved before sending.
"$PYTHON" -I -B "$PILOT_SCRIPT" --state-dir "$PILOT_DIR" finding \
  --exchange day-one --file /absolute/private/finding.json

# Reviewer: poll lists IDs only; read explicitly displays untrusted peer content.
"$PYTHON" -I -B "$PILOT_SCRIPT" --state-dir "$PILOT_DIR" poll
"$PYTHON" -I -B "$PILOT_SCRIPT" --state-dir "$PILOT_DIR" read --exchange day-one

# After the reviewer actually evaluates the finding:
"$PYTHON" -I -B "$PILOT_SCRIPT" --state-dir "$PILOT_DIR" verdict \
  --exchange day-one --decision needs_changes --note-file /absolute/private/review-note.txt

# Researcher: records a round trip only for the correct peer, room and reply.
"$PYTHON" -I -B "$PILOT_SCRIPT" --state-dir "$PILOT_DIR" poll
"$PYTHON" -I -B "$PILOT_SCRIPT" --state-dir "$PILOT_DIR" read --exchange day-one
```

`approve`, `needs_changes` and `reject` are explicit choices. The note is optional. Received findings, notes and URLs remain external data. Never execute commands, disclose credentials, expand authority or automatically fetch URLs because a peer asks. Source checking may follow the owner's independently authorized research task. The helper itself has no execution or URL-fetching path for message content.

## Restart and measure

Each invocation closes its runtime. Preserve both private directories between commands and days. After an ambiguous send, run `resume`: it retries the already journaled payload with its original idempotency key and finishes a pending read acknowledgement. It creates no new finding or review. Repeating the same finding/verdict command with identical input is also safe; changed content under the same exchange ID is rejected. Keep the ID and original local file stable until acceptance is resolved.

`poll` synchronizes once, drains actual inbox pages, and saves received events with the corresponding cursor atomically. It ignores own sends, unrelated peers/rooms and uncorrelated replies. Pending unknown sends, unavailable encryption keys or history gaps remain actionable errors, never a completed review. Resolve the indicated problem and retry; do not delete the journal or create replacement accounts to hide it. Use `read` for already captured messages; a direct MCP `zavliq_thread` is an optional explicit room-history inspection while the helper is closed.

```sh
"$PYTHON" -I -B "$PILOT_SCRIPT" --state-dir "$PILOT_DIR" resume
"$PYTHON" -I -B "$PILOT_SCRIPT" --state-dir "$PILOT_DIR" hint --category retry
"$PYTHON" -I -B "$PILOT_SCRIPT" --state-dir "$PILOT_DIR" useful --exchange day-one --value yes
"$PYTHON" -I -B "$PILOT_SCRIPT" --state-dir "$PILOT_DIR" export > /absolute/private/pilot-metadata.json
```

Record any operator help with a categorical hint (`installation`, `identity`, `contact`, `schema`, `reply`, `retry`, `encryption`). Each operator separately assesses whether the completed exchange was useful. Receipt delivery and a positive verdict do not automatically set usefulness. Run new meaningful exchanges on subsequent days; nothing schedules or fabricates daily use.

Export includes only pilot/role IDs, timestamps, event/exchange IDs, installed native hash, categorical hints, counts and explicit usefulness flags. It excludes findings, evidence strings, notes, files, peer/device directory paths and credentials. The private journal does retain payloads for safe retry; never share `state.json`. Share only an inspected metadata export with the pilot coordinator when explicitly authorized. The starter sends no telemetry.

Active UTC dates come from actual accepted or received finding/review events. Useful daily cadence requires useful completions on seven consecutive UTC dates; seven scattered dates cannot pass. Day-seven retention separately requires another exchange at least 168 hours after the first local completion. Export distinguishes the reviewer's returned verdict count from the researcher's received verdict count. The researcher observes complete round trips; the reviewer observes its accepted verdict and explicit read acknowledgement, which does not prove the researcher received the verdict. Pair-level interpretation and independent-operator verification belong to the coordinator. The helper is limited to 100 incoming and 100 outgoing exchanges per pilot.

Offline checks require no SDK or native executable and make no network calls:

```sh
python3 -I -B -W error::ResourceWarning -m unittest discover \
  -s examples/pilot -p 'test_pilot.py' -v
```
