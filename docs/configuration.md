# Configuration

Podly reads settings from environment variables and from the Config page at
`/config`. [`.env.local.example`](../.env.local.example) lists every variable.

## How settings are resolved

Environment variables such as `LLM_MODEL`, `WHISPER_TYPE` and `GROQ_API_KEY`
take precedence over values stored in the database or set in the web UI, following
the [12-factor app](https://12factor.net/config) model:

- Podly overlays env vars on the stored values at runtime and never writes them
  to the database.
- The API drops env-controlled fields from config updates, so the UI can't
  override them.
- The web UI shows env-controlled fields as read-only.
- To hand a setting back to the UI, remove the env var and restart.

## Transcription

Podly doesn't run Whisper inside its container. Point it at a transcription
backend:

- **Groq (easiest):** set `WHISPER_TYPE=groq` and `GROQ_API_KEY`. Nothing to
  self-host.
- **Self-hosted (most private):** set `WHISPER_TYPE=remote` and point
  `WHISPER_REMOTE_BASE_URL` at any OpenAI-compatible transcription server, for
  example:
  - [WhisperX API server](https://github.com/Nyralei/whisperx-api-server):
    WhisperX with word timestamps and diarization.
  - [ParakeetX](https://github.com/MaroonBrian1928/parakeetX): a fast server
    built on NVIDIA Parakeet.

```env
WHISPER_TYPE=remote
WHISPER_REMOTE_BASE_URL=http://localhost:8000/v1
WHISPER_REMOTE_MODEL=whisper-1
# WHISPER_REMOTE_API_KEY=   # only if your server requires one
```

To cut ads on exact word boundaries, turn on **Enable Intra-Segment Boundary
Refinement** in the LLM section of the Config page. Podly then asks the server
for word-level timestamps, and falls back to segment-level cuts when the server
doesn't return them.

### Speaker diarization

With `WHISPER_TYPE=remote`, Podly can ask the server to label speakers and
passes those labels to the LLM:

```env
WHISPER_REMOTE_DIARIZE=false
WHISPER_REMOTE_SPEAKER_EMBEDDINGS=false
```

`WHISPER_REMOTE_DIARIZE=true` requests diarization.
`WHISPER_REMOTE_SPEAKER_EMBEDDINGS=true` adds speaker embeddings and needs
diarization on.

### INA audio segmentation

A transcript holds only words, so the LLM can't hear the music stings and
silences that usually wrap an ad. [inaSpeechSegmenter](https://github.com/ina-foss/inaSpeechSegmenter)
(INA) tags time ranges as `speech`, `music`, `silence` or `noenergy`. With INA
on, Podly:

- adds non-speech regions to the transcript it sends the LLM (for example
  `[122.4] [MUSIC] (5.2s)`), a strong hint that an ad starts or ends there;
- merges ad windows separated only by music or silence, and extends ads at the
  start or end of an episode over the music sting, so cuts leave fewer stray
  jingles and gaps.

INA runs as a separate service and adds an audio-analysis pass per episode, so
it's off by default:

```env
INA_ENABLED=false
INA_BASE_URL=http://localhost:8001
INA_TIMEOUT_SEC=3600
```

Set `INA_ENABLED=true` and point `INA_BASE_URL` at a service that exposes
`POST /segment`, such as [InaFastAPI](https://github.com/MaroonBrian1928/InaFastAPI).
If the service is down, processing still finishes without the INA cues.

## Profanity bleeping

Turn on profanity bleeping per feed in the feed's settings. The Output section
of the Config page sets how much padding goes around each bleep.

## UI flags

```env
PODLY_HIDE_DISCORD_INTEGRATION=false
PODLY_HIDE_REPORT_ISSUE_BUTTON=false
```

- `PODLY_HIDE_DISCORD_INTEGRATION=true` hides the Discord login button and the
  admin Discord config tab.
- `PODLY_HIDE_REPORT_ISSUE_BUTTON=true` hides the **Report issue** button in the
  nav and mobile menu.

## Stripe billing

The admin cost dashboard can show Stripe subscription revenue next to compute
cost. It's off by default:

```env
PODLY_STRIPE_BILLING_ENABLED=false
STRIPE_SECRET_KEY=
```

While it's off, Podly never loads the Stripe SDK, which saves several MB of RAM.
Set `PODLY_STRIPE_BILLING_ENABLED=true` and `STRIPE_SECRET_KEY` to turn it on.
Podly caches subscription amounts for an hour to limit Stripe API calls.

## Rust writer and sidecar

Both ship in the Docker image and are on by default.

- **Writer (`podly_writer`)** handles every database write over the same RPC
  contract as the Python writer. To roll back, set `PODLY_WRITER_BACKEND=python`.
- **Sidecar (`podly_tools`)** handles the read-heavy and audio-heavy paths. It
  reads SQLite directly and returns the same JSON as the Python routes, which
  keeps large allocations out of the web process. If it fails, Podly falls back
  to Python and logs `falling back to Python` in `src/instance/logs/app.log`.

Each sidecar path has a flag. Set one to `false` to force the Python
implementation, for debugging or parity checks:

```env
PODLY_RUST_AUDIO_ENABLED=true
PODLY_RUST_FEED_XML_ENABLED=true
PODLY_RUST_CHAPTERS_ENABLED=true
PODLY_RUST_FEED_REFRESH_ENABLED=true
PODLY_RUST_JOBS_ENABLED=true
PODLY_RUST_STATS_ENABLED=true
PODLY_RUST_TRANSCRIPT_ENABLED=true
PODLY_RUST_AD_MERGE_ENABLED=true
PODLY_RUST_PROFANITY_ENABLED=true
PODLY_RUST_FEED_POSTS_ENABLED=true
PODLY_RUST_WORD_BOUNDARY_ENABLED=true
PODLY_RUST_CHAPTER_FALLBACK_ENABLED=true
PODLY_RUST_COSTS_ENABLED=true
PODLY_RUST_REPEAT_AD_ENABLED=true
```

`PODLY_RUST_COSTS_ENABLED` serves `/api/admin/costs` and
`/api/admin/costs/calls` from the sidecar. Python still looks up LiteLLM token
prices first, and adds Stripe revenue afterwards when billing is on.
