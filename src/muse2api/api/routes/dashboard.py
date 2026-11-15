"""Admin dashboard: a single self-contained HTML page, served at ``/``.

The page itself is public; it asks for the admin key in the browser and calls
``/admin/*`` with it, so serving the HTML reveals nothing. ``/dashboard`` is
kept as a redirect so old bookmarks keep working.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import HTMLResponse, RedirectResponse

router = APIRouter(tags=["dashboard"])

_PAGE = Path(__file__).resolve().parents[2] / "web" / "dashboard.html"


@lru_cache(maxsize=1)
def _html() -> str:
    return _PAGE.read_text(encoding="utf-8")


@router.get("/", response_class=HTMLResponse, include_in_schema=False)
async def dashboard() -> HTMLResponse:
    return HTMLResponse(_html(), headers={"Cache-Control": "no-cache",
                                          "X-Frame-Options": "DENY",
                                          "Referrer-Policy": "no-referrer"})


@router.get("/dashboard", include_in_schema=False)
async def dashboard_alias() -> RedirectResponse:
    return RedirectResponse("/", status_code=307)
