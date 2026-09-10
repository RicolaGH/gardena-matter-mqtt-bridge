"""Read-only, allowlisted diagnostics from the gateway. Never reads journal or config."""
import json
import time
import storage
import deploy
from worker import binding

STAGES = {'starting','api_enable','ws_connect','ws_discover','mqtt_connect','mqtt_subscribe',
          'sensor_read','mqtt_publish','event_loop','heartbeat','retry_wait','mqtt_read','mqtt_write','ws_read','ws_write'}
KINDS = {'timeout','connection_closed','protocol','file_missing','error'}
NUMBERS = ('updated','started','stage_ms','attempts','mqtt_tx','mqtt_rx','pings','pongs','ws_rx',
           'sensor_ms','sensor_failures','heartbeat_ms','heap_bytes','goroutines')


def number(value):
    if type(value) is not int or not 0 <= value <= 2**63-1:
        raise ValueError('Invalid diagnostic number')
    return value


def sanitize(raw):
    data = json.loads(raw)
    if not isinstance(data, dict) or data.get('version') != '0.2.2' or data.get('stage') not in STAGES:
        raise ValueError('Unsupported diagnostics')
    result = {'version': '0.2.2', 'stage': data['stage']}
    for key in NUMBERS:
        result[key] = number(data.get(key))
    events = data.get('events')
    if not isinstance(events, list) or len(events) > 32:
        raise ValueError('Invalid diagnostic history')
    result['events'] = []
    for event in events:
        if not isinstance(event, dict) or event.get('stage') not in STAGES or event.get('kind') not in KINDS:
            raise ValueError('Invalid diagnostic event')
        result['events'].append({'time': number(event.get('time')), 'stage': event['stage'], 'kind': event['kind']})
    return result


def refresh():
    cfg, gw = deploy.gateway()
    state = storage.read('gateway_deployment.json') or {}
    if state.get('binding') != binding(cfg):
        raise ValueError('Configuration mismatch')
    try:
        raw = gw.ssh('cat /run/gardena-local-diagnostics.json', limit=16384, timeout=8)
        data = sanitize(raw)
        storage.write('diagnostics.json', {'data': data, 'fetched': time.time()})
    except Exception:
        # Keep the last successful snapshot explicitly marked as outdated.
        previous = storage.read('diagnostics.json', {}) or {}
        previous['error'] = 'Diagnose nicht abrufbar. Gateway erreichbar und Runtime 0.2.2 installiert?'
        storage.write('diagnostics.json', previous)


def display(record):
    record = record or {}
    data = record.get('data')
    if not data:
        return record.get('error', 'Noch keine Diagnose. Runtime 0.2.2 über „Auf Gateway installieren“ einrichten.')
    # Revalidate before presentation: diagnostic storage is not a generic log viewer.
    data = sanitize(json.dumps(data))
    def stamp(value):
        return time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime(value)) if value else 'noch keine'
    def age(value):
        return str(max(0, int(time.time())-value))+' s' if value else 'noch keine'
    lines = [record.get('error', ''), 'Runtime: '+data['version'], 'Stand: '+stamp(data['updated']),
        'Alter der Gateway-Messung: '+age(data['updated']),
        'Prozess gestartet: '+stamp(data['started']),
        f"Aktueller Schritt: {data['stage']} (seit {data['stage_ms']} ms)",
        f"Verbindungsversuche: {data['attempts']}",
        'Letzte MQTT-Sendung: '+stamp(data['mqtt_tx']),
        'Letzter MQTT-Empfang: '+stamp(data['mqtt_rx']),
        f"MQTT Ping/Pong: {data['pings']}/{data['pongs']}",
        'Letzter API-Empfang: '+stamp(data['ws_rx']),
        f"Letzte Sensorabfrage: {data['sensor_ms']} ms; Lesefehler: {data['sensor_failures']}",
        f"Letzter Abstand der Lebenszeichen: {data['heartbeat_ms']} ms",
        f"Go-Heap: {data['heap_bytes']//1024} KiB; Goroutinen: {data['goroutines']}",
        'Letzte Verbindungsfehler (max. 32, seit Prozessstart):']
    lines.extend(stamp(e['time'])+' · '+e['stage']+' · '+e['kind'] for e in data['events'])
    return '\n'.join(line for line in lines if line)
