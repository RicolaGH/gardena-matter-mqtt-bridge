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

PAGE = '''<!doctype html><html lang="de"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>GARDENA Local MQTT</title><style>
body{font:17px system-ui;margin:2rem auto;padding:0 1rem;max-width:680px;color:#20382c;background:#f4f8f5}
main{background:white;padding:1.5rem;border-radius:16px}button{font:inherit;padding:12px;margin:8px 8px 0 0;border:0;border-radius:8px;background:#25754a;color:white}button:disabled{opacity:.4}
#back{background:#555}#status{white-space:pre-line;line-height:1.7}</style>
<main><h1>GARDENA Local MQTT</h1><p>Sensoren und Mähersteuerung direkt in Home Assistant.</p>
<p id="status">Verbindung wird vorbereitet …</p>
<p>Vor der Übernahme: Die bisherige Bridge-App stoppen und dort Autostart und Watchdog ausschalten.
Die Übernahme beendet den bisherigen MQTT-Publisher auf dem Gateway. Dateien bleiben erhalten.</p>
<button id="take" disabled>Sensoren und Steuerung übernehmen</button>
<button id="back" disabled>Zum bisherigen Publisher zurückkehren</button>
<p id="result" role="status"></p></main><script>
const statusNode=document.getElementById('status'), take=document.getElementById('take'), back=document.getElementById('back');
async function refresh(){try{let r=await fetch('api/status');if(!r.ok)throw Error();let s=await r.json();
statusNode.textContent=s.message+'\\n'+(s.sensors===undefined?'':s.sensors+' Sensoren · '+s.mowers+' Mäher');
take.disabled=s.mode!=='preview'||!s.fresh;back.disabled=!s.can_rollback;
}catch(e){statusNode.textContent='Status momentan nicht erreichbar.';take.disabled=true;}}
async function action(name){take.disabled=true;back.disabled=true;try{let r=await fetch('api/'+name,{method:'POST'});
document.getElementById('result').textContent=r.ok?'Auftrag wird ausgeführt …':'Auftrag momentan nicht möglich.';}catch(e){document.getElementById('result').textContent='Verbindung unterbrochen.';}}
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
            status['can_rollback'] = bool(storage.read('migration.json'))
            self.send(200, status)
        else:
            self.send(404, {})

    def do_POST(self):
        if not self.allowed():
            return
        status = storage.read('status.json', {})
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


def main():
    # Discard stale button presses, but preserve migration recovery journal.
    storage.write('action.json', None)
    storage.write('status.json', {'mode': 'starting', 'message': 'Verbindung wird vorbereitet …'})
    server = Server(('0.0.0.0', 8099), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    stopping = threading.Event()
    child = None

    def stop(*_):
        stopping.set()
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        while not stopping.is_set():
            child = subprocess.Popen([sys.executable, str(Path(__file__).with_name('worker.py'))], start_new_session=True)
            while child.poll() is None and not stopping.wait(0.25):
                pass
            try:
                os.killpg(child.pid, signal.SIGTERM)
                child.wait(timeout=5)
            except (ProcessLookupError, subprocess.TimeoutExpired):
                pass
            finally:
                try:
                    os.killpg(child.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                child.wait()
            if child.returncode == 0 and not stopping.is_set():
                # Rollback leaves the UI available but never restarts publishing.
                stopping.wait()
            stopping.wait(15)
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


if __name__ == '__main__':
    main()
