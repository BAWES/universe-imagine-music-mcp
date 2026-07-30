"""
Imagine Music MCP — FastMCP server wrapping ACE Step local API.
Same pattern as Designer MCP: calls local REST API, Streamable HTTP transport,
monkey-patches TransportSecurityMiddleware for Cloudflare tunnel compat.
"""

import json
import os
import sys
import time
import urllib.request
import urllib.error
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP, Context

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

PORT = int(os.getenv("PORT", "8006"))
HOST = os.getenv("HOST", "0.0.0.0")
ACESTEP_API_URL = os.getenv("ACESTEP_API_URL", "http://localhost:8001")
API_KEY = os.getenv("API_KEY", "")
PUBLIC_URL_BASE = os.getenv("PUBLIC_URL_BASE", "http://localhost:8006")

app = FastMCP("Imagine Music", port=PORT, host=HOST)

# ── Helpers ─────────────────────────────────────────────────────────────

def _api_post(path: str, body: dict, timeout: int = 120) -> dict:
    """POST to the local ACE Step API and return parsed JSON data."""
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


def _api_get(path: str, timeout: int = 30) -> dict:
    """GET from the local ACE Step API."""
    url = f"{ACESTEP_API_URL}{path}"
    req = urllib.request.Request(url, method="GET")
    try:
        resp = urllib.request.urlopen(req, timeout=timeout)
        result = json.loads(resp.read().decode())
        return result.get("data") or result
    except urllib.error.HTTPError as e:
        body_text = e.read().decode(errors="replace")
        raise RuntimeError(f"ACE API {e.code} on GET {path}: {body_text[:500]}")


def _audio_url(file_path: str) -> str:
    """Convert a local filesystem path to a public URL."""
    name = Path(file_path).name
    return f"{PUBLIC_URL_BASE}/audio/{name}"


def _format_generation(result: dict) -> dict:
    """Parse a generation result dict into a clean output."""
    raw = result.get("result", "[]")
    try:
        items = json.loads(raw) if isinstance(raw, str) else raw
    except (json.JSONDecodeError, TypeError):
        items = []
    if not items:
        return {"status": "pending", "task_id": result.get("task_id", "")}
    item = items[0]
    file_path = item.get("file", "").replace("/v1/audio?path=", "").strip('"')
    audio_url = _audio_url(file_path) if file_path else ""
    return {
        "status": "completed" if item.get("status") == 1 else "failed",
        "task_id": result.get("task_id", ""),
        "audio_url": audio_url,
        "duration_s": item.get("metas", {}).get("duration", 0),
        "bpm": item.get("metas", {}).get("bpm", "N/A"),
        "key": item.get("metas", {}).get("keyscale", "N/A"),
        "seed": item.get("seed_value", ""),
        "prompt": item.get("prompt", ""),
        "model": f"{item.get('dit_model', '')} + {item.get('lm_model', '')}",
        "generation_info": item.get("generation_info", ""),
    }


# ── Tools ───────────────────────────────────────────────────────────────

@app.tool()
def generate_music(
    ctx: Context,
    prompt: str,
    duration: int = 15,
    model: str = "acestep-v15-turbo",
    lyrics: str = "",
    inference_steps: int = 8,
    bpm: Optional[int] = None,
    key: str = "",
) -> str:
    """Generate music from a text description.

    Args:
        prompt: Text description of the music to generate (e.g. "a chill lo-fi beat
            with smooth piano and soft drums").
        duration: Length of the generated audio in seconds (10-600, default 15).
        model: Model to use. "acestep-v15-turbo" (fast, ~7s), "acestep-v15-xl-sft"
            (best quality, ~10-30s), or other installed models.
        lyrics: Optional lyrics text. If empty, generates instrumental.
        inference_steps: Quality vs speed (8 for turbo, 50 for XL).
        bpm: Optional tempo override (e.g. 86).
        key: Optional key (e.g. "F major").
    """
    body = {
        "prompt": prompt,
        "model": model,
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
            return json.dumps({"error": "No task ID returned from ACE API"})

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
                return json.dumps(
                    _format_generation(data[0]),
                    default=str,
                    indent=2,
                )
            time.sleep(2)

        return json.dumps({
            "status": "timeout",
            "task_id": task_id,
            "message": f"Generation did not complete within {max_wait}s. "
                       f"Check result with get_generation(task_id='{task_id}').",
        })
    except RuntimeError as e:
        return json.dumps({"error": str(e)})


@app.tool()
def get_generation(ctx: Context, task_id: str) -> str:
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
            return json.dumps({
                "status": "not_found",
                "task_id": task_id,
                "message": "No result found for this task ID",
            })
        return json.dumps(_format_generation(data[0]), default=str, indent=2)
    except RuntimeError as e:
        return json.dumps({"error": str(e)})


@app.tool()
def list_generations(ctx: Context, limit: int = 10) -> str:
    """List the most recent generations from the API.

    Args:
        limit: Maximum number of results to return (default 10, max 50).
    """
    try:
        stats = _api_get("/v1/stats")
        return json.dumps(stats, default=str, indent=2)
    except RuntimeError as e:
        return json.dumps({"error": str(e)})


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
    # Wrap the FastMCP ASGI app with auth middleware
    mcp_asgi = app.streamable_http_app()
    mcp_asgi.add_middleware(BearerAuthMiddleware)
    import uvicorn
    uvicorn.run(mcp_asgi, host=HOST, port=PORT, forwarded_allow_ips="*")
