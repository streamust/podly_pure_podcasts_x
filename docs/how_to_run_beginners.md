# How To Run: Ultimate Beginner's Guide

This guide sets up Podly from scratch with Docker. You need about 15 minutes,
an API key for an LLM, and a way to transcribe audio.

## Let an AI assistant do it

An AI coding assistant can run the commands, read the output, and fix problems
as they come up. Use an agentic terminal tool such as
[Claude Code](https://www.anthropic.com/claude-code) or
[Gemini CLI](https://github.com/google-gemini/gemini-cli), or an AI editor such
as [Cursor](https://www.cursor.com/) or [Windsurf](https://windsurf.com/) with
agent mode on. Then paste this prompt:

```
Help me install Docker and run Podly from
https://github.com/MaroonBrian1928/podly_pure_podcasts_x (skip the clone if
I already have it). Follow docs/how_to_run_beginners.md:
- install Docker and Docker Compose if they're missing
- copy .env.local.example to .env.local and help me fill in my API keys and
  admin password
- run `./run_podly_docker.sh -d`
- check that http://localhost:5001 loads
For transcription, use Groq or a self-hosted OpenAI-compatible server such as
WhisperX API server or ParakeetX. Podly doesn't need an OpenAI key.
```

The rest of this guide covers the same steps by hand.

## 1. Install Docker

- **Windows:** install [Docker Desktop](https://docs.docker.com/desktop/install/windows-install/),
  keep "Use WSL 2" checked, restart when asked, then open Docker Desktop.
- **macOS:** install [Docker Desktop](https://docs.docker.com/desktop/install/mac-install/),
  drag it to Applications, and open it.
- **Linux (Ubuntu/Debian):**

  ```bash
  sudo apt update
  sudo apt install docker.io docker-compose-v2
  sudo usermod -aG docker $USER   # then log out and back in
  ```

Check that both commands print a version:

```bash
docker --version
docker compose version
```

## 2. Get API keys

You need two services. One provider can cover both.

- **Transcription:** a [Groq](https://console.groq.com/) API key is the easiest
  (it has a free tier). You can also self-host an OpenAI-compatible server such
  as [WhisperX API server](https://github.com/Nyralei/whisperx-api-server) or
  [ParakeetX](https://github.com/MaroonBrian1928/parakeetX), which needs no key.
- **Ad detection:** an API key from OpenAI, Anthropic, Google Gemini, or Groq,
  or a local model.

Most providers show a new key only once, so copy it somewhere safe. Hosted APIs
bill per use: set a spending limit in each provider's dashboard before you
start.

## 3. Download and configure Podly

```bash
git clone https://github.com/MaroonBrian1928/podly_pure_podcasts_x.git
cd podly_pure_podcasts_x
cp .env.local.example .env.local
```

Open `.env.local` and set:

- your transcription settings (`WHISPER_TYPE`, and `GROQ_API_KEY` for Groq);
- your LLM settings (`LLM_MODEL` and `LLM_API_KEY`);
- `PODLY_ADMIN_PASSWORD`: replace the example with a long password of your own;
- `PODLY_SECRET_KEY`: generate one with
  `python3 -c "import secrets; print(secrets.token_hex(32))"`.

Leave `REQUIRE_AUTH=true`. Without it, anyone who can reach your Podly URL can
read and change your feeds.

## 4. Start Podly

```bash
./run_podly_docker.sh -d
```

The script pulls the published image on the first run. When it finishes, open
`http://localhost:5001` and sign in with `PODLY_ADMIN_USERNAME` (default
`podly_admin`) and your password.

Podly creates the admin account on first boot. To turn on authentication for an
existing install, delete `src/instance/sqlite3.db` first so it can create the
account.

## 5. Add a podcast

1. Paste a podcast's RSS URL into the add-feed form.
2. Open the feed and click **Subscribe to Podly RSS**. Podly copies a feed URL
   with a per-feed token, so your podcast app never sees your password.
3. Paste that URL into your podcast app.

Podly removes ads from new episodes as they arrive.

Some apps, including Pocket Casts, fetch feeds from their own servers rather
than your phone. For those, Podly must be reachable from the internet, through
your own server or a reverse proxy (see
[Reverse proxy examples](contributors.md#reverse-proxy-examples)). The protected
feed URL keeps the feed private.

## Stop and upgrade

- **Stop:** press `Ctrl+C` if Podly runs in the foreground, or run
  `docker compose down` if you started it with `-d`.
- **Upgrade:** run `git pull`, then `./run_podly_docker.sh -d`. It pulls the
  latest published image and restarts Podly.

## Troubleshooting

| Problem | Fix |
|---|---|
| `docker: command not found` | Start Docker Desktop. On Windows, reopen the terminal. On Linux, log out and back in after the `usermod` step. |
| `Cannot connect to the Docker daemon` | Open Docker Desktop, or start the Docker service. |
| `Permission denied` running the script | Run `chmod +x run_podly_docker.sh`. |
| Podly exits on startup with an auth error | Set `PODLY_ADMIN_PASSWORD` in `.env.local`. |
| LLM or transcription errors | Check the key on the Config page, and check billing and rate limits in the provider's dashboard. |
| Port 5001 already in use | Stop the other program, or change the port mapping in `compose.yml`. |

## Getting help

Ask in the upstream Podly [Discord](https://discord.gg/FRB98GtF6N).
