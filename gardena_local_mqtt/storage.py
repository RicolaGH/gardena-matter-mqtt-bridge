"""Atomic private runtime state; never published to MQTT or logs."""
import json
import os
from pathlib import Path
import tempfile

ROOT = Path(os.environ.get('GARDENA_LOCAL_DATA', '/data'))


def read(name, default=None):
    try:
        return json.loads((ROOT / name).read_text())
    except FileNotFoundError:
        return default


def write(name, value):
    ROOT.mkdir(parents=True, exist_ok=True)
    fd, path = tempfile.mkstemp(dir=ROOT, prefix='.state-')
    try:
        with os.fdopen(fd, 'w') as stream:
            json.dump(value, stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(path, ROOT / name)
    finally:
        if os.path.exists(path):
            os.unlink(path)
