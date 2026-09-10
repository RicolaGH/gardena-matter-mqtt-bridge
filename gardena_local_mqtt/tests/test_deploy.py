import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import deploy
import storage
from worker import binding


class DeployTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.patch=patch.object(storage,'ROOT',Path(self.temp.name));self.patch.start()
        self.cfg={'gateway_host':'192.0.2.1','device_id':'testonly-device',
            'mqtt_broker_host':'192.0.2.2','mqtt_broker_port':1883,'mqtt_broker_user':'test',
            'mqtt_broker_password':'secret;$literal','mqtt_topic_prefix':'gardena','mqtt_ha_prefix':'homeassistant'}
        self.plan={'phase':'active','binding':binding(self.cfg),'mower_ids':['test-device'],
            'plan':[{'topic':'homeassistant/sensor/gardena_abcd/battery/config','serial':'test-device',
                     'resource':'battery_level','key':'abcd','config':{'unique_id':'old-id','state_topic':'gardena/abcd/battery_level/state'},
                     'original':{'unused':'not deployed'}}]}
        storage.write('migration.json',self.plan)
        self.binary=storage.ROOT/'binary';self.binary.write_bytes(b'fake-build')
        self.gw=Mock(host='192.0.2.1',key='/data/ssh/key')

    def tearDown(self):
        self.patch.stop();self.temp.cleanup()

    def test_secrets_only_in_private_config_not_unit_or_argv(self):
        result=deploy.runtime_config(self.cfg,self.plan)
        self.assertEqual(result['mqtt_password'],'secret;$literal')
        self.assertNotIn('original',result['plan'][0])
        self.assertNotIn('secret',deploy.UNIT)
        with patch.object(deploy.subprocess,'run',return_value=Mock(returncode=0)) as run:
            deploy.send_input(self.gw,'cat > /fixed/path',json.dumps(result).encode())
        self.assertNotIn('secret',str(run.call_args.args))
        self.assertIn(b'secret',run.call_args.kwargs['input'])

    def install(self,stop):
        with patch.object(deploy,'gateway',return_value=(self.cfg,self.gw)), \
             patch.object(deploy,'BINARY',self.binary), \
             patch.object(deploy.telemetry,'parse_snapshot',return_value=[]), \
             patch.object(deploy.telemetry,'readings'), \
             patch.object(deploy,'upload_binary'),patch.object(deploy,'send_input'), \
             patch.object(deploy,'health',return_value=True):
            deploy.install(stop)

    def test_preflight_completes_before_stopping_ha(self):
        def stop():
            self.assertTrue(any('-check' in c.args[0] for c in self.gw.ssh.call_args_list))
            self.assertEqual(storage.read('gateway_deployment.json')['phase'],'switching')
        self.install(stop)
        self.assertEqual(storage.read('gateway_deployment.json')['phase'],'active')
        self.assertEqual(storage.read('status.json')['mode'],'gateway')

    def test_preflight_failure_leaves_existing_ha_process_running(self):
        def ssh(command,**kwargs):
            if '-check' in command:raise ConnectionError()
            return b''
        self.gw.ssh.side_effect=ssh
        stop=Mock()
        with self.assertRaises(ConnectionError):self.install(stop)
        stop.assert_not_called()
        self.assertIsNone(storage.read('gateway_deployment.json'))

    def test_failed_handover_only_restores_ha_after_gateway_stop(self):
        def ssh(command,**kwargs):
            if 'systemctl daemon-reload' in command:raise ConnectionError()
            return b''
        self.gw.ssh.side_effect=ssh
        with self.assertRaises(ConnectionError):self.install(Mock())
        self.assertIn('systemctl stop gardena-local.service',self.gw.ssh.call_args.args[0])
        self.assertIsNone(storage.read('gateway_deployment.json'))

    def test_uncertain_gateway_state_blocks_ha_restart(self):
        def ssh(command,**kwargs):
            if 'systemctl' in command:raise ConnectionError()
            return b''
        self.gw.ssh.side_effect=ssh
        with self.assertRaises(ConnectionError):self.install(Mock())
        self.assertEqual(storage.read('gateway_deployment.json')['phase'],'switching')

    def test_status_poll_has_no_mqtt_or_gateway_service_mutations(self):
        storage.write('gateway_deployment.json',{'phase':'active','binding':binding(self.cfg)})
        with patch.object(deploy,'gateway',return_value=(self.cfg,self.gw)),patch.object(deploy,'health',return_value=True):
            deploy.refresh_status()
        self.gw.prepare.assert_not_called()
        self.gw.stop_publisher.assert_not_called()

    def test_restore_stops_gateway_before_removing_journal(self):
        storage.write('gateway_deployment.json',{'phase':'active','binding':binding(self.cfg)})
        with patch.object(deploy,'gateway',return_value=(self.cfg,self.gw)):
            deploy.restore_ha()
        self.assertIn('systemctl stop gardena-local.service',self.gw.ssh.call_args.args[0])
        self.assertIsNone(storage.read('gateway_deployment.json'))


if __name__=='__main__':unittest.main()
