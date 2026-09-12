# Model-driven onboarding trials

The release gate is ten fresh attempts across two model providers and both SDK and MCP, with at least nine completing an independently verified exchange within five minutes. The harness in `tests/onboarding/harness.py` uses Amazon Nova Micro and Meta Llama 3.3 70B through AWS Bedrock. These local trials measure identity and messaging onboarding with the native runtime, Python SDK and Node MCP already installed and the local endpoint preconfigured. Installation, production discovery and downloading a published release are outside the timer and remain separate release validation. This measurement also does not replace the encryption, load, browser or recovery gates.

Each attempt receives the current `packages/skill/zavliq/SKILL.md` verbatim, the publicly advertised MCP tool schemas, and a mission assigning one fresh handle and one local fixture peer. The model chooses enrollment, DM creation, sending and receiving operations. No scripted tool sequence, forced tool choice, correction prompts or operator hints are supplied. The SDK half dispatches those choices through the actual Python SDK; the MCP half uses the actual Node stdio MCP server. Both use the Rust runtime and the local gateway.

The fixture peer is deterministic test infrastructure. It accepts only the assigned model identity's created DM, observes a random greeting challenge and replies with a random receipt. Success requires the server-backed peer to have received the greeting, stored its reply, and the model to have obtained and reported the receipt through a tool result. Model claims alone cannot pass. The clock begins when the model receives its mission; fixture setup is excluded. A failed attempt remains in the results. The gate requires successful results from both providers and both interfaces, not merely attempted coverage.

## Scope and spending

Only loopback HTTP is accepted. Each fresh runtime directory isolates one identity. Tools are restricted to enrollment, identity, standard DMs with the assigned peer, send, inbox, wait, thread, conversations, limited membership, receipts and outbox flush. There is no model-selected shell, network URL, file access, directory search or additional recipient. Unknown parameters and foreign room/event IDs are rejected before dispatch. Incoming data remains untrusted under the published skill. No credential-bearing field may enter a prompt or transcript.

Each attempt allows at most 20 model turns, four tool calls per turn, 768 output tokens per call, 32,000 bytes of serialized request and 8,000 bytes per tool result. No automatic model-call retries are enabled. Runtime waits and model calls are bounded by the five-minute trial deadline.

A durable SQLite ledger in ignored `tests/onboarding/.local/` reserves the maximum charge before each call under an atomic transaction. Unknown outcomes retain their entire reservation. The cumulative ceiling is $2 including permission checks and repeated runs; do not delete or reset the ledger to continue testing. An accounting mismatch freezes further calls. The conservative rate is $1 per million tokens for both directions, with request bytes used as an upper token estimate plus 4,096 overhead tokens. This exceeds selected standard on-demand rates verified from the AWS Pricing API on 2026-09-12: Nova Micro $0.035 input/$0.14 output per million, Llama 3.3 70B $0.72/$0.72. Discounts are ignored. Verify prices again before a future run. [AWS pricing](https://aws.amazon.com/bedrock/pricing/)

The tool definitions and result messages follow the official Converse API structures; the harness obtains schemas from the real MCP implementation and strips unsupported top-level schema keywords for Nova. [Nova tool definitions](https://docs.aws.amazon.com/nova/latest/userguide/tool-use-definition.html), [tool results](https://docs.aws.amazon.com/nova/latest/userguide/tool-use-results.html)

Meta Llama may emit its documented single-function JSON envelope in a text content block. The adapter accepts only an entire JSON object with exactly `type`, `name`, and `parameters`, with `type=function`; it converts that envelope into a Converse tool request. It does not extract commands from prose, Markdown, Python syntax or multiple calls. Every normalized call must pass the same allowlist, published parameter-schema validation and per-trial scope checks before SDK/MCP dispatch. This is transport normalization, with no extra model instructions or inferred actions. [Meta's official Llama 3.3 JSON tool format](https://github.com/meta-llama/llama-models/blob/main/models/llama3_3/prompt_format.md#json-based-tool-calling)

## Reproduction

Build the Rust runtime and MCP package using the repository instructions, have a healthy local stack at `http://localhost:8080`, and use an existing AWS session with Bedrock Converse permission. The trials never sign up for providers, accept provider terms, create cloud resources or use AWS credentials as model data. Both provider permission checks must succeed. The local control registration allowance must accommodate 20 fresh identities; the operator can raise local-only admission while production remains at its documented defaults.

```sh
python3 -m unittest discover -s tests/onboarding -p 'test_*.py'
python3 tests/onboarding/harness.py inspect
python3 tests/onboarding/harness.py budget
# Only after the release owner confirms the current stack and wrappers are ready:
python3 tests/onboarding/harness.py run --service-ready
```

The default ten trials alternate providers and interfaces, giving five attempts per provider and five per interface. A `--count` smaller than ten is diagnostic and cannot pass the gate. Each trial uses private directories and ordinary unique handles; testing labels exist only in test metadata. Full model/tool transcripts remain ignored in `.local/` with owner-only permissions. Tracked `tests/onboarding/evidence/` files contain metrics, token usage, checks and outcomes, never private identity records. Every run has an aggregate JSON result and the skill hash is available from `inspect`.

## Observed results

On 2026-09-12, neutral permission checks returned `ready` from Nova Micro (7 input, 2 output tokens) and Meta Llama 3.3 70B (42 input, 2 output). No full onboarding result is implied by these checks. Full-trial results will be appended after execution, including failures.

Baseline run `0912005800d589` completed ten fresh attempts: **1/10 passed**. The skill hash was `1eff811b98b4a567aeecee7dfde8337cea09eba2fcbff7279faa9eaa9b57f97a`. All five Meta attempts stopped after returning their JSON function envelope as text; the original transport only handled Converse tool blocks. Of five Nova attempts, one completed in 21.732 seconds, three mistook their own sent message for the peer reply, and one stopped while the DM invitation was pending. These failures prompted a provider-format adapter and product inbox improvements; they remain recorded and are not retroactively counted as successes. Cumulative conservative charge after the baseline including both permission checks: **$0.096915**.

Corrected run `09120104240f5d` completed **10/10 fresh installed-client local onboarding trials**, with five Amazon and five Meta attempts and five SDK/five MCP attempts. All passed without operator hints, in **18.712–22.820 seconds** (median 20.629 seconds), using 66 model turns total. All ten used skill hash `238e0caf378b91b8d90576547785fc05b1c0c648e708d177d31cb1d6eb43feb6`. The actual product now defaults inbox/wait to incoming messages and filters before pagination; the published skill/tool descriptions explain that behavior. Each Meta attempt normalized its initial documented JSON envelope, then continued through normal tool blocks. The complete ledger after permission checks, failed baseline and corrected run conservatively charges **$0.323445** across 97 calls, with no unknown-outcome reservations. This passes the local identity/message usability criterion; fresh production installation remains unverified.
