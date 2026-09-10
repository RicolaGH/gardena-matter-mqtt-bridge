"""Execute the actual cleanup shell in a sandbox with a simulated systemd."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import cleanup
import storage
from worker import binding

SYSTEMCTL = '''#!/usr/bin/env python3
import json, os, pathlib, sys
root=pathlib.Path(os.environ['FAKE_ROOT'])
p=root/'systemd.json'; data=json.loads(p.read_text())
args=sys.argv[1:]; cmd=args[0]
with (root/'calls').open('a') as f:f.write(' '.join(args)+'\\n')
if cmd=='list-unit-files':
 for u in data:print(u, 'enabled')
 sys.exit(0)
if cmd=='is-active':sys.exit(0 if os.environ.get('MQTT_DOWN')!='1' else 3)
if cmd=='show':
 u=args[1]
 if 'LoadState' in args:print('LoadState='+('masked' if (root/'etc/systemd/system'/u).is_symlink() else 'loaded' if u in data else 'not-found'))
 else:print('ActiveState='+data.get(u,'inactive'))
 sys.exit(0)
if cmd=='stop':
 if os.environ.get('STOP_FAIL')==args[1]:sys.exit(1)
 data[args[1]]='inactive';p.write_text(json.dumps(data))
'''

class ShellCleanupTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
        self.units=['gardena-matter-restore.path','gardena-matter-restore.service','gardena-matter-toggle.socket','gardena-matter-bridge.service']
        for folder in ('etc/systemd/system','etc/gardena-matter','usr/local/lib/gardena-matter','usr/local/lib/gardena-local/current','usr/share/gateway-config-interface/www/assets','var/lib/lemonbeatd','var/lib/gardena-matter','bin'):
            (self.root/folder).mkdir(parents=True,exist_ok=True)
        self.files=['etc/gardena-matter/matter.html','usr/local/lib/gardena-matter/chip-bridge-app','usr/share/gateway-config-interface/www/assets/matter.html']
        self.protected=['usr/local/lib/gardena-local/current/config.json','var/lib/lemonbeatd/device.json','var/lib/gardena-matter/chip_kvs','usr/share/gateway-config-interface/www/assets/qrcode.min.js','usr/share/gateway-config-interface/www/index.html']
        for f in self.files+self.protected:(self.root/f).write_text('preserve:'+f)
        for u in self.units:(self.root/'etc/systemd/system'/u).write_text('legacy unit '+u)
        (self.root/'systemd.json').write_text(json.dumps(dict.fromkeys(self.units,'active')))
        exe=self.root/'bin/systemctl';exe.write_text(SYSTEMCTL);exe.chmod(0o755)
    def tearDown(self):self.tmp.cleanup()
    def run_script(self,**env):
        script=Path(cleanup.__file__).with_name('cleanup.sh').read_text().replace("root=''", "root='"+str(self.root)+"'")
        return subprocess.run(['sh','-s'],input=script,text=True,capture_output=True,
            env={**os.environ,'FAKE_ROOT':str(self.root),'PATH':str(self.root/'bin')+':'+os.environ['PATH'],**env})
    def test_cleanup_preserves_mqtt_vendor_and_pairing_and_is_repeatable(self):
        for _ in range(2):
            r=self.run_script();self.assertEqual(r.returncode,0,r.stderr)
        for f in self.files:
            self.assertFalse((self.root/f).exists())
            self.assertEqual((self.root/'usr/local/lib/gardena-local/matter-backup/files'/f).read_text(),'preserve:'+f)
        for f in self.protected:self.assertEqual((self.root/f).read_text(),'preserve:'+f)
        calls=(self.root/'calls').read_text()
        self.assertNotIn('stop gardena-local',calls)
        self.assertLess(calls.index('stop gardena-matter-restore.path'),calls.index('stop gardena-matter-bridge.service'))
        self.assertEqual((self.root/'etc/systemd/system/gardena-matter-bridge.service').readlink(),Path('/dev/null'))
        self.assertEqual((self.root/'usr/local/lib/gardena-local/matter-backup').stat().st_mode&0o777,0o700)
    def test_failed_stop_keeps_live_files_and_can_resume(self):
        r=self.run_script(STOP_FAIL='gardena-matter-bridge.service');self.assertNotEqual(r.returncode,0)
        self.assertTrue((self.root/self.files[-1]).exists())
        r=self.run_script();self.assertEqual(r.returncode,0,r.stderr)
    def test_unknown_unit_aborts_before_mutation(self):
        f=self.root/'systemd.json';data=json.loads(f.read_text());data['gardena-matter-custom.service']='active';f.write_text(json.dumps(data))
        self.assertNotEqual(self.run_script().returncode,0)
        self.assertTrue((self.root/self.files[-1]).exists())
        self.assertFalse((self.root/'usr/local/lib/gardena-local/matter-backup').exists())
    def test_symlink_parent_aborts_without_touching_target(self):
        d=self.root/'usr/share/gateway-config-interface/www/assets';d.rename(d.with_name('real-assets'));d.symlink_to(d.with_name('real-assets'))
        self.assertNotEqual(self.run_script().returncode,0)
        self.assertTrue((d/'matter.html').exists())
    def test_unhealthy_mqtt_aborts_before_mutation(self):
        self.assertNotEqual(self.run_script(MQTT_DOWN='1').returncode,0)
        self.assertTrue((self.root/self.files[-1]).exists())
    def test_backup_collision_is_not_overwritten(self):
        b=self.root/'usr/local/lib/gardena-local/matter-backup';b.mkdir();(b/self.units[0]).write_text('different backup')
        self.assertNotEqual(self.run_script().returncode,0)
        self.assertEqual((b/self.units[0]).read_text(),'different backup')

class CleanupGateTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.patch=patch.object(storage,'ROOT',Path(self.tmp.name));self.patch.start()
        self.cfg=dict(gateway_host='192.0.2.1',device_id='test-device',mqtt_broker_host='192.0.2.2',mqtt_broker_port=1883,mqtt_topic_prefix='gardena',mqtt_ha_prefix='homeassistant')
        storage.write('gateway_deployment.json',{'phase':'active','binding':binding(self.cfg)})
    def tearDown(self):self.patch.stop();self.tmp.cleanup()
    def test_health_required_before_ssh(self):
        with patch.object(cleanup.deploy,'gateway',return_value=(self.cfg,Mock())),patch.object(cleanup.deploy,'health',return_value=False),patch.object(cleanup.subprocess,'run') as run:
            with self.assertRaises(ValueError):cleanup.run()
            run.assert_not_called()
    def test_failed_postcheck_never_reports_success(self):
        gw=Mock(host='192.0.2.1',key='/key')
        with patch.object(cleanup.deploy,'gateway',return_value=(self.cfg,gw)),patch.object(cleanup.deploy,'health',side_effect=[True,False]),patch.object(cleanup.subprocess,'run',return_value=Mock(returncode=0)):
            with self.assertRaises(ConnectionError):cleanup.run()
        self.assertEqual(storage.read('cleanup.json')['phase'],'error')
    def test_success_only_after_postcheck(self):
        gw=Mock(host='192.0.2.1',key='/key')
        with patch.object(cleanup.deploy,'gateway',return_value=(self.cfg,gw)),patch.object(cleanup.deploy,'health',return_value=True),patch.object(cleanup.subprocess,'run',return_value=Mock(returncode=0)):
            cleanup.run()
        self.assertEqual(storage.read('cleanup.json')['phase'],'done')
