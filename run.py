"""
AliBot startup script.

Run this instead of invoking uvicorn directly:

    python run.py

Why this file exists:
  Setting asyncio.WindowsProactorEventLoopPolicy inside app.py is too late —
  uvicorn may have already created a SelectorEventLoop by the time that module
  is imported.  We set the policy here, before uvicorn.run() is ever called,
  so the loop it creates is a ProactorEventLoop from the start.
"""

import asyncio
import os
import sys

# ── 1. Windows event loop policy (must happen before uvicorn.run) ────────────
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

# ── 2. Working directory = project root (where aliexpress_state.json lives) ──
ROOT = os.path.dirname(os.path.abspath(__file__))
os.chdir(ROOT)

# ── 3. Put backend/ on sys.path so "import app / scraper / ..." all resolve ──
BACKEND = os.path.join(ROOT, "backend")
if BACKEND not in sys.path:
    sys.path.insert(0, BACKEND)

# ── 4. Start the server ───────────────────────────────────────────────────────
import uvicorn

if __name__ == "__main__":
    # "app:app" resolves to backend/app.py (via sys.path above).
    # reload=False is intentional: --reload spawns a child process which
    # resets the event loop policy we just set.
    uvicorn.run("app:app", host="0.0.0.0", port=8000, reload=False)
