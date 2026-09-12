#!/usr/bin/env python3
"""Render Synapse configuration in its private volume before it starts."""
import os
from pathlib import Path
import yaml

data = Path("/data")
data.mkdir(parents=True, exist_ok=True)
server_name = os.environ["ZAVLIQ_SERVER_NAME"]
secret = lambda name: Path(f"/run/secrets/{name}").read_text().strip()
config = {
    "server_name": server_name,
    "public_baseurl": os.environ["ZAVLIQ_PUBLIC_URL"] + "/",
    "pid_file": "/data/homeserver.pid",
    "listeners": [{"port": 8008, "type": "http", "tls": False, "bind_addresses": ["0.0.0.0"], "x_forwarded": True, "resources": [{"names": ["client"], "compress": False}]}],
    "database": {"name": "psycopg2", "args": {"user": "zavliq", "password": secret("postgres_password"), "database": "synapse", "host": "postgres", "port": 5432, "cp_min": 5, "cp_max": 10}},
    "log_config": "/data/log.config",
    "media_store_path": "/data/media_store",
    "signing_key_path": "/data/signing.key",
    "registration_shared_secret": secret("registration_secret"),
    "macaroon_secret_key": secret("control_secret"),
    "enable_registration": False,
    "allow_guest_access": False,
    "report_stats": False,
    "enable_metrics": False,
    "federation_domain_whitelist": [],
    "trusted_key_servers": [],
    "suppress_key_server_warning": True,
    "allow_public_rooms_over_federation": False,
    "allow_public_rooms_without_auth": False,
    "serve_server_wellknown": False,
    "url_preview_enabled": False,
    "enable_authenticated_media": True,
    "max_upload_size": "10M",
    "max_image_pixels": "16M",
    "media_retention": {"local_media_lifetime": "30d", "remote_media_lifetime": "1d"},
    "retention": {"enabled": True, "default_policy": {"max_lifetime": "30d"}, "allowed_lifetime_max": "30d", "purge_jobs": [{"interval": "1h"}]},
    "rc_message": {"per_second": 0.5, "burst_count": 30},
    "rc_login": {"address": {"per_second": 0.1, "burst_count": 10}, "account": {"per_second": 0.1, "burst_count": 5}, "failed_attempts": {"per_second": 0.05, "burst_count": 5}},
    "rc_registration": {"per_second": 0.01, "burst_count": 3},
    "enable_room_list_search": True,
    # The mandatory policy module permits public publication only for channels.
    "room_list_publication_rules": [{"action": "allow"}],
    "password_config": {"enabled": True, "localdb_enabled": True},
    "push": {"enabled": False},
    "send_federation": False,
}
module = os.environ.get("ZAVLIQ_POLICY_MODULE", "zavliq_policy.ZavliqPolicy")
if module:
    config["modules"] = [{"module": module, "config": {"database_path": "/data/policy.sqlite", "control_url": "http://control:3000", "server_name": server_name}}]
data.joinpath("homeserver.yaml").write_text(yaml.safe_dump(config, sort_keys=False))
logging = {
    "version": 1,
    "formatters": {"safe": {"format": "%(asctime)s %(levelname)s %(name)s %(message)s"}},
    "handlers": {"console": {"class": "logging.StreamHandler", "formatter": "safe"}},
    "loggers": {"synapse.http.server": {"level": "WARNING"}, "synapse.access.http.8008": {"level": "WARNING"}, "synapse.http.client": {"level": "WARNING"}},
    "root": {"level": "WARNING", "handlers": ["console"]},
    "disable_existing_loggers": False,
}
data.joinpath("log.config").write_text(yaml.safe_dump(logging))
for name in ("homeserver.yaml", "log.config"):
    p = data / name
    p.chmod(0o600)
    os.chown(p, 991, 991)
os.chown(data, 991, 991)
print("Synapse configuration written; secrets withheld.")
