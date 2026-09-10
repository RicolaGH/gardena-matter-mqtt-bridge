"""Explicit, resumable removal of known legacy Matter components only."""
from pathlib import Path
import subprocess
import time
import deploy
import storage
from security import ssh_options
from worker import binding


def run():
    cfg, gw = deploy.gateway()
    state = storage.read('gateway_deployment.json') or {}
    if state.get('phase') != 'active' or state.get('binding') != binding(cfg) or not deploy.health(gw):
        raise ValueError('Healthy gateway MQTT deployment required')
    storage.write('cleanup.json', {'phase': 'running'})
    try:
        result = subprocess.run(['ssh', '-T', *ssh_options(), '-o', 'ConnectTimeout=5',
            '-i', gw.key, 'root@'+gw.host, 'env -i PATH=/usr/sbin:/usr/bin:/sbin:/bin sh -s'],
            input=Path(__file__).with_name('cleanup.sh').read_bytes(),
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=180, check=False)
        if result.returncode:
            raise ConnectionError('Matter cleanup incomplete')
        if not deploy.health(gw):
            raise ConnectionError('MQTT health check failed after cleanup')
        storage.write('cleanup.json', {'phase': 'done', 'updated': time.time(),
            'message': 'Matter-Bereinigung abgeschlossen. MQTT auf dem Gateway ist bereit.'})
    except Exception:
        storage.write('cleanup.json', {'phase': 'error',
            'message': 'Bereinigung nicht bestätigt. Sicherung bleibt erhalten; MQTT-Status prüfen. Erneuter Versuch möglich.'})
        raise
