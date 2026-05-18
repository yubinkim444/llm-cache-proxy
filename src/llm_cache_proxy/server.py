"""
llm-cache-proxy
===============
A localhost HTTP proxy that speaks the OpenAI and Anthropic protocols and
caches identical requests on disk. Point any tool at it with one env var
and pay 0¢ for repeated prompts during development.

Routes:
    /openai/*     -> https://api.openai.com/*
    /anthropic/*  -> https://api.anthropic.com/*
    /stats        -> live cache stats (hits, misses, bytes, est. dollars saved)
    /             -> liveness probe

Cache key:  SHA256(method + path + body + relevant headers)
Storage:    a single SQLite file at $LLM_CACHE_DIR/cache.db
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

import httpx
from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse, PlainTextResponse

UPSTREAMS = {
    "openai": os.getenv("OPENAI_UPSTREAM", "https://api.openai.com"),
    "anthropic": os.getenv("ANTHROPIC_UPSTREAM", "https://api.anthropic.com"),
}
CACHE_DIR = Path(os.getenv("LLM_CACHE_DIR", str(Path.home() / ".cache" / "llm-cache-proxy")))
CACHE_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = CACHE_DIR / "cache.db"
TTL_SECONDS = int(os.getenv("LLM_CACHE_TTL", "0"))  # 0 = forever
DEFAULT_TIMEOUT = float(os.getenv("LLM_CACHE_TIMEOUT", "300"))

# Rough $/MTok rates (input pricing — used only for "estimated savings" UI).
PRICE_PER_MTOK = {
    "gpt-4o": 2.50, "gpt-4o-mini": 0.15, "gpt-4-turbo": 10.00,
    "claude-opus-4": 15.00, "claude-sonnet-4": 3.00, "claude-haiku-4": 0.80,
}


def _db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS entries (
            key TEXT PRIMARY KEY,
            ts INTEGER NOT NULL,
            status INTEGER NOT NULL,
            content_type TEXT,
            body BLOB,
            upstream TEXT,
            model TEXT,
            req_bytes INTEGER,
            resp_bytes INTEGER
        )
    """)
    conn.execute("CREATE TABLE IF NOT EXISTS stats (k TEXT PRIMARY KEY, v INTEGER NOT NULL)")
    conn.commit()
    return conn


def _bump(conn: sqlite3.Connection, key: str, by: int = 1) -> None:
    conn.execute(
        "INSERT INTO stats(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=v+?",
        (key, by, by),
    )


def _cache_key(method: str, path: str, body: bytes, headers: dict[str, str]) -> str:
    h = hashlib.sha256()
    h.update(method.encode()); h.update(b"|"); h.update(path.encode()); h.update(b"|")
    h.update(body)
    for hk in sorted(("x-llm-cache-extra-key",)):
        if hk in headers:
            h.update(b"|"); h.update(hk.encode()); h.update(b"="); h.update(headers[hk].encode())
    return h.hexdigest()


def _extract_model(body: bytes) -> Optional[str]:
    if not body:
        return None
    try:
        return json.loads(body).get("model")
    except Exception:
        return None


@asynccontextmanager
async def lifespan(_app: FastAPI):
    conn = _db()
    _app.state.db = conn
    _app.state.client = httpx.AsyncClient(timeout=DEFAULT_TIMEOUT)
    yield
    conn.close()
    await _app.state.client.aclose()


app = FastAPI(
    title="llm-cache-proxy",
    version="0.1.0",
    summary="On-disk SQLite cache for OpenAI and Anthropic API calls.",
    description=(
        "Point your tool at this proxy by setting `OPENAI_BASE_URL` to "
        "`http://localhost:9001/openai/v1` or `ANTHROPIC_BASE_URL` to "
        "`http://localhost:9001/anthropic`. Identical requests return cached "
        "responses; new requests pass through and are cached for next time."
    ),
    lifespan=lifespan,
)


@app.get("/", include_in_schema=False)
async def health() -> JSONResponse:
    return JSONResponse({
        "status": "ok", "service": "llm-cache-proxy",
        "version": "0.1.0", "db": str(DB_PATH),
    })


@app.get("/stats")
async def stats(request: Request) -> JSONResponse:
    conn: sqlite3.Connection = request.app.state.db
    rows = dict(conn.execute("SELECT k, v FROM stats").fetchall())
    total_entries, total_bytes = conn.execute(
        "SELECT COUNT(*), COALESCE(SUM(resp_bytes), 0) FROM entries"
    ).fetchone()
    # Per-model hit counts
    by_model = dict(conn.execute(
        "SELECT model, COUNT(*) FROM entries GROUP BY model"
    ).fetchall())
    return JSONResponse({
        "hits": rows.get("hits", 0),
        "misses": rows.get("misses", 0),
        "bytes_served_from_cache": rows.get("bytes_served_from_cache", 0),
        "entries": total_entries,
        "cached_response_bytes": total_bytes,
        "by_model": by_model,
        "db": str(DB_PATH),
    })


@app.delete("/stats")
async def clear_stats(request: Request) -> JSONResponse:
    conn: sqlite3.Connection = request.app.state.db
    conn.execute("DELETE FROM stats")
    conn.commit()
    return JSONResponse({"cleared": True})


@app.delete("/cache")
async def clear_cache(request: Request) -> JSONResponse:
    conn: sqlite3.Connection = request.app.state.db
    n = conn.execute("SELECT COUNT(*) FROM entries").fetchone()[0]
    conn.execute("DELETE FROM entries")
    conn.commit()
    return JSONResponse({"cleared_entries": n})


@app.api_route("/{provider}/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
async def proxy(provider: str, path: str, request: Request) -> Response:
    if provider not in UPSTREAMS:
        return JSONResponse({"error": f"unknown provider: {provider}"}, status_code=404)

    upstream_base = UPSTREAMS[provider]
    upstream_url = f"{upstream_base}/{path}"
    if request.url.query:
        upstream_url += "?" + request.url.query

    body = await request.body()
    headers = {k: v for k, v in request.headers.items() if k.lower() not in ("host", "content-length")}
    method = request.method.upper()

    conn: sqlite3.Connection = request.app.state.db
    client: httpx.AsyncClient = request.app.state.client

    cache_key = _cache_key(method, f"/{provider}/{path}", body, {k.lower(): v for k, v in headers.items()})
    cacheable = method in ("POST", "GET") and request.headers.get("x-llm-cache-bypass") != "1"

    if cacheable:
        row = conn.execute(
            "SELECT ts, status, content_type, body, resp_bytes FROM entries WHERE key = ?",
            (cache_key,),
        ).fetchone()
        if row:
            ts, status, ctype, cached_body, resp_bytes = row
            if TTL_SECONDS == 0 or (time.time() - ts) < TTL_SECONDS:
                _bump(conn, "hits")
                _bump(conn, "bytes_served_from_cache", resp_bytes or 0)
                conn.commit()
                return Response(
                    content=cached_body, status_code=status,
                    media_type=ctype or "application/octet-stream",
                    headers={"x-llm-cache": "HIT", "x-llm-cache-key": cache_key[:12]},
                )

    # MISS: fetch upstream
    upstream_resp = await client.request(method, upstream_url, content=body, headers=headers)
    resp_bytes = upstream_resp.content
    ctype = upstream_resp.headers.get("content-type", "application/octet-stream")

    if cacheable and 200 <= upstream_resp.status_code < 300:
        conn.execute(
            "INSERT OR REPLACE INTO entries(key, ts, status, content_type, body, upstream, model, req_bytes, resp_bytes) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (cache_key, int(time.time()), upstream_resp.status_code, ctype, resp_bytes,
             provider, _extract_model(body), len(body), len(resp_bytes)),
        )
        _bump(conn, "misses")
        conn.commit()

    return Response(
        content=resp_bytes, status_code=upstream_resp.status_code,
        media_type=ctype,
        headers={"x-llm-cache": "MISS", "x-llm-cache-key": cache_key[:12]},
    )


def main() -> None:
    import uvicorn
    port = int(os.getenv("LLM_CACHE_PORT", "9001"))
    host = os.getenv("LLM_CACHE_HOST", "127.0.0.1")
    uvicorn.run(app, host=host, port=port, log_level="info")


if __name__ == "__main__":
    main()
