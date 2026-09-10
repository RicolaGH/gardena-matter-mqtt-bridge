"""Ingress-only UI and independent worker supervision."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading
import time
import storage
import deploy

DEPLOY_LOCK = threading.Lock()

PAGE = '''<!doctype html><html lang="de"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>GARDENA Local MQTT</title><style>
body{font:17px system-ui;margin:2rem auto;padding:0 1rem;max-width:680px;color:#20382c;background:#f4f8f5}
main{background:white;padding:1.5rem;border-radius:16px}button{font:inherit;padding:12px;margin:8px 8px 0 0;border:0;border-radius:8px;background:#25754a;color:white}button:disabled{opacity:.4}
#back{background:#555}#status{white-space:pre-line;line-height:1.7}</style>
<main><h1>GARDENA Local MQTT</h1><p>Sensoren und Mähersteuerung auf dem GARDENA-Gateway.</p>
<p id="status">Verbindung wird vorbereitet …</p>
<p>Vor der Übernahme: Die bisherige Bridge-App stoppen und dort Autostart und Watchdog ausschalten.
Die Übernahme beendet den bisherigen MQTT-Publisher auf dem Gateway. Dateien bleiben erhalten.</p>
<button id="take" disabled>Sensoren und Steuerung übernehmen</button>
<button id="back" disabled>Zum bisherigen Publisher zurückkehren</button>
<p>Nach der Sensorübernahme: Den eigenständigen Gateway-Dienst installieren. Danach wird diese HA-App für den Betrieb nicht benötigt.</p>
<button id="install" disabled>Auf Gateway installieren</button>
<button id="restore" disabled>HA-Betrieb wiederherstellen</button>
<p id="result" role="status"></p></main><script>
const statusNode=document.getElementById('status'), take=document.getElementById('take'), back=document.getElementById('back');
async function refresh(){try{let r=await fetch('api/status');if(!r.ok)throw Error();let s=await r.json();
statusNode.textContent=s.message+'\\n'+(s.sensors===undefined?'':s.sensors+' Sensoren · '+s.mowers+' Mäher');
take.disabled=s.mode!=='preview'||!s.fresh;back.disabled=!s.can_rollback;
document.getElementById('install').disabled=!s.can_install;document.getElementById('restore').disabled=!s.can_restore;
if(s.mode==='gateway')document.getElementById('result').textContent='Installation abgeschlossen.';
}catch(e){statusNode.textContent='Status momentan nicht erreichbar.';take.disabled=true;}}
async function action(name){take.disabled=true;back.disabled=true;try{let r=await fetch('api/'+name,{method:'POST'});
document.getElementById('result').textContent=r.ok?'Auftrag wird ausgeführt …':'Auftrag momentan nicht möglich.';}catch(e){document.getElementById('result').textContent='Verbindung unterbrochen.';}}
document.getElementById('install').onclick=()=>action('install');document.getElementById('restore').onclick=()=>action('restore');
take.onclick=()=>action('activate');back.onclick=()=>action('rollback');refresh();setInterval(refresh,3000);
</script></html>'''


class Server(ThreadingHTTPServer):
    daemon_threads = True
    slots = threading.BoundedSemaphore(16)

    def process_request(self, request, address):
        if not self.slots.acquire(False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, address)
        except BaseException:
            self.slots.release()
            raise

    def process_request_thread(self, request, address):
        try:
            super().process_request_thread(request, address)
        finally:
            self.slots.release()


class Handler(BaseHTTPRequestHandler):
    def setup(self):
        super().setup()
        self.connection.settimeout(10)

    def send(self, code, content, kind='application/json'):
        raw = content.encode() if isinstance(content, str) else json.dumps(content).encode()
        self.send_response(code)
        for key, value in [('Content-Type', kind), ('Content-Length', str(len(raw))),
                ('Cache-Control', 'no-store'), ('X-Content-Type-Options', 'nosniff')]:
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(raw)

    def allowed(self):
        if self.client_address[0] != '172.30.32.2':
            self.send(403, {'error': 'Ingress required'})
            return False
        return True

    def do_GET(self):
        if not self.allowed():
            return
        if self.path in ('/', '/index.html'):
            self.send(200, PAGE, 'text/html; charset=utf-8')
        elif self.path == '/api/status':
            status = storage.read('status.json', {'mode': 'starting', 'message': 'Verbindung wird vorbereitet …'})
            status['fresh'] = time.time() - status.get('updated', 0) < 45
            gateway_mode = bool(storage.read('gateway_deployment.json'))
            migration = storage.read('migration.json', {}) or {}
            status['can_rollback'] = bool(migration) and not gateway_mode and not DEPLOY_LOCK.locked()
            status['can_install'] = migration.get('phase') == 'active' and not DEPLOY_LOCK.locked()
            status['can_restore'] = gateway_mode and not DEPLOY_LOCK.locked()
            self.send(200, status)
        else:
            self.send(404, {})

    def do_POST(self):
        if not self.allowed():
            return
        status = storage.read('status.json', {})
        if self.path in ('/api/install', '/api/restore'):
            if not storage.read('migration.json') or not DEPLOY_LOCK.acquire(False):
                self.send(409, {})
                return
            storage.write('deployment_action.json', self.path.rsplit('/', 1)[1])
            self.send(202, {})
            return
        if storage.read('gateway_deployment.json') or DEPLOY_LOCK.locked():
            self.send(409, {})
            return
        if self.path == '/api/activate' and status.get('mode') == 'preview' and time.time()-status.get('updated', 0) < 45:
            storage.write('action.json', 'activate')
        elif self.path == '/api/rollback' and storage.read('migration.json'):
            storage.write('action.json', 'rollback')
        else:
            self.send(409, {})
            return
        self.send(202, {})

    def log_message(self, *_):
        pass


def stop_child(child):
    if child is None:
        return
    try:
        os.killpg(child.pid, signal.SIGTERM)
        child.wait(timeout=8)
    except (ProcessLookupError, subprocess.TimeoutExpired):
        pass
    finally:
        try:
            os.killpg(child.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        child.wait()


def main():
    storage.write('action.json', None)
    storage.write('deployment_action.json', None)
    storage.write('status.json', {'mode': 'starting', 'message': 'Status wird geprüft …'})
    server = Server(('0.0.0.0', 8099), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    stopping = threading.Event()
    child = None
    next_check = 0

    def stop(*_):
        stopping.set()

    def stop_worker():
        nonlocal child
        stop_child(child)
        child = None

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        while not stopping.is_set():
            action = storage.read('deployment_action.json')
            if action:
                storage.write('deployment_action.json', None)
                try:
                    if action == 'install':
                        deploy.install(stop_worker)
                    elif action == 'restore':
                        deploy.restore_ha()
                    next_check = 0
                except Exception:
                    storage.write('status.json', {'mode': 'gateway_error', 'updated': time.time(),
                        'message': 'Gateway-Installation fehlgeschlagen. Ein unklarer Gateway-Status verhindert parallele Steuerung. Protokoll prüfen.'})
                    print('[gardena-install] Installation nicht abgeschlossen; Gateway-Daten und SSH-Verbindung prüfen.', flush=True)
                    next_check = time.monotonic() + 30
                finally:
                    if DEPLOY_LOCK.locked():
                        DEPLOY_LOCK.release()
            if storage.read('gateway_deployment.json'):
                stop_worker()
                if time.monotonic() >= next_check:
                    try:
                        deploy.refresh_status()
                    except Exception:
                        storage.write('status.json', {'mode': 'gateway_error', 'updated': time.time(),
                            'message': 'Gateway-Status nicht erreichbar. Die HA-App verarbeitet keine MQTT-Befehle.'})
                    next_check = time.monotonic()+30
            elif child is None or child.poll() is not None:
                if child is not None:
                    result=child.returncode
                    stop_worker()
                    if result == 0:
                        stopping.wait()
                        continue
                    if stopping.wait(15):
                        break
                child = subprocess.Popen([sys.executable, str(Path(__file__).with_name('worker.py'))], start_new_session=True)
            stopping.wait(0.25)
    finally:
        # Only local processes are stopped. No service-stop or MQTT-offline call to the gateway.
        stop_worker()
        server.shutdown()
        server.server_close()
        thread.join()


if __name__ == '__main__':
    main()
