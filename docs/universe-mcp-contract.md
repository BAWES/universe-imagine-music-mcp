# Universe MCP contract

What Universe reads from an MCP server, and how this repo satisfies it. Applies to every MCP tool a Universe bot can call; the concrete examples come from this music server.

If you change tool definitions or result shapes, run the [verification](#verification) section before saying it works.

## 1. Connect

| Rule | How this repo does it |
|------|----------------------|
| Streamable HTTP, one URL, POST, JSON or SSE | `app.streamable_http_app()` + uvicorn on `/mcp` |
| Must answer `initialize`; Universe speaks `2024-11-05` | MCP SDK 1.29 negotiates down and echoes `2024-11-05` |
| Auth: none / `Bearer` / `X-API-Key` / OAuth token | `BearerAuthMiddleware` accepts `X-API-Key` and `Authorization: Bearer`; `/health` excluded |
| Identity in `initialize` as `clientInfo.player_id` | read for the Discord webhook; never invented locally |
| Public address, no redirects, no private networks | `music.bawes.net` via a cloudflared tunnel |
| Patient wait, but writes must be repeatable | tool calls wait up to 180 s for boot and 120 s for generation; a retry only happens when the request never arrived |

Universe's default patience is 90 s (the bot owner can set 5–600 s). Our waits are deliberately generous — the bot's Patience setting is the limit, not a timeout invented inside the server.

The tool list is cached by Universe for about an hour. New `title`/`description` text appears after the bot is respawned or after that cache expires. **Re-saving the server in the bot admin panel does not clear it.**

## 2. Tool definition

- `name` — letters, digits, `_`, `-`, ≤64 chars, unique across every server on the bot (first server wins). No dots.
- `title` — what the player reads on the steps line. Universe reads `title`, then `annotations.title`, then derives from the name (`generate_music` → "Generate music"). This repo sets both explicitly: `Generate music`, `Check song status`.
- `description` and `inputSchema` reach the model verbatim, so they are written for a listener, never for an operator. No model ids, ports, or internal knob names.
- The main argument is named `prompt` — the steps line shows its first 60 characters. Links are cut at the path; email- or token-shaped values are hidden.
- Per-argument text lives in `Annotated[..., Field(description=...)]`. FastMCP **does not** parse Google-style `Args:` docstrings into parameter descriptions, and the whole docstring would otherwise land in `description`.

## 3. Results

One short line plus the media item on success; one plain sentence with `isError` on failure.

```python
# success: short line, then the audio (mime type decides whether Universe posts it)
CallToolResult(content=[
    TextContent(type="text", text="Made a 15s track at 86 BPM in F major."),
    EmbeddedResource(type="resource", resource=BlobResourceContents(
        uri="audio://<file>.mp3", blob="<base64>", mimeType="audio/mpeg")),
])

# failure: one sentence, no internals
CallToolResult(content=[TextContent(type="text", text=_PHRASES["warming"])], isError=True)

# still working: not an error — carry the id the model needs to poll
CallToolResult(content=[TextContent(type="text", text="Still rendering — ask me about song <id> in a moment.")])
```

Hard rules encoded in `imagine_music_mcp.py`:

- **Nothing escapes as an exception.** The SDK wraps a raised exception as `Error executing tool <name>: ...`, which would eat the 80 characters Universe shows. Every tool body runs through `_run_guarded`, which maps failures to a fixed phrase.
- **Details go to the log, never into the result** — no URLs, ports, paths, or API bodies. `_log()` writes to `imagine_music.log`.
- **Brevity.** The bot owner pays per word and nothing is trimmed. One sentence, no JSON dump.
- **Media guard.** Only `.mp3`, `.wav`, `.ogg` under 10 MB are attached; anything else is not posted to chat. ACE Step emits 128 kbps mp3, so the cap never bites inside the 10–600 s duration range.
- **`structured_output=False`** on every tool. Without it FastMCP emits an `outputSchema` and validates `structuredContent`, which a bare `CallToolResult` does not carry.
- Annotation hints are set for other clients even though Universe only reads `annotations.title`. `get_generation` is `readOnlyHint` + `idempotentHint`.

## 4. What Universe ignores today

- Annotation hints (`readOnlyHint`, `destructiveHint`, `idempotentHint`, `openWorldHint`) — no confirm step.
- `icons`, `outputSchema`, `_meta`, content annotations (`audience`, `priority`).
- `structuredContent` — only `content` is passed, so anything structured must also appear as text.
- `resource_link`, and the text inside an embedded resource.
- Elicitation, sampling, tasks, `input_required` answers, list-changed notices.
- Log or progress messages on the stream **break the call**; never emit them before the result.
- PDF/ZIP files are never posted by themselves.

## 5. Size and delivery limits

- Nearby chat: 25 MB fetched per file, 10 MB kept by default. Two or more images become one gallery that loads straight from your links, so keep them up for days.
- Nearby chat, once a file is auto-posted: the model only sees "Auto-sent 1 media file" — the rest of that result's text is dropped. Facts the bot should say need a separate tool call.
- Direct messages: whole text passed, 50 MB per file, no redirects, every file sent (the first 5 until the next game release).

## Verification

`scripts/universe_probe.py` runs the checks against a live endpoint (stdlib only):

```bash
# 1. initialize + tools/list: titles, annotations, argument descriptions, no outputSchema
python scripts/universe_probe.py list --url https://music.bawes.net/mcp --key "$API_KEY"

# 2. one real call: content must be a short text line plus the media item
python scripts/universe_probe.py call generate_music \
  --url https://music.bawes.net/mcp --key "$API_KEY" \
  --args '{"prompt": "warm lo-fi beat, soft piano", "bpm": 86, "key": "F major"}'

# 3. one induced failure: isError true, <=80 characters, no URL
python scripts/universe_probe.py call get_generation \
  --url https://music.bawes.net/mcp --key "$API_KEY" \
  --args '{"task_id": "00000000-0000-0000-0000-000000000000"}'
```

A hard failure can also be induced by pointing a throwaway instance at a dead engine:

```bash
PORT=8218 ACESTEP_API_URL=http://127.0.0.1:9 ACE_STEP_DIR=/tmp/no-such-dir \
  python imagine_music_mcp.py     # then call generate_music against :8218
```

Expected: `isError: true`, one sentence, 80 characters or fewer, no `Error executing tool` prefix.

Finally, remember the refresh rule: the bot's cached tool list updates on respawn or after about an hour.
