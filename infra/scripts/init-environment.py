#!/usr/bin/env python3
"""Create private configuration. Never print credentials; never overwrite state."""
import argparse
import os
from pathlib import Path
import re
import secrets
from urllib.parse import urlsplit

parser = argparse.ArgumentParser()
parser.add_argument("--directory", default="infra/.local")
parser.add_argument("--server-name", default="localhost")
parser.add_argument("--public-url", default="http://localhost:8080")
parser.add_argument("--production", action="store_true")
args = parser.parse_args()
url = urlsplit(args.public_url)
if not re.fullmatch(r"[a-zA-Z0-9.-]+", args.server_name):
    parser.error("server-name must be a DNS name without a port")
if url.scheme not in ("http", "https") or not url.hostname or url.path not in ("", "/") or url.username or url.query or url.fragment:
    parser.error("public-url must be an HTTP(S) origin")
if args.production and (url.scheme != "https" or args.server_name in ("localhost", "127.0.0.1")):
    parser.error("production requires a public DNS server-name and HTTPS origin")
directory = Path(args.directory).resolve()
os.umask(0o077)
directory.mkdir(parents=True, exist_ok=True, mode=0o700)
env_path = directory / "compose.env"
if env_path.exists():
    parser.error("environment already exists; reuse it instead of regenerating identity secrets")
for name in ("postgres_password", "registration_secret", "policy_secret"):
    (directory / name).write_text(secrets.token_urlsafe(48) + "\n")
(directory / "control_secret").write_text(secrets.token_hex(32) + "\n")
mode = "production" if args.production else "local"
values = {
    "COMPOSE_PROJECT_NAME": f"zavliq-{mode}",
    "ZAVLIQ_STATE_DIR": str(directory),
    "ZAVLIQ_SERVER_NAME": args.server_name,
    "ZAVLIQ_PUBLIC_URL": args.public_url.rstrip("/"),
    "ZAVLIQ_SITE_ADDRESS": args.server_name if args.production else ":80",
    "ZAVLIQ_RELEASE": "development",
}
env_path.write_text("".join(f"{k}={v}\n" for k, v in values.items()))
print(f"Created private {mode} configuration at {directory}; credentials were not printed.")
