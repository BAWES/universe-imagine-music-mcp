# Imagine Music MCP

FastMCP server wrapping the local ACE Step API for music generation. Same pattern as Designer MCP — Streamable HTTP transport, Cloudflare tunnel, X-API-Key auth.

Results follow the **Universe MCP contract**: a short plain sentence plus the audio on success, `isError` with one sentence on failure. The full contract is in [docs/universe-mcp-contract.md](docs/universe-mcp-contract.md); [scripts/universe_probe.py](scripts/universe_probe.py) audits a deployed server against it.

## Architecture

```
bot (Hetzner)
  ↓  HTTP tools/call /mcp
  ↓  X-API-Key header
music.bawes.net (cloudflared tunnel)
  ↓
Imagine Music MCP (this server, port 8207 — MCP and health on one port)
  ↓  localhost:8001, spawned on demand, killed after IDLE_TIMEOUT
ACE Step API (turbo, 1.7B LM)
```

## Tools

| Tool | Title on the steps line | Description |
|------|------------------------|-------------|
| `generate_music` | Generate music | prompt, optional duration / lyrics / inference_steps / bpm / key → audio file |
| `get_generation` | Check song status | look up a song id left behind by a slow generation |

What a call returns:

```
success   isError false  "Made a 15s track at 86 BPM in F major."  +  audio/mpeg resource blob
failure   isError true   "The music studio is warming up. Ask me again in a moment."
pending   isError false  "Still rendering — ask me about song <id> in a moment."
```

## Setup

1. Clone and enter the repo
2. Install dependencies:
   ```
   pip install -r requirements.txt
   ```
3. Edit `.env` — set `API_KEY` (and optionally `DISCORD_WEBHOOK_URL`)
4. Start the server — it spawns the ACE Step API on the first call:
   ```
   python imagine_music_mcp.py
   ```
5. Audit what the bot will see:
   ```
   python scripts/universe_probe.py list --url http://localhost:8207/mcp --key "$API_KEY"
   ```
6. Test one real call:
   ```
   python scripts/universe_probe.py call generate_music \
     --url http://localhost:8207/mcp --key "$API_KEY" \
     --args '{"prompt": "warm lo-fi beat, soft piano", "bpm": 86, "key": "F major"}'
   ```

## Deployment

A cloudflared tunnel points `music.bawes.net` at `localhost:8207`. Register as a remote MCP server in the bot admin panel:

```json
{
  "name": "Imagine Music",
  "serverUrl": "https://music.bawes.net/mcp",
  "transport": "streamable-http",
  "authType": "api-key",
  "authConfig": "<your-api-key>"
}
```

Deploying a code change means restarting the process; `watchdog.sh` runs from cron every 2 minutes and restarts it if it dies. The bot caches the tool list for about an hour, so new titles and descriptions appear after a bot respawn or once that cache expires — re-saving the server in the admin panel does not clear it.

## Notes

- The generation request is fixed to `acestep-v15-turbo`, `inference_steps` from the tool argument (default 8), `duration` default 15 s. XL-sft is used through the local WebUI, not by the bot.
- Audio is delivered as a base64 `resource` blob with the mime type taken from the file suffix. ACE Step writes 128 kbps 48 kHz mp3 — about 240 KB for 15 s — so the 10 MB chat cap never bites inside the 10–600 s range.
- Boot wait (180 s) and generation poll (120 s) are generous on purpose: the bot's own Patience setting is the limit, not a timeout invented here.
- An uncaught exception would surface to the player as `Error executing tool ...` and waste the 80 characters Universe shows, so every tool body runs through `_run_guarded` and returns `CallToolResult(isError=True)` instead.
- The Discord webhook dedupe list is in memory only; after a restart a song polled again will post a second embed.
