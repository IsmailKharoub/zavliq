#!/usr/bin/env python3
"""Provision the private service identity without exposing its credentials."""
import hashlib
import hmac
import json
import os
from pathlib import Path
import secrets
import urllib.request

base = "http://synapse:8008"
target = Path("/bootstrap/admin_token")
target.parent.mkdir(parents=True, exist_ok=True)
os.umask(0o077)

def call(path, payload=None, token=None):
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = "Bearer " + token
    req = urllib.request.Request(base + path, data=json.dumps(payload).encode() if payload else None, headers=headers)
    with urllib.request.urlopen(req, timeout=30) as response:
        return json.load(response)

if target.exists():
    call("/_matrix/client/v3/account/whoami", token=target.read_text().strip())
    print("Existing internal service credential verified.")
else:
    shared = Path("/run/secrets/registration_secret").read_text().strip()
    nonce = call("/_synapse/admin/v1/register")["nonce"]
    username = "zavliq-service"
    password = secrets.token_urlsafe(64)
    body = "\0".join([nonce, username, password, "admin"])
    mac = hmac.new(shared.encode(), body.encode(), hashlib.sha1).hexdigest()
    result = call("/_synapse/admin/v1/register", {"nonce": nonce, "username": username, "password": password, "admin": True, "mac": mac})
    target.write_text(result["access_token"] + "\n")
    target.chmod(0o600)
    print("Internal service credential persisted; value withheld.")
