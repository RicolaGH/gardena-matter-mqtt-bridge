"""Local gateway bootstrap and bounded, read-only LsDL snapshots.

No binary is installed and no upstream repository is contacted.
Certificate verification remains the documented follow-up from 0.3.2.
"""
import json
import os
from pathlib import Path
import selectors
import ssl
import subprocess
import time
import urllib.request
from security import ssh_options, validate_host

MAX_SNAPSHOT = 8 * 1024 * 1024
SERVICE = 'gardena-mqtt-publisher.service'


class Gateway:
    def __init__(self, host, device_id, key):
        self.host = validate_host(host)
        self.password = device_id[:8]
        self.key = key
        self.session = None

    def request(self, method, path, body):
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        headers = {'Content-Type': 'application/json'}
        if self.session:
            headers['X-Session'] = self.session
        request = urllib.request.Request('https://' + self.host + path,
            data=json.dumps(body).encode(), headers=headers, method=method)
        # Do not redirect credentials or send gateway requests through ambient proxies.
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}),
            urllib.request.HTTPSHandler(context=context), NoRedirect())
        with opener.open(request, timeout=15) as response:
            raw = response.read(65537)
            if len(raw) > 65536:
                raise ValueError('Gateway response too large')
            return json.loads(raw) if raw else {}

    def ssh(self, command, limit=65536, timeout=20):
        proc = subprocess.Popen(['ssh', '-T', *ssh_options(), '-o', 'ConnectTimeout=5',
            '-i', self.key, 'root@' + self.host, command], stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            env={k: v for k, v in os.environ.items() if 'TOKEN' not in k and 'PASSWORD' not in k})
        selector = selectors.DefaultSelector()
        output = bytearray()
        deadline = time.monotonic() + timeout
        try:
            selector.register(proc.stdout, selectors.EVENT_READ)
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0 or not selector.select(remaining):
                    raise TimeoutError('SSH timeout')
                chunk = os.read(proc.stdout.fileno(), min(65536, limit + 1 - len(output)))
                if not chunk:
                    break
                output.extend(chunk)
                if len(output) > limit:
                    raise ValueError('SSH output exceeds limit')
            if proc.wait(timeout=max(0.1, deadline-time.monotonic())):
                raise ConnectionError('SSH command failed')
            return bytes(output)
        finally:
            selector.close()
            if proc.poll() is None:
                proc.kill()
            proc.wait()
            proc.stdout.close()

    def prepare(self):
        self.session = self.request('POST', '/login', {'password': self.password}).get('session')
        if not isinstance(self.session, str) or not self.session:
            raise ConnectionError('Gateway login failed')
        try:
            self.ssh('true')
        except ConnectionError:
            key = Path(self.key + '.pub').read_text().strip()
            if not key.startswith('ssh-ed25519 '):
                raise ValueError('Invalid public key')
            self.request('POST', '/ssh_access_credentials',
                {name: key for name in ('key', 'public_key', 'ssh_public_key')})
            self.request('PUT', '/ssh_access_enable', {'enable': True})
        self.request('PUT', '/websocket_api', {'enable': True})

    def snapshot(self):
        return self.ssh('tar -C /var/lib/lemonbeatd -cf - .', MAX_SNAPSHOT, 20)

    def publisher_state(self):
        raw = self.ssh('systemctl is-active ' + SERVICE + ' 2>/dev/null; '
            'systemctl is-enabled ' + SERVICE + ' 2>/dev/null; true').decode().splitlines()
        if len(raw) != 2 or raw[0] not in ('active', 'inactive', 'failed', 'unknown') or raw[1] not in (
                'enabled', 'disabled', 'static', 'masked', 'not-found', 'indirect'):
            raise ValueError('Unknown publisher state')
        return {'active': raw[0] == 'active', 'enabled': raw[1] == 'enabled'}

    def stop_publisher(self):
        self.ssh('set -e; if systemctl cat ' + SERVICE + ' >/dev/null 2>&1; then '
            'systemctl stop ' + SERVICE + '; systemctl disable ' + SERVICE + '; fi; '
            'if systemctl is-active --quiet ' + SERVICE + '; then exit 1; fi')

    def restore_publisher(self, previous):
        commands = []
        if previous.get('enabled'):
            commands.append('systemctl enable ' + SERVICE)
        if previous.get('active'):
            commands.append('systemctl start ' + SERVICE)
        if commands:
            self.ssh('set -e; ' + '; '.join(commands))


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ConnectionError('Gateway redirect refused')
