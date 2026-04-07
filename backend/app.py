import asyncio
import concurrent.futures
import dataclasses
import json
import os
import sys
import time
import uuid
from dataclasses import dataclass, field
from io import BytesIO
from typing import Optional

# ---------------------------------------------------------------------------
# Windows / Playwright fix
#
# Playwright needs asyncio.create_subprocess_exec, which requires a
# ProactorEventLoop on Windows.  Uvicorn creates its event loop before this
# module loads, so we can't rely on changing the global policy here.
#
# Solution: every Playwright coroutine runs inside a ThreadPoolExecutor thread
# where we explicitly spin up a fresh ProactorEventLoop.  Progress updates
# travel back to the uvicorn loop via call_soon_threadsafe (thread-safe).
# The global policy is set in run.py, before uvicorn.run() is called, as a
# belt-and-suspenders measure.
# ---------------------------------------------------------------------------

_playwright_executor = concurrent.futures.ThreadPoolExecutor(
    max_workers=1,   # one browser session at a time
    thread_name_prefix="playwright",
)


def _run_in_proactor(coro_fn, *args, **kwargs):
    """
    Synchronous helper executed inside a ThreadPoolExecutor thread.
    Creates a fresh ProactorEventLoop (Windows) or default loop (other OS),
    runs the given coroutine to completion, then closes the loop.
    """
    if sys.platform == "win32":
        loop = asyncio.ProactorEventLoop()
    else:
        loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        return loop.run_until_complete(coro_fn(*args, **kwargs))
    finally:
        loop.close()
        asyncio.set_event_loop(None)

import pandas as pd
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, model_validator

from scraper import sweep_search_page
from deep_dive import analyze_products
from scorer import score_products

from pathlib import Path

# Get the directory where app.py is located
BASE_DIR = Path(__file__).resolve().parent

# Define the static directory (one level up from app.py)
STATIC_DIR = BASE_DIR.parent / "static"

# ---------------------------------------------------------------------------
# App setup
# ---------------------------------------------------------------------------
app = FastAPI(title="AliBot")
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

AUTH_STATE_FILE = "aliexpress_state.json"

# ---------------------------------------------------------------------------
# Job state
# ---------------------------------------------------------------------------
@dataclass
class JobState:
    status: str = "pending"       # pending | running | complete | error
    phase: str = ""
    percent: int = 0
    messages: list = field(default_factory=list)
    results: list = field(default_factory=list)
    error: Optional[str] = None
    queue: asyncio.Queue = field(default_factory=lambda: asyncio.Queue(maxsize=500))


job_store: dict[str, JobState] = {}

# Auth subprocess handle (one at a time)
_auth_proc = None

# ---------------------------------------------------------------------------
# Request model
# ---------------------------------------------------------------------------
class SearchRequest(BaseModel):
    search_term: str = Field(..., min_length=1, max_length=200)
    negative_keywords: list[str] = ["case", "cable", "cover", "box", "empty"]
    min_sales: int = Field(10, ge=0)
    price_floor_pct: float = Field(30.0, ge=0, le=100)
    num_pages: int = Field(1, ge=1, le=10)
    w_rating: float = Field(20.0, ge=0, le=100)
    w_sales: float = Field(25.0, ge=0, le=100)
    w_reviews: float = Field(15.0, ge=0, le=100)
    w_price: float = Field(40.0, ge=0, le=100)
    include_seller_score: bool = False
    include_shipping_time: bool = False
    include_choice: bool = False

    @model_validator(mode="after")
    def weights_must_sum_100(self):
        total = self.w_rating + self.w_sales + self.w_reviews + self.w_price
        if abs(total - 100.0) > 0.5:
            raise ValueError(f"Scoring weights must sum to 100, got {total:.1f}")
        return self


# ---------------------------------------------------------------------------
# Pipeline background task
# ---------------------------------------------------------------------------
async def run_pipeline(job_id: str, req: SearchRequest):
    job = job_store[job_id]
    job.status = "running"

    # Capture the uvicorn event loop so the thread can send updates back safely.
    main_loop = asyncio.get_running_loop()

    def _push(item):
        """Thread-safe push into the SSE queue."""
        try:
            main_loop.call_soon_threadsafe(job.queue.put_nowait, item)
        except Exception:
            pass

    def progress_cb(percent: int, message: str, phase: str = ""):
        job.percent = percent
        if phase:
            job.phase = phase
        job.messages.append(message)
        _push({"percent": percent, "message": message, "phase": job.phase})

    try:
        progress_cb(0, "Pipeline started.", "Phase 1: Search Scraping")

        # Playwright runs in a dedicated thread with its own ProactorEventLoop.
        candidates = await main_loop.run_in_executor(
            _playwright_executor,
            lambda: _run_in_proactor(
                sweep_search_page,
                req.search_term,
                req.negative_keywords,
                req.min_sales,
                req.price_floor_pct,
                progress_cb,
                req.num_pages,
            ),
        )

        if not candidates:
            raise ValueError("No candidate products found after filtering. Try a different search term or looser filters.")

        progress_cb(35, f"Found {len(candidates)} candidates. Starting deep dive...", "Phase 2: Deep Dive")

        enriched = await main_loop.run_in_executor(
            _playwright_executor,
            lambda: _run_in_proactor(
                analyze_products,
                candidates,
                req.include_seller_score,
                req.include_shipping_time,
                req.include_choice,
                progress_cb,
            ),
        )

        progress_cb(80, "Scoring and ranking products...", "Phase 3: Scoring")
        scored = score_products(
            enriched,
            w_rating=req.w_rating / 100,
            w_sales=req.w_sales / 100,
            w_reviews=req.w_reviews / 100,
            w_price=req.w_price / 100,
        )

        job.results = scored
        job.status = "complete"
        progress_cb(100, f"Done! {len(scored)} products ranked.", "Complete")

    except Exception as e:
        job.status = "error"
        job.error = str(e)
        _push({"percent": job.percent, "message": f"ERROR: {e}", "phase": "Error"})
    finally:
        _push(None)  # sentinel — closes the SSE stream


# ---------------------------------------------------------------------------
# SSE helper
# ---------------------------------------------------------------------------
async def sse_generator(job_id: str):
    job = job_store.get(job_id)
    if not job:
        yield f"data: {json.dumps({'error': 'Job not found'})}\n\n"
        return

    # Replay historical messages so a reconnecting client catches up
    for msg in job.messages:
        yield f"data: {json.dumps({'percent': job.percent, 'message': msg, 'phase': job.phase})}\n\n"

    if job.status in ("complete", "error"):
        yield f"data: {json.dumps({'done': True, 'status': job.status, 'error': job.error})}\n\n"
        return

    # Stream live updates
    while True:
        try:
            item = await asyncio.wait_for(job.queue.get(), timeout=25.0)
        except asyncio.TimeoutError:
            yield f"data: {json.dumps({'ping': True})}\n\n"
            continue

        if item is None:
            yield f"data: {json.dumps({'done': True, 'status': job.status, 'error': job.error})}\n\n"
            return

        yield f"data: {json.dumps(item)}\n\n"


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------
@app.get("/")
async def index():
    index_path = STATIC_DIR / "index.html"
    if not index_path.exists():
        raise HTTPException(status_code=404, detail="Frontend file not found")
    return FileResponse(str(index_path))

@app.get("/auth/status")
async def auth_status():
    if not os.path.exists(AUTH_STATE_FILE):
        return {"exists": False, "age_hours": None}
    age_hours = (time.time() - os.path.getmtime(AUTH_STATE_FILE)) / 3600
    return {"exists": True, "age_hours": round(age_hours, 1)}


@app.post("/auth")
async def start_auth():
    """Spawn auth.py as a subprocess; the browser opens on the user's machine."""
    global _auth_proc
    if _auth_proc and _auth_proc.returncode is None:
        return JSONResponse({"status": "already_running"}, status_code=200)

    _auth_proc = await asyncio.create_subprocess_exec(
        sys.executable, "auth.py",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )

    async def stream():
        yield f"data: {json.dumps({'message': 'Playwright browser is opening...'})}\n\n"
        async for line in _auth_proc.stdout:
            text = line.decode(errors="replace").rstrip()
            if text:
                yield f"data: {json.dumps({'message': text})}\n\n"
        yield f"data: {json.dumps({'done': True})}\n\n"

    return StreamingResponse(stream(), media_type="text/event-stream")


@app.post("/auth/confirm")
async def confirm_auth():
    """Send ENTER to the auth subprocess to complete the session save."""
    global _auth_proc
    if not _auth_proc or _auth_proc.returncode is not None:
        raise HTTPException(status_code=400, detail="No active auth session.")
    try:
        _auth_proc.stdin.write(b"\n")
        await _auth_proc.stdin.drain()
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    return {"status": "ok"}


@app.post("/search")
async def start_search(req: SearchRequest):
    if not os.path.exists(AUTH_STATE_FILE):
        raise HTTPException(
            status_code=400,
            detail="No AliExpress session found. Please authenticate first.",
        )

    job_id = str(uuid.uuid4())
    job_store[job_id] = JobState()
    asyncio.create_task(run_pipeline(job_id, req))
    return {"job_id": job_id}


@app.get("/progress/{job_id}")
async def progress(job_id: str):
    if job_id not in job_store:
        raise HTTPException(status_code=404, detail="Job not found.")
    return StreamingResponse(sse_generator(job_id), media_type="text/event-stream")


@app.get("/results/{job_id}")
async def results(job_id: str):
    job = job_store.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found.")
    if job.status == "running":
        return JSONResponse({"status": "running"}, status_code=202)
    if job.status == "error":
        raise HTTPException(status_code=400, detail=job.error)
    return job.results


@app.get("/export/{job_id}")
async def export_excel(job_id: str):
    job = job_store.get(job_id)
    if not job or job.status != "complete":
        raise HTTPException(status_code=404, detail="Results not ready.")

    df = pd.DataFrame(job.results)

    # Choose which columns to include and in what order
    preferred = [
        "Title", "Final_Score", "Total_Cost_RWF", "Price", "Shipping_RWF",
        "Rating", "Sales", "Review_Count", "Seller_Score", "Shipping_Time",
        "Choice_Badge", "URL", "Image_URL",
    ]
    cols = [c for c in preferred if c in df.columns]
    df = df[cols]

    buf = BytesIO()
    df.to_excel(buf, index=False)
    buf.seek(0)

    return StreamingResponse(
        buf,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": "attachment; filename=alibot_results.xlsx"},
    )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app:app", host="127.0.0.1", port=8000, reload=True)
