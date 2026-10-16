"""FastAPI entry point: health checks, the UI's API, and (if built) the static UI."""
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from app.api import error_response, router
from app.config import get_settings
from app.constants import DISCLAIMER  # noqa: F401  (re-exported)
from app.db import check_falkordb
from app.lifecycle import shutdown_runtime
from app.mcp_support import McpToolError


@asynccontextmanager
async def lifespan(_app: FastAPI):
    yield
    shutdown_runtime()                     # stop the MCP server processes


app = FastAPI(title="The Forgetting Agent", version="0.5.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=get_settings().cors_origins,
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)


@app.exception_handler(McpToolError)
async def _tool_error(_req: Request, exc: McpToolError) -> JSONResponse:
    return error_response(exc)


@app.get("/health")
def health() -> dict:
    """Process liveness only. Does not touch the database."""
    return {"status": "ok", "service": "the-forgetting-agent", "disclaimer": DISCLAIMER}


@app.get("/health/graph")
def health_graph() -> JSONResponse:
    """Reports whether FalkorDB is reachable, using a real connection attempt."""
    result = check_falkordb()
    return JSONResponse(status_code=200 if result["reachable"] else 503, content=result)


app.include_router(router)

_DIST = Path(__file__).resolve().parents[2] / "frontend" / "dist"
if _DIST.is_dir():                         # `npm run build` makes the UI available from this same server
    app.mount("/", StaticFiles(directory=_DIST, html=True), name="ui")
