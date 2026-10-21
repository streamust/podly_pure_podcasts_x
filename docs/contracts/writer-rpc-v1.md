# Podly writer RPC protocol v1

Status: implementation contract for the Rust writer migration. Version 1 is
frozen by the fixtures in `contracts/writer-rpc/v1/`.

## Transport and endpoints

The writer listens only on `127.0.0.1:50001` and is never published by Docker.
It uses HTTP/1.1 with JSON UTF-8 bodies.

- `POST /v1/commands` authenticates, validates, admits, and optionally waits for
  one command.
- `GET /v1/ready` is unauthenticated but loopback-only. It returns only backend,
  protocol, schema revision, executor readiness, queue capacity, and lifecycle
  state. It never returns the database path, secret, or command content.

Command requests and responses conform to the JSON Schemas beside this file.
Unknown envelope fields are rejected. Model data remains operation-compatible:
UPDATE ignores unknown allowlisted-model fields, DELETE of an absent row
succeeds, and UPDATE of an absent row fails. CREATE retains its current
constructor validation rather than silently ignoring unknown fields.

## Authentication

`PODLY_IPC_AUTHKEY` remains the shared local secret. It must be nonempty in Rust
mode. The client UTF-8 encodes it, Base64URL-encodes those bytes without padding,
and sends:

```text
Authorization: PodlyWriter <base64url-no-pad(utf8(secret))>
```

The server decodes the token and compares the resulting bytes in constant time.
Malformed/missing credentials return 401; incorrect credentials return 403.
Neither the header, decoded bytes, nor full command bodies may be logged.

## Limits and admission

Initial limits are based on the P0 synthetic processing payload (1,228,431
serialized bytes), leaving headroom for longer episodes:

| Limit | v1 value |
| --- | ---: |
| Request body | 16 MiB |
| Queue entries | 128 |
| Aggregate queued request bytes | 64 MiB |
| Concurrent HTTP connections | 64 |
| Header read | 5 seconds |
| Body read | 30 seconds |
| Default client deadline | 30 seconds |
| Graceful drain | 30 seconds |

The HTTP task validates the complete envelope and reserves both one queue slot
and its serialized-byte budget before admission. Capacity failure returns 429
without admission. Readiness/lifecycle rejection returns 503. No async SQLite
task or connection is created per request: one dedicated writer thread owns one
connection and drains the bounded queue.

`PODLY_WRITER_TIMEOUT_SECONDS` remains the client deadline and defaults to 30.
It covers connection establishment, request upload, admission, and reply wait.
A server-side request timeout does not cancel an admitted command.

## Request forms

Every top-level request includes:

- `version`: integer `1`.
- `command_id`: caller-generated nonempty identifier, at most 128 characters.
- `operation`: `action`, `create`, `update`, `delete`, or `transaction`.
- `wait`: boolean.

Operation fields:

- `action`: `action` string and `params` object.
- `create`: `model` string and `data` object.
- `update`: `model`, scalar `id`, and `data` object. `id` is not copied into the
  wire `data`; the adapter restores the current executor shape internally.
- `delete`: `model` and scalar `id`.
- `transaction`: nonempty ordered `commands` array. Subcommands carry their own
  `command_id` and one of the four non-transaction operation forms. Nested
  transactions and subcommand `version`/`wait` fields are rejected.

One top-level request owns one database transaction except
`update_combined_config`, whose current section-by-section commits are an
explicit v1 compatibility exception until that action is deliberately revised.
Actions never gain an implicit helper commit. A normal transaction returns
ordered nested results and commits once only after every subcommand succeeds.
Any subcommand or commit failure rolls back and produces a completed failure.

## Response and failure states

All responses repeat `version` and the caller's `command_id` when it was valid.

- `wait=true`: HTTP 200 after commit or rollback, state `completed`, boolean
  `success`, `result` on success, and structured `error` on failure. The Python
  adapter maps this to the existing `WriteResult` without exposing transport
  internals.
- `wait=false`: HTTP 202 only after the command is in the bounded queue, state
  `accepted`, `admitted=true`. The public Python method returns `None`. This is
  not a commit claim or durable-queue guarantee. Later execution failures are
  emitted as redacted structured logs/metrics correlated by command ID.
- Pre-admission failures use state `rejected`, `admitted=false`, and an HTTP
  status appropriate to `unauthorized`, `forbidden`, `malformed`,
  `unsupported_version`, `unsupported_operation`, `unsupported_action`,
  `unsupported_model`, `payload_too_large`, `capacity_exhausted`,
  `not_ready`, or `schema_incompatible`.
- A server deadline reached after admission uses HTTP 504, state `unknown`,
  `admitted=true`, and error outcome `unknown`. It does not cancel the command.

The v1 HTTP mapping is fixed as follows:

| HTTP | Condition |
| ---: | --- |
| 200 | Completed command, including domain failure and rollback |
| 202 | Accepted `wait=false` command |
| 400 | Malformed JSON/envelope or unsupported protocol version |
| 401 | Missing or malformed authorization (`WWW-Authenticate: PodlyWriter`) |
| 403 | Well-formed authorization with the wrong secret |
| 413 | Request body exceeds the configured limit |
| 422 | Unsupported operation, action, or model |
| 429 | Queue-entry or aggregate-byte capacity exhausted |
| 503 | Writer is not ready, is draining, or has incompatible schema |
| 504 | Deadline after admission; outcome remains unknown |

Responses before a valid envelope is available omit `command_id`; all later
responses include it.

Errors contain stable `code`, a sanitized `message`, `retryable`, and `outcome`:
`not_admitted`, `rolled_back`, or `unknown`. They never contain SQL, database
paths, secrets, complete payloads, or raw provider/file content.

A timeout, truncated response, or connection loss after admission has outcome
unknown. Command IDs correlate observations but provide neither deduplication nor
exactly-once delivery. Clients never automatically retry an unknown-outcome
write. Only a proven pre-admission rejection may be retried, and then only when
the caller/action contract says doing so is safe.

## JSON codecs

- Missing and explicit `null` remain distinct. Empty arrays/objects remain
  present values. Action-specific merge/replace behavior is not generalized.
- Integers are JSON integers. Floats must be finite JSON numbers; NaN and
  infinities are rejected. SQLite INTEGER versus REAL response shape is
  preserved by tolerant readers.
- Naive UTC datetimes use `YYYY-MM-DDTHH:MM:SS.ffffff` (fraction may be omitted
  when zero). Aware input must include an ISO-8601 offset and is normalized to
  naive UTC before persistence. Responses use naive UTC for current compatibility.
- UUIDs use lowercase hyphenated strings unless an existing field explicitly
  uses UUID hex (for example token IDs).
- Enums are their existing lowercase strings.
- Bytes, if a reachable field requires them, use the sole-key tagged object
  `{"$bytes_base64":"<standard-base64>"}`. Untagged strings are never guessed
  to be bytes.
- JSON columns use ordinary nested JSON and preserve null/missing and list order.
  Duplicate object keys are rejected before schema validation. Both transports
  must use a duplicate-aware decoder; ordinary last-key-wins JSON parsing is not
  conformant.
- Transaction results are an ordered array of full nested result objects, not
  the legacy local-test shortcut containing only `.data`.

## Readiness, schema, and shutdown

Ready is true only after the configured database already exists, SQLite opens
with the required busy-timeout/journal/durability settings, the
Alembic revision matches the binary's expected revision, all required actions
are registered, and the executor accepts work. A wrong path must not silently
create an empty database.

SIGTERM changes lifecycle state to draining, makes readiness false, rejects new
admission, and drains already accepted commands for at most 30 seconds. The
writer then closes the connection and exits. Force termination relies on SQLite
rollback for uncommitted work; accepted asynchronous work is not claimed durable.

For compatibility with the current Python writer connection, v1 explicitly
uses `PRAGMA foreign_keys=OFF`; changing enforcement requires a separately
tested contract revision rather than silently changing cascade/error behavior.

Readiness responses conform to `writer-rpc-v1-ready.schema.json`. HTTP 200 with
`ready=true` is the only ready state; startup, incompatibility, and draining use
HTTP 503 with `ready=false`.
