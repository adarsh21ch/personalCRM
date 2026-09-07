# -*- coding: utf-8 -*-
"""
Vercel's Python runtime looks for an ASGI app named `app` under /api. This
file only re-exports the real one from app/main.py - no logic lives here,
so Render/Docker and Vercel run the exact same application.
"""
from app.main import app  # noqa: F401
