"""Atomic, content-free readiness for the local process supervisor."""
import json
import os
from pathlib import Path
import tempfile
import time


class Health:
    def __init__(self, directory: Path, owner: str, now=time.time):
        self.path = directory / 'health.json'
        self.owner, self.now = owner, now
        self.previous, self.written_at = None, 0

    def update(self, status: str):
        if status not in {'starting', 'ready', 'retrying', 'stopped'}:
            raise ValueError('Unsupported health status')
        now = int(self.now())
        if self.previous == status and 0 <= now - self.written_at < 10:
            return
        fd, temporary = tempfile.mkstemp(prefix='.health-', dir=self.path.parent)
        try:
            with os.fdopen(fd, 'w') as output:
                json.dump({'status': status, 'user_id': self.owner, 'updated_at': now}, output)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, self.path)
            directory = os.open(self.path.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
        self.previous, self.written_at = status, now
