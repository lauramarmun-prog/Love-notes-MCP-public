import inspect
import os
import secrets
from datetime import datetime

import requests
from dotenv import load_dotenv
from fastmcp import FastMCP
from supabase import create_client

load_dotenv()

ELEVENLABS_API_KEY = os.getenv("ELEVENLABS_API_KEY")
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_SERVICE_ROLE = os.getenv("SUPABASE_SERVICE_ROLE")
SUPABASE_BUCKET = os.getenv("SUPABASE_BUCKET", "love-notes")

if not (ELEVENLABS_API_KEY and SUPABASE_URL and SUPABASE_SERVICE_ROLE):
    raise RuntimeError("Missing env vars. Check Render Environment variables.")

supabase = create_client(SUPABASE_URL, SUPABASE_SERVICE_ROLE)
mcp = FastMCP("Love Notes MCP Bridge")
MCP_PATH = "/sse/"


def generate_and_store_voice(
    text: str,
    voice_id: str,
    title: str | None,
    model_id: str | None = "eleven_v3",
):
    url = f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}"
    headers = {
        "xi-api-key": ELEVENLABS_API_KEY,
        "Content-Type": "application/json",
        "Accept": "audio/mpeg",
    }
    payload = {"text": text, "model_id": model_id}

    r = requests.post(url, json=payload, headers=headers, timeout=60)
    if r.status_code != 200:
        raise RuntimeError(f"ElevenLabs error: {r.status_code} - {r.text}")

    audio_bytes = r.content
    filename = f"voices/{int(datetime.utcnow().timestamp())}-{secrets.token_hex(6)}.mp3"

    try:
        supabase.storage.from_(SUPABASE_BUCKET).upload(
            path=filename,
            file=audio_bytes,
            file_options={"content-type": "audio/mpeg", "upsert": False},
        )
    except Exception as e:
        raise RuntimeError(f"Storage upload failed: {e}") from e

    audio_url = supabase.storage.from_(SUPABASE_BUCKET).get_public_url(filename)
    if not audio_url:
        raise RuntimeError("Could not get public URL.")

    try:
        supabase.table("voices").insert(
            {"title": title, "audio_url": audio_url, "storage_path": filename}
        ).execute()
    except Exception as e:
        raise RuntimeError(f"DB insert failed: {e}") from e

    return {"audio_url": audio_url, "storage_path": filename}


@mcp.tool()
def create_voice_note(
    text: str,
    voice_id: str,
    title: str | None = None,
    model_id: str | None = "eleven_v3",
) -> dict:
    """Generate audio with ElevenLabs and save into Love Notes (Supabase)."""
    return generate_and_store_voice(text=text, voice_id=voice_id, title=title, model_id=model_id)


@mcp.tool()
def search(query: str) -> dict:
    """
    Search stored voice notes by text and return lightweight result items.
    """
    q = (query or "").strip().lower()
    if not q:
        return {"results": []}

    rows = (
        supabase.table("voices")
        .select("id,title,audio_url,storage_path,created_at")
        .order("created_at", desc=True)
        .limit(100)
        .execute()
        .data
        or []
    )

    results = []
    for row in rows:
        title = str(row.get("title") or "")
        audio_url = str(row.get("audio_url") or "")
        storage_path = str(row.get("storage_path") or "")
        haystack = f"{title} {audio_url} {storage_path}".lower()
        if q in haystack:
            row_id = row.get("id")
            results.append(
                {
                    "id": str(row_id) if row_id is not None else storage_path,
                    "title": title or storage_path or "Voice note",
                    "text": audio_url,
                    "url": audio_url,
                }
            )
    return {"results": results}


@mcp.tool()
def fetch(id: str) -> dict:
    """
    Fetch one stored voice note by id for connector compatibility.
    """
    lookup = (id or "").strip()
    if not lookup:
        raise ValueError("id is required")

    rows = (
        supabase.table("voices")
        .select("*")
        .order("created_at", desc=True)
        .limit(200)
        .execute()
        .data
        or []
    )
    for row in rows:
        row_id = row.get("id")
        if (row_id is not None and str(row_id) == lookup) or str(row.get("storage_path") or "") == lookup:
            return {
                "id": str(row_id) if row_id is not None else str(row.get("storage_path") or ""),
                "title": str(row.get("title") or "Voice note"),
                "text": str(row.get("audio_url") or ""),
                "url": str(row.get("audio_url") or ""),
                "metadata": {
                    "storage_path": row.get("storage_path"),
                    "created_at": row.get("created_at"),
                },
            }
    raise ValueError(f"voice note not found: {lookup}")


def build_mcp_app(server):
    method = getattr(server, "http_app", None)
    if callable(method):
        try:
            sig = inspect.signature(method)
            kwargs = {}
            if "path" in sig.parameters:
                kwargs["path"] = MCP_PATH
            if "transport" in sig.parameters:
                kwargs["transport"] = "sse"
            return method(**kwargs)
        except TypeError:
            return method()

    for method_name in ("sse_app", "streamable_http_app", "asgi_app"):
        method = getattr(server, method_name, None)
        if not callable(method):
            continue
        try:
            sig = inspect.signature(method)
            if "path" in sig.parameters:
                return method(path=MCP_PATH)
            return method()
        except TypeError:
            return method()

    raise RuntimeError("Unsupported fastmcp version: no compatible app builder found.")


app = build_mcp_app(mcp)
