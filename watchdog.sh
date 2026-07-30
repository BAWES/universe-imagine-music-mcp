#!/bin/bash
# Watchdog for imagine-music-mcp server.
# Checks every 2 minutes if the server is running, restarts if dead.
# Same pattern as Designer MCP watchdog.

MCP_DIR="$(cd "$(dirname "$0")" && pwd)"
MCP_LOG="$MCP_DIR/imagine_music.log"

cd "$MCP_DIR" || exit 1

if ! pgrep -f "imagine_music_mcp.py" > /dev/null 2>&1; then
    echo "[$(date)] Imagine Music MCP not running, starting..." >> "$MCP_LOG"
    nohup "$MCP_DIR/.venv/bin/python3" imagine_music_mcp.py >> "$MCP_LOG" 2>&1 &
    echo "[$(date)] Started PID $!" >> "$MCP_LOG"
fi
