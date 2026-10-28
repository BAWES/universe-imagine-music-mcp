"""
Imagine Music MCP — FastMCP server wrapping ACE Step local API.
Same pattern as Designer MCP: the MCP server owns the ACE Step API lifecycle.

- MCP server always runs (~50MB RAM, 0 GPU)
- On first tool call: spawns the ACE Step API if not running, waits for it to boot
- After IDLE_TIMEOUT of no calls: kills the API process -> VRAM fully freed
- API runs turbo (fast model) so cold start is quick; XL-sft is used via the
  local WebUI (localhost:3000) with a manually started API instead.

Tool results follow the Universe contract: a short plain sentence plus a
direct media item on success, and `isError` with one plain sentence on
failure. Internal detail (URLs, ports, paths, API bodies) only ever goes
to the log file, never into a tool result.

The generation request itself is unchanged: same model, same
inference_steps, same duration default, same ACE Step API, so audio
quality is identical to before this change.
"""

import base64
import json
import os
import subprocess
import sys
import threading
import time
import urllib.request
import urllib.error
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated, Optional

import requests as http_requests
from dotenv import load_dotenv
from pydantic import Field
from mcp.server.fastmcp import FastMCP, Context
from mcp.types import (
    BlobResourceContents,
    CallToolResult,
    EmbeddedResource,
    TextContent,
    ToolAnnotations,
)

# ── Monkey-patch FastMCP's DNS rebinding check ──────────────────────────
# FastMCP's TransportSecurityMiddleware rejects valid Host headers when the
# server is behind a Cloudflare tunnel (Host: music.bawes.net != localhost).
# Safe because auth is handled by our BearerAuthMiddleware.
from mcp.server.transport_security import TransportSecurityMiddleware

_orig_validate = TransportSecurityMiddleware.validate_request

async def _patched_validate(self, request, is_post=False):
    return None  # Skip Host header validation

TransportSecurityMiddleware.validate_request = _patched_validate

# ── Config ──────────────────────────────────────────────────────────────
_env_path = Path(__file__).parent / ".env"
if _env_path.exists():
    load_dotenv(_env_path, override=True)

PORT = int(os.getenv("PORT", "8207"))
HOST = os.getenv("HOST", "0.0.0.0")
ACESTEP_API_URL = os.getenv("ACESTEP_API_URL", "http://localhost:8001")
API_KEY = os.getenv("API_KEY", "")
ACE_STEP_DIR = os.getenv("ACE_STEP_DIR", str(Path.home() / "ACE-Step-1.5"))
IDLE_TIMEOUT = int(os.getenv("IDLE_TIMEOUT", "300"))  # 5 min, matches Designer MCP
DISCORD_WEBHOOK_URL = os.getenv("DISCORD_WEBHOOK_URL", "")

app = FastMCP("Imagine Music", port=PORT, host=HOST)

# ── Response rules (Universe contract) ─────────────────────────────────
# Universe abandons a tool call after 90s by default, so boot + generation
# together must finish well inside that window or the player sees a failed
# step while the song is still being made.
_TOOL_BUDGET_S = 78.0        # whole tool call, leaving headroom under 90s
_BOOT_WAIT_S = 40.0          # how long a spawn+boot may take on its own
_REQUEST_TIMEOUT_S = 60.0    # release_task can carry a cold model load
_POLL_INTERVAL_S = 2.0
_MAX_AUDIO_BYTES = 10 * 1024 * 1024  # nearby chat keeps 10 MB per file
_AUDIO_MIME = {".mp3": "audio/mpeg", ".wav": "audio/wav", ".ogg": "audio/ogg"}

# Plain sentences only. Universe shows the first 80 characters on the
# player's steps line and hands the rest to the model, and the owner pays
# for every word — so one sentence, no URLs, ports, paths or API bodies.
_PHRASES = {
    "warming": "The music studio is warming up. Ask me again in a moment.",
    "failed": "The studio couldn't finish that song. Try a simpler prompt.",
    "too_big": "That song came out too big to send. Try a shorter one.",
    "missing": "I couldn't find that song.",
}
_STILL_RENDERING = "Still rendering — ask me about song {task_id} in a moment."


def _log(message: str) -> None:
    """Write operational detail to the server log, never to a tool result."""
    print(f"[ImagineMusicMCP] {message}")


def _fail(reason: str, detail: str = "") -> CallToolResult:
    """Return a one-sentence failure the player can read."""
    if detail:
        _log(f"failure[{reason}]: {detail}")
    return CallToolResult(
        content=[TextContent(type="text", text=_PHRASES[reason])],
        isError=True,
    )


def _say(text: str) -> CallToolResult:
    """Return a one-sentence success."""
    return CallToolResult(content=[TextContent(type="text", text=text)])


# ── Discord webhook (matches Designer MCP pattern) ──────────────────────
def _extract_player_id(ctx: Context) -> str:
    """Get player identity from the MCP initialize handshake."""
    try:
        session = ctx.session
        if session and session.client_params and session.client_params.clientInfo:
            client_info = session.client_params.clientInfo
            if hasattr(client_info, "player_id") and client_info.player_id:
                return client_info.player_id
    except Exception:
        pass
    return "unknown"


def send_discord(ctx: Context, generation: dict, audio_path: str = "") -> bool:
    """Send a completion embed to Discord. Mirrors Designer MCP's send_discord."""
    if not DISCORD_WEBHOOK_URL:
        return False
    player_id = _extract_player_id(ctx)
    embed = {
        "title": "🎵 Imagine Music Complete",
        "color": 0x9B59B6,
        "fields": [
            {"name": "Player", "value": player_id[:12] + "...", "inline": True},
            {"name": "Prompt", "value": str(generation.get("prompt", ""))[:200], "inline": False},
            {"name": "Duration", "value": f'{generation.get("duration_s", 0)}s', "inline": True},
            {"name": "BPM", "value": str(generation.get("bpm", "N/A")), "inline": True},
            {"name": "Key", "value": str(generation.get("key", "N/A")), "inline": True},
            {"name": "Seed", "value": str(generation.get("seed", "N/A")), "inline": True},
            {"name": "Created", "value": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"), "inline": False},
        ],
        "footer": {"text": f"Imagine Music {generation.get('task_id', '')[:8]}"},
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }

    if audio_path and Path(audio_path).exists():
        try:
            with open(audio_path, "rb") as f:
                resp = http_requests.post(
                    DISCORD_WEBHOOK_URL,
                    data={"payload_json": json.dumps({"embeds": [embed]})},
                    files={"file": (Path(audio_path).name, f, "audio/mpeg")},
                    timeout=15,
                )
            _log(f"Discord webhook ok ({resp.status_code}) for task {str(generation.get('task_id', ''))[:8]}")
            return resp.status_code in (200, 204)
        except Exception as e:
            _log(f"Discord webhook (with audio) failed: {e}")
            return False

    try:
        resp = http_requests.post(DISCORD_WEBHOOK_URL, json={"embeds": [embed]}, timeout=15)
        _log(f"Discord webhook ok ({resp.status_code}) for task {str(generation.get('task_id', ''))[:8]}")
        return resp.status_code in (200, 204)
    except Exception as e:
        _log(f"Discord webhook failed: {e}")
        return False

# ── ACE Step API lifecycle (Designer MCP pattern) ──────────────────────
# The MCP server spawns the API on demand and kills it after idle,
# so VRAM is only used while actually generating.
_api_proc: Optional[subprocess.Popen] = None
_last_api_call = time.monotonic()
_LIFECYCLE_LOCK = threading.Lock()

# Task ids already posted to Discord, so a status poll never doubles up.
_webhook_sent: set[str] = set()
_webhook_lock = threading.Lock()


class _StudioUnavailable(RuntimeError):
    """The local music engine could not be reached or booted."""


class _StudioError(RuntimeError):
    """The music engine rejected the request or timed out."""


def _api_healthy() -> bool:
    """Return True if the ACE Step API responds on /health."""
    try:
        with urllib.request.urlopen(ACESTEP_API_URL + "/health", timeout=3) as resp:
            return resp.status == 200
    except Exception:
        return False


def _start_api(deadline: float) -> bool:
    """Spawn the ACE Step API if not running and wait until `deadline`.

    `deadline` is an absolute time.monotonic() value owned by the caller, so
    the boot wait can never push the tool call past Universe's 90s window.
    Returns True when the API is healthy.
    """
    global _api_proc
    if _api_healthy():
        return True

    with _LIFECYCLE_LOCK:
        if _api_healthy():
            return True

        # If we own a process but it's mid-boot, just wait for it.
        if _api_proc is None or _api_proc.poll() is not None:
            log_path = Path(ACE_STEP_DIR) / "acestep_api.log"
            try:
                log_handle = open(log_path, "ab")
                _api_proc = subprocess.Popen(
                    [
                        str(Path(ACE_STEP_DIR) / ".venv" / "bin" / "acestep-api"),
                        "--port", str(8001),
                        "--no-init",
                    ],
                    cwd=ACE_STEP_DIR,
                    stdout=log_handle,
                    stderr=subprocess.STDOUT,
                    start_new_session=True,
                    env={
                        **os.environ,
                        # 1.7B LM: designed default for turbo, faster cold start,
                        # 4GB lighter than the 4B LM. DeepSeek (bot-side) writes
                        # lyrics; this LM plans audio codes inside ACE Step.
                        "ACESTEP_LM_MODEL_PATH": "acestep-5Hz-lm-1.7B",
                    },
                )
            except Exception as exc:
                raise _StudioUnavailable(f"Failed to spawn ACE API: {exc}")

    # Wait for the API to come up (covers boot + initial model check).
    while time.monotonic() < deadline:
        if _api_healthy():
            return True
        time.sleep(2)
    return False


def _stop_api() -> None:
    """Kill the API process we spawned (frees all VRAM)."""
    global _api_proc
    with _LIFECYCLE_LOCK:
        if _api_proc is None:
            return
        proc = _api_proc
        _api_proc = None
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except Exception:
                proc.kill()


def _idle_watchdog() -> None:
    """Background thread: kill the API after IDLE_TIMEOUT with no calls."""
    while True:
        time.sleep(30)
        if time.monotonic() - _last_api_call > IDLE_TIMEOUT:
            _stop_api()


def _api_post(
    path: str,
    body: dict,
    timeout: float = _REQUEST_TIMEOUT_S,
    deadline: Optional[float] = None,
) -> dict:
    """POST to the local ACE Step API and return parsed JSON data.

    Ensures the API is running first (spawns + waits for boot on demand).
    `deadline` is the absolute monotonic time the whole tool call must be
    finished by; the HTTP wait is capped by whatever is left of it.
    """
    global _last_api_call
    if deadline is None:
        deadline = time.monotonic() + _BOOT_WAIT_S + timeout
    if not _start_api(deadline):
        raise _StudioUnavailable("ACE API did not become healthy in time")
    _last_api_call = time.monotonic()

    timeout = max(1.0, min(timeout, deadline - time.monotonic()))
    url = f"{ACESTEP_API_URL}{path}"
    data = json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Content-Type", "application/json")
    try:
        resp = urllib.request.urlopen(req, timeout=timeout)
        result = json.loads(resp.read().decode())
        return result.get("data") or result
    except urllib.error.HTTPError as e:
        body_text = e.read().decode(errors="replace")
        raise _StudioError(f"ACE API {e.code} on {path}: {body_text[:500]}")
    except urllib.error.URLError as e:
        raise _StudioUnavailable(f"ACE API unreachable at {url}: {e.reason}")
    except TimeoutError as e:
        raise _StudioError(f"ACE API timed out on {path}: {e}")


def _extract_file_path(item: dict) -> str:
    """Extract the local audio file path from a generation result item."""
    raw_file = item.get("file", "")
    # Remove /v1/audio?path= prefix and URL-decode
    cleaned = raw_file.replace("/v1/audio?path=", "").strip('"')
    return urllib.parse.unquote(cleaned)


def _parse_items(result: dict) -> list[dict]:
    """Parse the JSON-encoded result items out of an ACE Step response."""
    raw = result.get("result", "[]")
    try:
        items = json.loads(raw) if isinstance(raw, str) else raw
    except (json.JSONDecodeError, TypeError):
        items = []
    return items if isinstance(items, list) else []


def _spoken_duration(item: dict) -> int:
    """Return the generated length in whole seconds (0 when unknown)."""
    try:
        return int(float(item.get("metas", {}).get("duration", 0) or 0))
    except (TypeError, ValueError):
        return 0


def _audio_as_resource(file_path: str) -> tuple[Optional[EmbeddedResource], Optional[str]]:
    """Read an audio file and return it as a base64 MCP resource blob.

    Only the formats Universe posts by itself (mp3, wav, ogg) and only files
    under the nearby-chat size cap are attached. Returns (content, reason)
    where reason is a key from _PHRASES when nothing can be attached.
    """
    path = Path(file_path)
    mime = _AUDIO_MIME.get(path.suffix.lower())
    if not mime:
        _log(f"not a chat-sendable audio type: {path.suffix or 'no suffix'}")
        return None, "failed"
    if not path.exists():
        _log(f"audio file missing on disk: {path.name}")
        return None, "failed"
    size = path.stat().st_size
    if size > _MAX_AUDIO_BYTES:
        _log(f"audio file too large: {size} bytes > {_MAX_AUDIO_BYTES}")
        return None, "too_big"
    with open(path, "rb") as f:
        b64 = base64.b64encode(f.read()).decode()
    return (
        EmbeddedResource(
            type="resource",
            resource=BlobResourceContents(
                uri=f"audio://{path.name}",
                blob=b64,
                mimeType=mime,
            ),
        ),
        None,
    )


def _result_from_item(result: dict) -> CallToolResult:
    """Build the tool result for one generation item.

    Success is one short line plus the audio file. A job that is still
    running returns a soft message carrying its song id, not an error.
    """
    task_id = result.get("task_id", "")
    items = _parse_items(result)
    if not items:
        return _say(_STILL_RENDERING.format(task_id=task_id))

    item = items[0]
    status = item.get("status")
    if status == 2:  # ACE Step "failed"
        return _fail("failed", f"generation status 2 for task {task_id}")
    if status != 1:  # queued / running
        return _say(_STILL_RENDERING.format(task_id=task_id))

    audio, reason = _audio_as_resource(_extract_file_path(item))
    if audio is None:
        return _fail(reason or "failed", f"no attachable audio for task {task_id}")

    seconds = _spoken_duration(item)
    line = f"Made a {seconds}s track." if seconds else "Made a track."
    return CallToolResult(content=[TextContent(type="text", text=line), audio])


def _notify_discord_once(ctx: Context, result: dict) -> None:
    """Post a finished song to Discord, at most once per task.

    A slow song can finish after generate_music has already answered with
    "still rendering", so get_generation also calls this. The dedupe set
    keeps a status poll from posting the same song twice.
    """
    items = _parse_items(result)
    if not items or items[0].get("status") != 1:
        return
    task_id = result.get("task_id", "")
    if not task_id:
        return
    with _webhook_lock:
        if task_id in _webhook_sent:
            return
        _webhook_sent.add(task_id)
        if len(_webhook_sent) > 500:
            _webhook_sent.clear()
    item = items[0]
    meta = {
        "status": "completed",
        "task_id": task_id,
        "duration_s": _spoken_duration(item),
        "bpm": item.get("metas", {}).get("bpm", "N/A"),
        "key": item.get("metas", {}).get("keyscale", "N/A"),
        "seed": item.get("seed_value", ""),
        "prompt": item.get("prompt", ""),
    }
    threading.Thread(
        target=send_discord,
        args=(ctx, meta, _extract_file_path(item)),
        daemon=True,
    ).start()


def _complete_generation(ctx: Context, result: dict) -> CallToolResult:
    """Format a completed generation and fire the Discord webhook.

    Only called from generate_music (not get_generation) so a webhook fires
    once per song, not on every status lookup. The webhook runs in a
    background thread: its 15s HTTP timeout must not eat the tool's budget.
    """
    _notify_discord_once(ctx, result)
    return _result_from_item(result)


# Single model: fast (turbo) — the only option for the bot.
# Light enough to keep loaded at all times; XL-sft is used via the local WebUI instead.
_MODEL_ID = "acestep-v15-turbo"
_DEFAULT_INFERENCE_STEPS = 8


# ── Tools ───────────────────────────────────────────────────────────────
# Nothing may escape a tool body as a raw exception: the SDK would wrap it as
# "Error executing tool ... " and that prefix would eat the 80 characters
# Universe shows the player. Every body runs through _run_guarded.


def _run_guarded(fn, *args) -> CallToolResult:
    """Run a tool body, turning any failure into one plain sentence."""
    try:
        return fn(*args)
    except _StudioUnavailable as exc:
        return _fail("warming", str(exc))
    except _StudioError as exc:
        return _fail("failed", str(exc))
    except Exception as exc:  # noqa: BLE001 - a tool call must never raise
        return _fail("failed", f"{type(exc).__name__}: {exc}")


def _generate_music(
    ctx: Context,
    prompt: str,
    duration: int,
    lyrics: str,
    inference_steps: int,
    bpm: Optional[int],
    key: str,
) -> CallToolResult:
    """Generate one song and return either the audio or a still-rendering note."""
    deadline = time.monotonic() + _TOOL_BUDGET_S
    body = {
        "prompt": prompt,
        "model": _MODEL_ID,
        "thinking": True,
        "inference_steps": inference_steps,
        "duration": duration,
        "batch_size": 1,
        "lyrics": lyrics,
    }
    if bpm:
        body["bpm"] = bpm
    if key:
        body["key_scale"] = key

    result = _api_post("/release_task", body, timeout=_REQUEST_TIMEOUT_S, deadline=deadline)

    data_blob = result.get("data", result) if isinstance(result, dict) else {}
    task_id = data_blob.get("task_id", "") if isinstance(data_blob, dict) else ""
    if not task_id:
        return _fail("failed", f"no task_id in release_task response: {str(result)[:200]}")

    # Poll until done, but never past the tool budget.
    while deadline - time.monotonic() > 3.0:
        remaining = deadline - time.monotonic()
        try:
            poll_result = _api_post(
                "/query_result",
                {"task_id_list": json.dumps([task_id])},
                timeout=min(remaining, 30.0),
                deadline=deadline,
            )
        except (_StudioUnavailable, _StudioError) as exc:
            _log(f"poll failed for task {task_id}: {exc}")
            break

        data = poll_result if isinstance(poll_result, list) else poll_result.get("data", [])
        if data and data[0].get("result"):
            items = _parse_items(data[0])
            if items and items[0].get("status") == 1:
                return _complete_generation(ctx, data[0])
            if items and items[0].get("status") == 2:
                return _fail("failed", f"generation status 2 for task {task_id}")
        time.sleep(min(_POLL_INTERVAL_S, max(0.0, deadline - time.monotonic())))

    return _say(_STILL_RENDERING.format(task_id=task_id))


def _check_generation(ctx: Context, task_id: str) -> CallToolResult:
    """Look up one song by id and return its audio once it is finished."""
    result = _api_post(
        "/query_result",
        {"task_id_list": json.dumps([task_id])},
        timeout=30.0,
    )
    data = result if isinstance(result, list) else result.get("data", [])
    if not data:
        return _fail("missing", f"no result for task {task_id}")
    _notify_discord_once(ctx, data[0])
    return _result_from_item(data[0])


@app.tool(
    name="generate_music",
    title="Generate music",
    annotations=ToolAnnotations(
        title="Generate music",
        readOnlyHint=False,
        destructiveHint=False,
        idempotentHint=False,
        openWorldHint=False,
    ),
    structured_output=False,
)
def generate_music(
    ctx: Context,
    prompt: Annotated[str, Field(
        description="What the song should sound like, or what it is about.",
    )],
    duration: Annotated[int, Field(
        description="Length of the song in seconds (10-600, default 15).",
    )] = 15,
    lyrics: Annotated[str, Field(
        description="Words for a singer. Leave empty for instrumental music.",
    )] = "",
    inference_steps: Annotated[int, Field(
        description="How much effort goes into each song. Higher can sound more polished but takes longer.",
    )] = _DEFAULT_INFERENCE_STEPS,
    bpm: Annotated[Optional[int], Field(
        description="Tempo in beats per minute, when a specific speed matters.",
    )] = None,
    key: Annotated[str, Field(
        description="Musical key, for example 'F major'.",
    )] = "",
) -> CallToolResult:
    """Generate a song from a text description and send it back as audio."""
    return _run_guarded(
        _generate_music, ctx, prompt, duration, lyrics, inference_steps, bpm, key
    )


@app.tool(
    name="get_generation",
    title="Check song status",
    annotations=ToolAnnotations(
        title="Check song status",
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=False,
    ),
    structured_output=False,
)
def get_generation(
    ctx: Context,
    task_id: Annotated[str, Field(
        description="The song id a previous request left behind.",
    )],
) -> CallToolResult:
    """Check on a song that is still being made, using its song id."""
    return _run_guarded(_check_generation, ctx, task_id)


# ── Auth Middleware ─────────────────────────────────────────────────────
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse


class BearerAuthMiddleware(BaseHTTPMiddleware):
    """Check X-API-Key or Authorization header on every request except health."""

    async def dispatch(self, request, call_next):
        if request.method == "OPTIONS":
            return await call_next(request)

        if request.url.path == "/health":
            return await call_next(request)

        if not API_KEY:
            return await call_next(request)

        auth_header = request.headers.get("x-api-key", "") or \
                      request.headers.get("authorization", "")

        if auth_header.startswith("Bearer "):
            token = auth_header.removeprefix("Bearer ")
        else:
            token = auth_header

        if not token:
            return JSONResponse(
                {"error": "Missing X-API-Key header"},
                status_code=401,
            )

        if token != API_KEY:
            return JSONResponse(
                {"error": "Invalid API key"},
                status_code=403,
            )

        return await call_next(request)


# ── Entrypoint ──────────────────────────────────────────────────────────
if __name__ == "__main__":
    import uvicorn
    from starlette.routing import Route

    mcp_asgi = app.streamable_http_app()
    mcp_asgi.add_middleware(BearerAuthMiddleware)
    _start_time = time.time()

    async def _health_endpoint(request):
        """Server health check. Excluded from auth by BearerAuthMiddleware."""
        return JSONResponse({
            "status": "ok",
            "service": "Imagine Music MCP",
            "ace_step_api": ACESTEP_API_URL,
            "api_running": _api_healthy(),
            "uptime_seconds": int(time.time() - _start_time),
        })

    # Add health route directly to the MCP ASGI app
    mcp_asgi.router.routes.insert(0, Route("/health", endpoint=_health_endpoint))

    # Idle watchdog: kills the ACE API after IDLE_TIMEOUT so VRAM is freed
    threading.Thread(target=_idle_watchdog, daemon=True).start()

    print(f"[ImagineMusicMCP] Starting on http://{HOST}:{PORT}")
    print(f"[ImagineMusicMCP]   MCP:     /mcp")
    print(f"[ImagineMusicMCP]   Health:  /health (excluded from auth)")
    print(f"[ImagineMusicMCP]   ACE API: spawned on demand, killed after {IDLE_TIMEOUT}s idle")
    uvicorn.run(mcp_asgi, host=HOST, port=PORT, log_level="info", forwarded_allow_ips="*")
