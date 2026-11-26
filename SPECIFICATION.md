# Agent Room implementation specification

**Release target** — One person can open their existing Codex chat and ask it to consult one or more of their own Foundry agents. The agents exchange attributed messages in a local room, challenge a proposal, and either endorse a specific recommendation or record where they disagree. The user sets the maximum number of rounds.

**Design decision** — Use the operator's existing Azure OpenAI Chat Completions REST endpoint through their Foundry resource. Python runs the room, FastMCP exposes local stdio tools, and SQLite stores the transcript, pending work, and usage. V1 needs only the existing Azure inference permission. Infrastructure deployment, a Codex plugin, hosted agents, a browser frontend, and additional provider integrations are outside this release.

The [setup and acceptance guide](SETUP-AND-ACCEPTANCE.md) specifies the installation examples, test fixtures, and live demonstration. The [room participation instructions](AGENTS.md) are the Codex side of the protocol.

## 1. Who takes a turn

| Component | Responsibility |
| --- | --- |
| Existing Codex chat | Acts as the room chair. It sends its own reasoning, reads Foundry's response, revises or challenges it, and tells the user what happened. Its model, instructions, reasoning effort, and private chat context belong to the Codex host. |
| Local FastMCP server | Binds requests to a local client identity, validates tool arguments, calls the application services, and returns structured results. The stdio stream carries MCP traffic. Diagnostics go to stderr. |
| Room service | Owns membership, message order, addressing, causal links, turn limits, budget admission, and closure. The same operations must be callable from a future authenticated transport. |
| SQLite worker | Claims committed activations, serializes each managed session, reserves budget, calls the adapter outside the transaction, and commits the result. Start and stop the worker with the MCP server. |
| Agent adapter | Translates an agent's profile and room history into one provider request. Returns a message draft, usage, and timing or a classified error. Azure-specific options and wire formats live here. |

The interaction is client-driven. An addressed Codex message creates durable work for Foundry. Codex can wait, read, and continue in the same user-requested task. An address to Codex is visible to that chat on its next tool call. The server has no facility to wake an idle desktop chat or to instantiate a second Codex model. During an active conversation, Codex surfaces the substantive objections and decisions in the original chat so the user can observe both voices without opening SQLite.

## 2. Identities, model registry, and context

- A **principal** identifies a trusted local client profile. A **participant** identifies a speaking agent. A **membership** gives that participant one handle in one room. A managed **session** is exactly one `(room_id, participant_id)`. Inviting the same agent into a second room creates an independent session. Two agents built from the same deployment have distinct IDs, instructions, history, counters, and limits.
- Each immutable managed agent revision stores its owner, display name, provider binding, deployment alias and configuration revision, selected settings profile, system instructions, and execution limits. A membership pins a revision. To introduce different instructions or settings in v1, create a new agent. Enforce handle uniqueness per room after normalization. Accept lowercase ASCII letters, digits, hyphens and underscores with an initial letter. Reserve `codex` for the room chair.
- A trusted local TOML registry maps aliases to allowed deployments. Each entry includes provider type, trusted endpoint reference, deployment name, API route mode, adapter-specific settings profiles, context and output limits, an input accounting policy, and an optional dated price card. Profiles may advertise reasoning effort where supported. Multiple aliases may share an endpoint or model. All deployment names, including the first, come from configuration.
- `list_models` discovers locally authorized, enabled entries and their public profiles. It requires neither Azure control-plane discovery nor model-management permission. The client selects an alias and profile. The adapter validates settings for that deployment. Reject unsupported options explicitly. Never silently lower reasoning effort, select a fallback model, or accept endpoint URLs, credentials, or arbitrary HTTP fields from a room tool.
- Revalidate authorization before dispatch. Disabling an alias prevents new calls, including queued work. Version endpoint/deployment/profile changes so existing identities cannot silently switch models. Keep historical usage tied to the configuration and price revision used at dispatch. A single deployment is sufficient to run the application.
- Codex gets its own external identity, provider descriptor, room membership, message history, and room limits. Its private host history and host instructions stay in Codex. Host model/effort fields have provenance and may be `unknown`. Agent Room enforces what the participant can do in the room. Codex controls its own inference.

## 3. Public message contract, version 1

The public envelope is immutable after commit. IDs are opaque UUIDs, sequence numbers are positive integers, and timestamps are server-assigned UTC in RFC 3339 format. Sequence determines order even when timestamps match.

| Field | Meaning |
| --- | --- |
| `schema_version`, `message_id`, `room_id`, `seq`, `created_at` | Version 1 and server-assigned identity, room, order, and timestamp. |
| `sender_id`, `body` | Authenticated participant and bounded UTF-8 plain text. Preserve the author's body. Render it as untrusted text. |
| `kind` | `discussion`, `proposal`, `endorsement`, or `dissent`. A proposal contains the exact recommendation. An endorsement or dissent directly replies to a proposal in the same room. |
| `reply_to_id` | Optional ID of an earlier same-room message. A managed response points to the message that activated it. A reply link alone requests no work. |
| `mentions` | Ordered, deduplicated list of participant IDs explicitly addressed by the sender. Tools accept IDs or room handles such as `@critic` and return canonical IDs. The presentation layer displays the handles. Addresses appearing only in free text, quotations, or code samples are inert. |
| `round_no`, `depth` | Server-assigned causal metadata defined below. A non-activating chair note can have a null round before the first round. |

Example transcript fragment, with abbreviated IDs for readability. The display address is generated from the stored mention.

| Seq | Author | Display address | Kind | Reply to | Body |
| ---|---|---|---|---|---|
| 5 | Codex | @critic | proposal | m4 | Deduplicate by source record ID. Commit the imported record and its ID in one transaction. |
| 6 | Critic | @codex | endorsement | m5 | I agree. The single transaction covers a crash between saving the record and saving its ID. |

Every accepted chair or agent message appears exactly once in the shared transcript, including unaddressed messages and dissent. Inviting a participant grants access to the room's public history from its beginning. Credentials, private Codex history, agent instructions, internal prompts, accounting events, and provider reasoning traces have no place in this envelope. A future Claude Code client, A2A bridge, or web client maps its own identity and protocol data at its boundary. An adapter contract test must exercise the same serialized v1 fixture for each integration.

## 4. Rounds, delegation, and closure

1. The chair opens a round by sending one message explicitly addressing at least one managed participant. A follow-up by the chair opens the next round even when it replies to an earlier message. A broadcast with no addresses, a read, an invitation, and a wait consume zero rounds.
2. A chair turn has depth 0. Its addressed managed participants run at depth 1. Each managed-to-managed handoff stays in that round and increases depth by one. Addressing the external chair sends a notification only. A chair follow-up resets delegation depth. Thus a two-party discussion can use three rounds with a maximum depth of one.
3. Create at most one activation per message and recipient, and admit at most one inference per managed participant per round. Multiple recipients can run independently. Repeated mentions coalesce. A self-address, a cycle back to an already scheduled participant, or a handoff beyond the depth cap cannot start more work. A valid generated reply remains in the transcript with a separate per-recipient delivery disposition. Unknown or cross-room addresses in a client send are validation errors before append.
4. The room owner requests positive finite limits. Operator policy bounds those settings. Proposed demo defaults are three rounds, one level of delegation, five minutes of wall time starting at the first activating send, 60 seconds per model call, and 16 public messages. Per-agent policy can further limit turns, wall time per call, output, messages, tokens, and estimated cost. Apply each agent's allowances independently to its session in each room. For a limit the user explicitly specified above policy, return the allowed maximum so Codex can explain the difference. Return effective limits on room and participant creation.
5. Reaching the round cap prevents a new round. Work already admitted in the last round can finish within the remaining time and budgets. The chair can still post an unaddressed conclusion if there is message capacity. Recheck the absolute deadline on every admission and completion, including after restart. At expiry stop scheduling, cancel queued work, fence late publications, and make the deadline visible to readers. Closure and reads remain available even if all budgets are exhausted.
6. `close_room` is an owner operation. Persist outcome, reason, optional recommendation ID and supporting message IDs as a room event. `agreement` requires a proposal and assent from every current participant, including at least two distinct authors. The proposal counts as the author's assent. For each other participant require its latest endorsement/dissent on that exact proposal to be an endorsement. A revision is a new proposal. Check membership and assent in the closure transaction. Otherwise allow `no_agreement`, `limit_reached`, or `stopped`. Agreement is a recorded decision, never inferred from tone, elapsed time, or a majority vote.

Use explicit units in policy objects. Room limits are `max_rounds`, `max_depth`, `max_wall_seconds`, `max_messages`, and `max_call_seconds`. A member can further set `max_turns`, `max_call_seconds`, `max_messages`, and `max_output_tokens`. A budget at either level can cap combined input and output with `max_total_tokens` and estimated USD with the decimal string `max_estimated_usd`. Its `inference_scope` is `managed` by default or `all_participants`. Omitted settings inherit the configured policy. Return the budget scope and effective numeric values with each admission decision that rejects work.

## 5. MCP tools

All tools return typed structured results. Mutations take a caller-generated high-entropy `request_id`. Scope idempotency by authenticated principal, tool, and request ID. Persist the result and normalized input digest atomically with the change. After checking the caller's authority, the same input returns the original receipt, including after close or restart. Reuse with different input returns `idempotency_conflict`. The table omits this common argument and the private room access proof for readability.

| Tool | Inputs | Result and checks |
| --- | --- | --- |
| `create_room` | title, objective, requested limits/budgets | Room ID, chair ID/handle, private room access proof, effective policy, initial cursor. Creates the chair membership. |
| `list_models` | optional provider filter | Authorized aliases, provider labels, supported profile IDs and descriptions, capability and pricing availability. Empty is a valid result. |
| `create_agent` | name, deployment alias, profile ID, system instructions, requested limits | Owned agent ID, pinned revision, effective settings descriptor and limits. Creation spends no inference budget. |
| `invite_participant` | room ID, owned agent ID, handle | Membership and session ID. Reinviting the same agent under the same handle returns the existing membership. Joining never activates it. |
| `list_participants` | room ID | Chair and invitees, handles, external/managed mode, public provider/model labels, profile, effective limits, and session status. |
| `send_message` | room ID, body, kind (default discussion), optional reply ID, explicit mentions | Committed v1 envelope, round and delivery receipts. Sender and causal metadata are derived. A managed draft enters the same domain operation through its server-owned activation. |
| `read_messages` | room ID, exclusive `after_seq` (default 0), page size | Chronological messages, next cursor, more flag, room status and closure record if present. Cursor means the last message actually returned. Suggested default 50, maximum 200. |
| `wait_for_reply` | room ID, triggering message ID, expected participant ID, timeout | Returns the first committed **direct reply** by that participant or a disposition such as `timeout`, `not_addressed`, `suppressed`, `failed`, `outcome_unknown`, `deadline`, or `closed`. Include reason and IDs. Check stored replies first, even after closure. Default 10 s, maximum 20 s per wait. Waiting is repeatable, consumes no inference, and never creates a turn. |
| `get_usage` | room ID and optional participant ID, or an owned managed participant ID alone | Room total and participant breakdown, a room-member subtotal, or that agent's cross-room total. Include remaining budgets, reservations, unknowns and measurement coverage. |
| `close_room` | room ID, outcome, reason, optional proposal ID | Immutable closure record with verified evidence IDs, transcript cursor, and usage snapshot. Later reads expose accounting updates to unresolved calls. Conflicting closure returns `room_closed`. |

Room operations require membership and the appropriate capability. Bind the chair to the configured local launcher principal and a random room-specific bearer proof issued by `create_room`. A public room or sender ID grants no access. Keep proof material private to the local tool exchange, protect idempotent creation receipts as secrets, compare proofs securely, and redact them from logs. The existing chat can resume using its original receipt after a server restart. The protocol does not depend on a desktop chat ID being provided by MCP. Local processes with the same OS account and access to those secrets are within the single-owner trust boundary.

Use stable error codes and safe explanatory text for invalid references, unsupported profiles, closed rooms, authorization, idempotency conflicts, and limits. Return relevant IDs, limit name, and whether retry is safe. For rejected recipients on an accepted message, expose the disposition separately from the immutable envelope. A wait rechecks durable state before sleeping and after every bounded wake-up so commits from another process and missed notifications are visible.

## 6. Adapter and Foundry requirements

| Adapter operation | Contract |
| --- | --- |
| Validate binding | Accept an operator-configured deployment and profile, validate supported settings, return an immutable binding and a public capability description. |
| Prepare turn | Accept a session, pinned role instructions, selected public history, triggering message ID, allowed recipient IDs, effective limits and deadline. Build an opaque, immutable prepared request and return accounting bounds for its exact payload. |
| Execute prepared turn | Perform at most one inference for a dispatch attempt. Return a normalized draft, observed usage with provenance, duration and optional provider request ID, or an error indicating what is known about delivery and charges. |

The core depends on these operations and provider-neutral records. Foundry's JSON field names, URL construction, role names, tokenizer assumptions, token audience, reasoning settings, and price categories belong to its adapter. An external interactive participant uses authenticated send/read operations. It needs no inference adapter inside the server.

**Foundry transport.** Use an asynchronous HTTP client and `DefaultAzureCredential` with token caching/refresh for the configured resource audience. Send a bearer token to the locally authorized HTTPS host. Read the Chat Completions route mode from the registry. In v1 mode, post to `/openai/v1/chat/completions` with the configured deployment in `model`. In dated mode, use `/openai/deployments/{deployment}/chat/completions` and the configured `api-version`. Disable automatic inference retries and cross-host redirects. Surface actionable, redacted diagnostics for authentication, RBAC, invalid deployment/profile, throttling, timeout, content filtering, and malformed replies. [Microsoft Chat Completions REST reference](https://learn.microsoft.com/en-us/azure/foundry/openai/latest) · [Dated REST route](https://learn.microsoft.com/en-US/azure/cognitive-services/openai/reference) · [Foundry endpoint and Entra examples](https://learn.microsoft.com/en-us/azure/foundry/foundry-models/concepts/endpoints)

**Persistent context.** SQLite is sufficient for conversational memory. At dispatch preparation, freeze the session revision, trigger and transcript high-water sequence. Build a chronological view including prior public contributions, this agent's own previous replies, and the current trigger exactly once. Mark the trigger explicitly if it is earlier than the high-water sequence. Reopening the database reconstructs the same session. A late joiner sees existing public evidence. Private instructions, messages from another room, and another participant's private context are excluded. When space runs short, retain the fixed instructions, objective, trigger, referenced proposal/parent and recent complete messages, then drop oldest optional history first and flag truncation. If required context cannot fit, return `context_limit` before inference.

**Instruction boundary.** Build the privileged instruction channel exclusively from fixed adapter policy and the agent's owner-approved role definition. Place the objective and transcript in bounded data records with real authors and message IDs. Quoting an instruction, role label, URL, or tool invocation in a transcript cannot elevate it. The model has a text response channel only. Ask it for a small validated draft with `body`, `kind`, and explicit recipient handles. The worker supplies its sender and parent. Use a supported structured response mode when configured, otherwise validate the same JSON draft from plain text. Malformed drafts produce a visible terminal delivery failure and usage accounting. Room text cannot execute tools, create participants, change policies, modify the registry, or expose local credentials.

## 7. Durability, isolation, and crash recovery

Use one local SQLite database with versioned migrations, foreign keys, WAL mode, short write transactions, and a bounded busy timeout. Persistent UTC deadlines survive restarts. Measure elapsed inference durations with a monotonic clock. Newer unsupported schema versions fail at startup with a clear diagnostic. Multiple stdio server processes may open the same database.

| Stored records | Required invariants |
| --- | --- |
| Rooms, participants/revisions, memberships, sessions | Unique handle and participant per room. A session is unique per membership. Owner, policy, deadlines and effective settings persist. |
| Messages, mentions, room events | Unique message ID and `(room_id, seq)`. Composite references enforce same-room parent, sender, and mention membership. Allocate sequence in the append transaction. |
| Request receipts and deliveries | Unique mutation receipt by principal/tool/request key. Unique delivery by trigger/recipient. At most one admitted inference per room/round/recipient. A generated reply references one activation and is unique for it. |
| Dispatch attempts, session ownership, usage ledger | At most one live or unresolved dispatch per session. Persist claim token, phase, reservation, actual/unknown usage, duration and provider request ID where available. Successful settlement is unique per attempt. |

1. In one transaction, authorize the sender, check limits, allocate sequence, append the message and mentions, record delivery decisions, enqueue new activations, and persist the request receipt. Returning a successful send means the transcript and queue are durable.
2. Claim eligible work in trigger sequence order per session using conditional writes and a fencing token. Prepare the exact request, then atomically reserve budget and mark the attempt as dispatching immediately before network I/O. Recheck session ownership, alias authorization, room state and deadline. Other rooms and independent sessions may proceed while inference runs.
3. On completion, use the same fence to append the reply, route its mentions, update session state, settle accounting, and complete the activation in one transaction. Closure or expiry wins the publication race if committed first. Account for a late billable result without adding a post-close message. Closing cancels queued work, wakes waiters, and attempts cancellation of active I/O.
4. An abandoned claim proven to be pre-dispatch can be reclaimed. A process loss, timeout or disconnect after the dispatch marker can leave Azure's outcome unknown. Keep the reservation, mark the delivery `outcome_unknown`, and quarantine that session from further inference. An expired lease alone never authorizes resending or a second concurrent call. Settle only upon trustworthy evidence of completion or non-delivery. If none is available in v1, keep the uncertainty visible and allow the chair to close. New rooms have independent sessions. This gives exactly-once local publication and avoids a second paid request when Azure might have accepted the first.

## 8. Observability and budget enforcement

| Measure | Definition |
| --- | --- |
| Messages | Count every committed authored message once, including Codex's. Membership events, waits and retries do not count. Aggregate by author, room, and owned managed identity. |
| Inference latency | Measure dispatch to provider completion or failure per attempt. Return measured-call count, sum, mean, and max in milliseconds. Record queue delay separately. Codex reply turnaround, if displayed, is labeled separately. |
| Tokens | Record input and output counts, plus cached input and reasoning details where available. Subcategories already included in totals must not be added twice. Record provider-reported, bounded estimate, or unavailable provenance. |
| Estimated cost | Use the deployment's versioned operator-supplied rates in USD per million tokens. Apply category rates once, including cached input when priced separately. Use decimal arithmetic and round only for display, with reservations rounded upward. Store price revision, quantities, estimate, and outstanding exposure for each attempt. |

Check the room and member budgets in **one** SQLite write transaction. Reserve one output message slot, a conservative upper bound for the *complete serialized input*, and the enforced model output-token maximum including billable reasoning, priced at the highest applicable configured rates. Use a verified tokenizer plus overhead bound, or a documented conservative model input/context ceiling. A heuristic character estimate is insufficient for a hard token or cost cap. Reject budgeted dispatch if the adapter cannot bound usage, apply the output cap, or price every required category for a cost limit. The atomic test is **settled usage + outstanding reservations + proposed reservation ≤ each applicable cap**. Competing calls cannot spend the same remaining budget.

Settle from reported usage and release the unused balance. Retain a conservative charge/reservation if a billable attempt has missing usage or ambiguous delivery. An explicitly unmetered run may proceed without a price card and displays cost as unknown. Count an admitted model attempt against the turn limit even when inference fails. A draft validation failure may consume tokens without producing a public message. If actual usage exceeds a declared bound, record it honestly, block further affected dispatches, and expose the discrepancy.

Every usage response states its coverage. Report the external Codex participant's message count exactly and its unobserved tokens, inference latency, and cost as unavailable. Display room totals as measured managed inference plus explicit unknown components. Token and cost budgets default to **server-dispatched inference**. An all-participant hard inference budget must fail validation if any participant lacks enforceable metering. Room rounds, elapsed time and transcript message caps apply to both sides. Estimated cost uses configured rates and is distinct from an Azure invoice.

## 9. Build sequence

| Increment | Exit condition |
| --- | --- |
| 1. Domain contract and storage | Versioned envelope fixture, identity and room operations, SQLite constraints, idempotent append/read, and migrations pass offline tests. |
| 2. Worker and mock adapter | Scripted agents complete the three-round scenario, with independent context. Concurrency, unknown outcomes, and budget races pass on a real temporary database. |
| 3. FastMCP boundary | A client exercises the documented tools through FastMCP and launches the packaged server over stdio. The transcript survives process restart. [FastMCP server and client documentation](https://gofastmcp.com/) |
| 4. Foundry adapter and registry | HTTP fixtures verify both URL modes, bearer authentication, profile translation, context reconstruction, reply validation, usage and error handling. Two aliases and two agents on the same alias work without core changes. |
| 5. Operator handoff | Follow the setup guide on Windows and macOS, then run the opt-in live scenario from an existing Codex chat against an existing authorized deployment. Record transcript IDs, effective limits and measured coverage. |
