<h2 align="center">
<img width="50%" src="src/app/static/images/logos/logo_with_text.png" />

</h2>

<p align="center">
<p align="center">Ad-block for podcasts. Create an ad-free RSS feed.</p>
<p align="center">
  <a href="https://discord.gg/FRB98GtF6N" target="_blank">
      <img src="https://img.shields.io/badge/discord-join-blue.svg?logo=discord&logoColor=white" alt="Discord">
  </a>
</p>

A performance-focused fork of
[Podly Pure Podcasts](https://github.com/podly-pure-podcasts/podly_pure_podcasts).
Podly transcribes each episode, asks an LLM to find the ads, cuts them out, and
gives you back an ad-free RSS feed. This fork moves database writes and the busy
read paths to Rust, so it idles on about a fifth of upstream's memory and serves
feeds hundreds of times faster.

It also adds optional tools upstream doesn't have. Speaker diarization and
[INA](https://github.com/ina-foss/inaSpeechSegmenter) music and silence
detection give the LLM more cues for finding ads (see the
[configuration guide](docs/configuration.md#transcription)). With
intra-segment boundary refinement on, Podly uses word-level timestamps to cut
on exact word boundaries. You can also turn on profanity bleeping in each feed's settings.

<img width="100%" src="docs/images/screenshot.png" />

## Benchmarks

| | Upstream | This fork |
|---|---|---|
| Memory, idle | 406 MiB | 86 MiB |
| Memory, 60 s after load | 468 MiB | 98 MiB |
| Feed XML, 500 items | 0.3 req/s · p50 38.3 s | 125 req/s · p50 56 ms |
| Episode list | 235 req/s · p50 32 ms | 573 req/s · p50 13 ms |

Medians over 3 rounds with 8 concurrent clients. See
[docs/benchmarks.md](docs/benchmarks.md) for the method and the full table.

## Quick start

You need Docker, an API key for an LLM (OpenAI, Anthropic, Gemini, Groq, or a
local model), and a transcription backend (Groq, or any OpenAI-compatible
Whisper/Parakeet server).

```bash
git clone https://github.com/MaroonBrian1928/podly_pure_podcasts_x.git
cd podly_pure_podcasts_x
cp .env.local.example .env.local   # set your admin password and API keys
./run_podly_docker.sh -d
```

Then open `http://localhost:5001`. The
[beginner's guide](docs/how_to_run_beginners.md) walks through each step.

> ⚠️ Keep `REQUIRE_AUTH=true` and set your own admin password before you expose
> Podly to the internet. See
> [Download and configure Podly](docs/how_to_run_beginners.md#3-download-and-configure-podly).

## Configuration

Every setting is in [`.env.local.example`](.env.local.example). The
[configuration guide](docs/configuration.md) covers transcription, INA audio
segmentation, UI flags, Stripe billing, and the Rust writer and sidecar flags.

## Contributing

See the [contributor guide](docs/contributors.md).

## License

[MIT](LICENCE), inherited from upstream Podly.
