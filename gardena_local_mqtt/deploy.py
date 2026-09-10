"""Install the independently supervised gateway runtime; no MQTT client here."""
import hashlib
import json
import secrets
from pathlib import Path
import subprocess
import time
import urllib.error

import storage
import telemetry
from gateway import Gateway
from security import ssh_options
from worker import binding, options, report

SERVICE = 'gardena-local.service'
REMOTE = '/usr/local/lib/gardena-local'
BINARY = Path(__file__).with_name('gardena-local-gateway')
UNIT = '''[Unit]
Description=GARDENA Local MQTT gateway runtime
Wants=network-online.target
After=network-online.target lemonbeatd.service
StartLimitIntervalSec=0

[Service]
Type=simple
ExecStart=/usr/local/lib/gardena-local/current/gardena-local-gateway -config /usr/local/lib/gardena-local/current/config.json
Restart=always
RestartSec=15
UMask=0077
Environment=GOMEMLIMIT=32MiB
Environment=GOGC=50
NoNewPrivileges=true

[Install]
WantedBy=multi-user.target
'''


def gateway():
    cfg = options()
    return cfg, Gateway(cfg['gateway_host'], cfg['device_id'].strip(),
                        str(storage.ROOT/'ssh/addon_ed25519'))


def send_input(gw, command, data):
    # Only constant shell commands and locally generated hashes in command text.
    result = subprocess.run(['ssh', '-T', *ssh_options(), '-o', 'ConnectTimeout=5',
        '-i', gw.key, 'root@'+gw.host, command], input=data,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30, check=False)
    if result.returncode:
        raise ConnectionError('Gateway upload failed')


def upload_binary(gw, directory):
    result = subprocess.run(['scp', '-O', *ssh_options(), '-o', 'ConnectTimeout=5',
        '-i', gw.key, str(BINARY), 'root@'+gw.host+':'+directory+'/binary.tmp'],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        timeout=180, check=False)
    if result.returncode:
        raise ConnectionError('Gateway binary upload failed')


def runtime_config(cfg, migration):
    if not migration or migration.get('phase') != 'active' or migration.get('binding') != binding(cfg):
        raise ValueError('Eine aktive und passende Sensorübernahme ist erforderlich.')
    return {'mqtt_host': cfg['mqtt_broker_host'], 'mqtt_port': cfg['mqtt_broker_port'],
        'mqtt_user': cfg.get('mqtt_broker_user',''), 'mqtt_password': cfg.get('mqtt_broker_password',''),
        'gateway_password': cfg['device_id'].strip()[:8], 'topic_prefix': cfg['mqtt_topic_prefix'],
        'ha_prefix': cfg['mqtt_ha_prefix'], 'plan': [
            {k:r[k] for k in ('topic','serial','resource','key','config')} for r in migration['plan']],
        'mower_ids': migration['mower_ids']}


def health(gw, expected_version=None):
    raw = gw.ssh('systemctl is-active --quiet '+SERVICE+' && cat /run/gardena-local-status.json')
    state = json.loads(raw)
    return state.get('ready') is True and state.get('version') in ((expected_version,) if expected_version else ('0.2.0', '0.2.2')) and abs(time.time()-state.get('updated',0)) < 90


STEPS = {
    'configuration': 'Einstellungen und Sensorzuordnung prüfen',
    'gateway_access': 'Gateway-Anmeldung und SSH-Zugang prüfen',
    'sensor_snapshot': 'Aktuelle Sensordaten lesen',
    'local_binary': 'Mitgeliefertes Gateway-Programm lesen',
    'gateway_space': 'Gateway-Architektur und freien Speicher prüfen',
    'staging': 'Installationsverzeichnis vorbereiten',
    'upload': 'Gateway-Programm übertragen',
    'checksum': 'Übertragung und Prüfsumme prüfen',
    'configuration_upload': 'Private Konfiguration übertragen',
    'runtime_check': 'Neues Programm und lokale Gateway-API prüfen',
    'handover': 'Gateway-Dienst umschalten',
    'health': 'Betriebsbereitschaft der neuen Runtime prüfen',
}

class InstallError(Exception):
    """Only locally constructed, non-sensitive explanations."""


def step(name):
    storage.write('installation.json', {'phase': 'running', 'step': name,
        'message': 'Installation: '+STEPS[name]})


def check_space(gw, required_kb):
    architecture = gw.ssh('uname -m').decode().strip()
    if architecture != 'mips':
        raise InstallError('Nicht unterstützte Gateway-Architektur; erwartet wird MIPS.')
    raw = gw.ssh("df -Pk /usr/local/lib | awk 'END {print $4}'").decode().strip()
    if not raw.isascii() or not raw.isdecimal() or len(raw)>12:
        raise InstallError('Freier Gateway-Speicher konnte nicht eindeutig ermittelt werden.')
    free_kb = int(raw)
    if free_kb < required_kb:
        raise InstallError(f'Zu wenig freier Speicher auf dem Gateway: {free_kb} KiB frei, mindestens {required_kb} KiB benötigt. Es wurde keine neue Runtime übertragen und kein Dienst gestoppt.')


def install(stop_worker):
    step('configuration')
    try:
        _install(stop_worker)
        storage.write('installation.json', {'phase': 'done',
            'message': 'Gateway-Installation abgeschlossen; Runtime 0.2.2 ist bereit.'})
    except Exception as exc:
        state = storage.read('installation.json', {}) or {}
        name = state.get('step', 'configuration')
        label = STEPS.get(name, STEPS['configuration'])
        if isinstance(exc, InstallError):
            reason = str(exc)
        elif isinstance(exc, (TimeoutError, subprocess.TimeoutExpired)):
            reason = 'Zeitlimit überschritten.'
        elif isinstance(exc, urllib.error.HTTPError):
            reason = f'Gateway antwortet mit HTTP {exc.code}.'
        elif isinstance(exc, FileNotFoundError):
            reason = 'Eine erforderliche lokale Datei fehlt.'
        elif isinstance(exc, ConnectionError):
            reason = 'Verbindung oder entfernter Befehl fehlgeschlagen.'
        elif isinstance(exc, ValueError):
            reason = 'Daten oder Zuordnung konnten nicht validiert werden.'
        else:
            reason = 'Schritt konnte nicht abgeschlossen werden.'
        message = 'Fehler bei „'+label+'“: '+reason
        storage.write('installation.json', {'phase': 'error', 'step': name, 'message': message})
        print('[gardena-install] '+message, flush=True)
        raise


def _install(stop_worker):
    cfg, gw = gateway()
    migration = storage.read('migration.json')
    payload = runtime_config(cfg, migration)
    step('gateway_access')
    gw.prepare()
    step('sensor_snapshot')
    telemetry.readings(migration['plan'], telemetry.parse_snapshot(gw.snapshot()))
    step('local_binary')
    digest = hashlib.sha256(BINARY.read_bytes()).hexdigest()
    directory = REMOTE+'/releases/'+digest+'-'+secrets.token_hex(4)
    size_kb = (BINARY.stat().st_size+1023)//1024 + 512
    step('gateway_space')
    check_space(gw, size_kb)
    step('staging')
    report('deploying', message='Gateway wird geprüft und vorbereitet. MQTT läuft bis zur Umschaltung weiter.')
    # Preserve all previous application and vendor files. Fail before handover if full.
    gw.ssh('set -e; test "$(uname -m)" = mips; '
        'mkdir -p '+REMOTE+'/releases; chmod 700 '+REMOTE+' '+REMOTE+'/releases; '
        'free=$(df -Pk '+REMOTE+' | awk \'END {print $4}\'); test "$free" -ge '+str(size_kb)+'; '
        'mkdir -p '+directory+'; chmod 700 '+directory)
    step('upload')
    upload_binary(gw,directory)
    step('checksum')
    gw.ssh('set -e; echo "'+digest+'  '+directory+'/binary.tmp" | sha256sum -c -; '
        'chmod 755 '+directory+'/binary.tmp; mv -f '+directory+'/binary.tmp '+directory+'/gardena-local-gateway')
    step('configuration_upload')
    send_input(gw,'umask 077; cat > '+directory+'/config.json',json.dumps(payload).encode())
    send_input(gw,'umask 077; cat > '+directory+'/gardena-local.service',UNIT.encode())
    step('runtime_check')
    report('deploying', message='Gateway prüft Sensoren und lokale Steuerung, ohne Befehle auszuführen.')
    gw.ssh(directory+'/gardena-local-gateway -config '+directory+'/config.json -check', timeout=90)
    # Journal before terminating the HA client. On uncertain outcomes never restart it automatically.
    step('handover')
    storage.write('gateway_deployment.json', {'phase':'switching','binding':binding(cfg),'digest':digest})
    report('deploying', message='MQTT-Betrieb wird auf das Gateway übertragen.')
    stop_worker()
    try:
        gw.stop_publisher()
        gw.ssh('set -e; systemctl stop '+SERVICE+' 2>/dev/null || true; '
            'ln -sfn '+directory+' '+REMOTE+'/current; '
            'cp '+directory+'/gardena-local.service /etc/systemd/system/'+SERVICE+'; '
            'chmod 644 /etc/systemd/system/'+SERVICE+'; '
            'rm -f /run/gardena-local-status.json; systemctl daemon-reload; '
            'systemctl enable '+SERVICE+'; systemctl start '+SERVICE)
        step('health')
        deadline = time.monotonic()+120
        while time.monotonic()<deadline:
            try:
                if health(gw, expected_version='0.2.2'):
                    storage.write('gateway_deployment.json', {'phase':'active','binding':binding(cfg),'digest':digest})
                    report('gateway', message='MQTT läuft auf dem Gateway. Diese HA-App kann gestoppt werden.',
                           sensors=len(payload['plan']),mowers=len(payload['mower_ids']))
                    return
            except (ConnectionError, ValueError):
                pass
            time.sleep(3)
        raise ConnectionError('Gateway not ready')
    except BaseException:
        # If gateway cannot be stopped/verified, keep the journal to prevent two controllers.
        try:
            gw.ssh('systemctl stop '+SERVICE+'; systemctl disable '+SERVICE+'; '
                'if systemctl is-active --quiet '+SERVICE+'; then exit 1; fi')
            storage.write('gateway_deployment.json', None)
        except Exception:
            pass
        raise


def restore_ha():
    cfg, gw = gateway()
    state=storage.read('gateway_deployment.json')
    if not state or state['binding']!=binding(cfg):
        raise ValueError('Configuration mismatch')
    # Never touch the old upstream publisher: this returns to the saved HA runtime.
    gw.ssh('set -e; systemctl stop '+SERVICE+'; systemctl disable '+SERVICE+'; '
           'if systemctl is-active --quiet '+SERVICE+'; then exit 1; fi')
    storage.write('gateway_deployment.json',None)
    report('starting',message='Der bisherige HA-Betrieb wird wieder gestartet.')


def refresh_status():
    cfg,gw=gateway()
    state=storage.read('gateway_deployment.json')
    if state['binding']!=binding(cfg):
        raise ValueError('Configuration mismatch')
    if health(gw):
        state['phase']='active';storage.write('gateway_deployment.json',state)
        plan=storage.read('migration.json',{})
        report('gateway',message='MQTT läuft auf dem Gateway. Diese HA-App kann gestoppt werden.',
            sensors=len(plan.get('plan',[])),mowers=len(plan.get('mower_ids',[])))
    else:
        report('gateway_error',message='Gateway-Dienst ist noch nicht bereit. Der Dienst versucht die Verbindung selbst erneut.')
