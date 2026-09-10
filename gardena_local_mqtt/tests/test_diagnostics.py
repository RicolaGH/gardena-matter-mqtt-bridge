import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import diagnostics
import deploy
import storage
from worker import binding

class DiagnosticTests(unittest.TestCase):
    def data(self):
        return dict.fromkeys(diagnostics.NUMBERS,1)|{'version':'0.2.2','stage':'sensor_read','events':[{'time':1,'stage':'mqtt_read','kind':'timeout'}]}
    def test_only_fixed_fields_are_imported(self):
        d=self.data();d['password']='SECRET';d['events'][0]['payload']='SECRET'
        result=diagnostics.sanitize(json.dumps(d))
        self.assertNotIn('SECRET',json.dumps(result))
        self.assertNotIn('SECRET',diagnostics.display({'data':result}))
    def test_invalid_or_excessive_data_is_rejected(self):
        for change in ({'events':[self.data()['events'][0]]*33},{'stage':'SECRET'},{'mqtt_tx':True},{'pings':-1},{'version':'other'}):
            with self.assertRaises(ValueError):diagnostics.sanitize(json.dumps(self.data()|change))
    def test_refresh_is_read_only_and_retains_last_snapshot_on_failure(self):
        cfg=dict(gateway_host='192.0.2.1',device_id='test-device',mqtt_broker_host='192.0.2.2',mqtt_broker_port=1883,mqtt_topic_prefix='gardena',mqtt_ha_prefix='homeassistant')
        gw=Mock();gw.ssh.return_value=json.dumps(self.data()).encode()
        with tempfile.TemporaryDirectory() as tmp,patch.object(storage,'ROOT',Path(tmp)),patch.object(deploy,'gateway',return_value=(cfg,gw)):
            storage.write('gateway_deployment.json',{'binding':binding(cfg)})
            diagnostics.refresh();gw.ssh.assert_called_once_with('cat /run/gardena-local-diagnostics.json',limit=16384,timeout=8)
            gw.prepare.assert_not_called()
            self.assertEqual(storage.read('diagnostics.json')['data']['stage'],'sensor_read')
            gw.ssh.side_effect=ConnectionError('SECRET')
            diagnostics.refresh();record=storage.read('diagnostics.json')
            self.assertIn('error',record);self.assertIn('data',record);self.assertNotIn('SECRET',json.dumps(record))
    def test_deployment_requires_new_version_but_status_accepts_old_runtime(self):
        import time
        gw=Mock();gw.ssh.return_value=json.dumps({'ready':True,'version':'0.2.0','updated':time.time()}).encode()
        self.assertTrue(deploy.health(gw));self.assertFalse(deploy.health(gw,expected_version='0.2.2'))
        gw.ssh.return_value=json.dumps({'ready':True,'version':'0.2.2','updated':time.time()}).encode()
        self.assertTrue(deploy.health(gw,expected_version='0.2.2'))
