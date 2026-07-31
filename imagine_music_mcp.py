"""
Imagine Music MCP — FastMCP server wrapping ACE Step local API.
Same pattern as Designer MCP: the MCP server owns the ACE Step API lifecycle.

- MCP server always runs (~50MB RAM, 0 GPU)
- On first tool call: spawns the ACE Step API if not running, waits for it to boot
- After IDLE_TIMEOUT of no calls: kills the API process -> VRAM fully freed
- API runs turbo (fast model) so cold start is quick; XL-sft is used via the
  local WebUI (localhost:3000) with a manually started API instead.
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
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP, Context
from mcp.types import TextContent, EmbeddedResource
from mcp.types import BlobResourceContents

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

app = FastMCP("Imagine Music", port=PORT, host=HOST)

# ── ACE Step API lifecycle (Designer MCP pattern) ──────────────────────
# The MCP server spawns the API on demand and kills it after idle,
# so VRAM is only used while actually generating.
_api_proc: Optional[subprocess.Popen] = None
_last_api_call = time.monotonic()
_LIFECYCLE_LOCK = threading.Lock()


def _api_healthy() -> bool:
    """Return True if the ACE Step API responds on /health."""
    try:
        with urllib.request.urlopen(ACESTEP_API_URL + "/health", timeout=3) as resp:
            return resp.status == 200
    except Exception:
        return False


def _start_api() -> bool:
    """Spawn the ACE Step API if not running. Waits up to 60s for boot.

    Returns True when the API is healthy. Does NOT return early errors to
    callers — it blocks until the API is ready or the boot window expires,
    so the bot never sees a "service down" message during startup.
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
            log_handle = open(log_path, "ab")
            try:
                _api_proc = subprocess.Popen(
                    [
                        str(Path(ACE_STEP_DIR) / ".venv" / "bin" / "acestep-api"),
                        "--port", str(8001),
                    ],
                    cwd=ACE_STEP_DIR,
                    stdout=log_handle,
                    stderr=subprocess.STDOUT,
                    start_new_session=True,
                )
            except Exception as exc:
                raise RuntimeError(f"Failed to spawn ACE API: {exc}")

    # Wait for the API to come up (covers boot + initial model check).
    deadline = time.monotonic() + 60
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


def _api_post(path: str, body: dict, timeout: int = 120) -> dict:
    """POST to the local ACE Step API and return parsed JSON data.

    Ensures the API is running first (spawns + waits for boot on demand).
    """
    global _last_api_call
    if not _start_api():
        raise RuntimeError("ACE API failed to start within 60s")
    _last_api_call = time.monotonic()

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
        raise RuntimeError(f"ACE API {e.code} on {path}: {body_text[:500]}")
    except urllib.error.URLError as e:
        raise RuntimeError(f"ACE API unreachable at {url}: {e.reason}")


def _extract_file_path(item: dict) -> str:
    """Extract the local audio file path from a generation result item."""
    raw_file = item.get("file", "")
    # Remove /v1/audio?path= prefix and URL-decode
    cleaned = raw_file.replace("/v1/audio?path=", "").strip('"')
    return urllib.parse.unquote(cleaned)


def _audio_as_resource(file_path: str) -> EmbeddedResource:
    """Read an audio file and return it as a base64 MCP resource blob.

    Same pattern as ElevenLabs MCP — bot-server's PR #348 fix handles
    uploading this to CDN automatically.
    """
    path = Path(file_path)
    if not path.exists():
        return TextContent(type="text", text=f"Audio file not found: {file_path}")
    with open(path, "rb") as f:
        b64 = base64.b64encode(f.read()).decode()
    return EmbeddedResource(
        type="resource",
        resource=BlobResourceContents(
            uri=f"audio://{Path(file_path).name}",
            blob=b64,
            mimeType="audio/mpeg",
        ),
    )


def _format_generation(result: dict) -> list[TextContent | EmbeddedResource]:
    """Parse a generation result and return MCP content items.

    Returns audio as base64 resource blob + text metadata.
    """
    raw = result.get("result", "[]")
    try:
        items = json.loads(raw) if isinstance(raw, str) else raw
    except (json.JSONDecodeError, TypeError):
        items = []
    if not items:
        return [TextContent(type="text", text=json.dumps({
            "status": "pending", "task_id": result.get("task_id", ""),
        }))]

    item = items[0]
    file_path = _extract_file_path(item)
    status = "completed" if item.get("status") == 1 else "failed"

    meta = {
        "status": status,
        "task_id": result.get("task_id", ""),
        "duration_s": item.get("metas", {}).get("duration", 0),
        "bpm": item.get("metas", {}).get("bpm", "N/A"),
        "key": item.get("metas", {}).get("keyscale", "N/A"),
        "seed": item.get("seed_value", ""),
        "prompt": item.get("prompt", ""),
        "model": f"{item.get('dit_model', '')} + {item.get('lm_model', '')}",
        "generation_info": item.get("generation_info", ""),
    }

    content: list[TextContent | EmbeddedResource] = []
    if file_path and status == "completed":
        content.append(_audio_as_resource(file_path))
    content.append(TextContent(type="text", text=json.dumps(meta, indent=2)))
    return content


# Single model: fast (turbo) — the only option for the bot.
# Light enough to keep loaded at all times; XL-sft is used via the local WebUI instead.
_MODEL_ID = "acestep-v15-turbo"
_DEFAULT_INFERENCE_STEPS = 8


# ── Tools ───────────────────────────────────────────────────────────────

_GEN_DOC = f"""Generate music from a text description.

    Args:
        prompt: Text description of the music to generate (e.g. "a chill lo-fi beat
            with smooth piano and soft drums").
        duration: Length of the generated audio in seconds (10-600, default 15).
        inference_steps: Generation quality — higher = better but slower (default 8).
        lyrics: Optional lyrics text. If empty, generates instrumental.
        bpm: Optional tempo override (e.g. 86).
        key: Optional key (e.g. "F major").
    """


def _generate_music_fn(
    ctx: Context,
    prompt: str,
    duration: int = 15,
    lyrics: str = "",
    inference_steps: int = _DEFAULT_INFERENCE_STEPS,
    bpm: Optional[int] = None,
    key: str = "",
) -> list[TextContent | EmbeddedResource]:
    """Generate music from a text description."""
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

    try:
        result = _api_post("/release_task", body, timeout=120)
        data_blob = result.get("data", result)
        task_id = data_blob.get("task_id", "")
        if not task_id:
            return [TextContent(type="text", text=json.dumps({"error": "No task ID returned from ACE API"}))]

        # Poll until done (sync with timeout)
        start = time.time()
        max_wait = 120
        while time.time() - start < max_wait:
            poll_result = _api_post("/query_result", {
                "task_id_list": json.dumps([task_id]),
            })
            data = poll_result if isinstance(poll_result, list) else \
                   poll_result.get("data", [])

            if data and data[0].get("result"):
                # Only return when the inner result has status=1 (completed)
                raw = data[0].get("result", "[]")
                try:
                    inner = json.loads(raw) if isinstance(raw, str) else raw
                except (json.JSONDecodeError, TypeError):
                    inner = []
                if inner and inner[0].get("status") == 1:
                    return _format_generation(data[0])
            time.sleep(2)

        return [TextContent(type="text", text=json.dumps({
            "status": "timeout",
            "task_id": task_id,
            "message": f"Generation did not complete within {max_wait}s. "
                       f"Check result with get_generation(task_id='{task_id}').",
        }))]
    except RuntimeError as e:
        return [TextContent(type="text", text=json.dumps({"error": str(e)}))]


# Register generate_music with dynamic docstring
_generate_music_fn.__doc__ = _GEN_DOC
generate_music = app.tool(name="generate_music")(_generate_music_fn)


@app.tool()
def get_generation(ctx: Context, task_id: str) -> list[TextContent | EmbeddedResource]:
    """Check the status and result of a previous generation by task ID.

    Args:
        task_id: The task ID returned from generate_music.
    """
    try:
        result = _api_post("/query_result", {
            "task_id_list": json.dumps([task_id]),
        })
        data = result if isinstance(result, list) else result.get("data", [])
        if not data:
            return [TextContent(type="text", text=json.dumps({
                "status": "not_found",
                "task_id": task_id,
                "message": "No result found for this task ID",
            }))]
        return _format_generation(data[0])
    except RuntimeError as e:
        return [TextContent(type="text", text=json.dumps({"error": str(e)}))]


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
