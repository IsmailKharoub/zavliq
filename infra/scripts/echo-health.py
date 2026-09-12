#!/usr/bin/env python3
"""Content-free healthcheck; never print runtime identity or journal data."""
import json
import os
from pathlib import Path
import sys
import time

try:
    health = json.loads(Path("/echo/journal/health.json").read_text())
    ready = (
        health.get("status") == "ready"
        and health.get("user_id") == os.environ["ZAVLIQ_ECHO_USER_ID"]
        and 0 <= time.time() - float(health["updated_at"]) <= 90
    )
except (OSError, ValueError, KeyError, TypeError):
    ready = False
sys.exit(0 if ready else 1)
