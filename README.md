# Imagine Music MCP

FastMCP server wrapping the local ACE Step API for music generation. Same pattern as Designer MCP — Streamable HTTP transport, Cloudflare tunnel, X-API-Key auth.

## Architecture

```
bot (Hetzner)
  ↓  HTTP tools/call /mcp
  ↓  X-API-Key header
music.bawes.net (cloudflared tunnel)
  ↓
Imagine Music MCP (this server, port 8006)
  ↓  localhost:8001
ACE Step API (runs separately)
```

## Tools

| Tool | Description | Latency |
|------|-------------|---------|
| `generate_music` | Generate music from text prompt → audio URL | ~7-30s |
| `get_generation` | Check result by task ID | Instant |
| `list_generations` | List recent generations | Instant |

## Setup

1. Clone and enter the repo
2. Install dependencies:
   ```
   pip install -r requirements.txt
   ```
3. Edit `.env` — set `API_KEY` to your key
4. Ensure ACE Step API is running on `localhost:8001`
5. Start the server:
   ```
   python imagine_music_mcp.py
   ```
6. Test:
   ```
   curl -X POST http://localhost:8006/mcp \
     -H "Content-Type: application/json" \
     -H "Accept: application/json, text/event-stream" \
     -H "X-API-Key: your-key" \
     -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"test","version":"1.0"}}}'
   ```

## Deployment

Set up a cloudflared tunnel pointing to `localhost:8006` with hostname `music.bawes.net`. Register as a remote MCP server in the bot admin panel:

```json
{
  "name": "Imagine Music",
  "serverUrl": "https://music.bawes.net/mcp",
  "transport": "streamable-http",
  "authType": "api-key",
  "authConfig": "<your-api-key>"
}
```
