import contextlib
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import deploy
import storage

class InstallationErrorTests(unittest.TestCase):
    def test_insufficient_space_reports_numbers_using_read_only_commands(self):
        gw=Mock();gw.ssh.side_effect=[b'mips\n',b'1024\n']
        with self.assertRaisesRegex(deploy.InstallError,'1024 KiB frei, mindestens 2048 KiB'):
            deploy.check_space(gw,2048)
        for call in gw.ssh.call_args_list:
            self.assertNotIn('systemctl',call.args[0]);self.assertNotIn('mkdir',call.args[0])
    def test_invalid_or_untrusted_probe_output_is_not_echoed(self):
        for values in ([b'ARM-SECRET',b'0'],[b'mips',b'SECRET'],[b'mips',b'-1'],[b'mips',b'9'*13]):
            gw=Mock();gw.ssh.side_effect=values
            with self.assertRaises(deploy.InstallError) as exc:deploy.check_space(gw,2048)
            self.assertNotIn('SECRET',str(exc.exception))
    def test_sufficient_space(self):
        gw=Mock();gw.ssh.side_effect=[b'mips',b'2048']
        deploy.check_space(gw,2048)
    def test_failed_stage_and_explanation_survive_without_secret(self):
        with tempfile.TemporaryDirectory() as tmp,patch.object(storage,'ROOT',Path(tmp)):
            def failure(stop):
                deploy.step('upload');raise ConnectionError('SECRET_PASSWORD@HOST')
            output=io.StringIO()
            with patch.object(deploy,'_install',side_effect=failure),contextlib.redirect_stdout(output):
                with self.assertRaises(ConnectionError):deploy.install(Mock())
            result=storage.read('installation.json')
            self.assertEqual(result['step'],'upload');self.assertEqual(result['phase'],'error')
            self.assertIn('Gateway-Programm übertragen',result['message'])
            self.assertNotIn('SECRET',output.getvalue()+json.dumps(result))
    def test_success_is_explicit(self):
        with tempfile.TemporaryDirectory() as tmp,patch.object(storage,'ROOT',Path(tmp)),patch.object(deploy,'_install'):
            deploy.install(Mock());self.assertEqual(storage.read('installation.json')['phase'],'done')
    def test_low_space_cannot_stop_or_upload(self):
        with tempfile.TemporaryDirectory() as tmp,patch.object(storage,'ROOT',Path(tmp)):
            storage.write('migration.json',{'plan':[]})
            binary=Path(tmp)/'binary';binary.write_bytes(b'example')
            gw=Mock();gw.ssh.side_effect=[b'mips',b'0']
            stop=Mock()
            with patch.object(deploy,'gateway',return_value=({},gw)),patch.object(deploy,'runtime_config',return_value={}),patch.object(deploy.telemetry,'parse_snapshot',return_value=[]),patch.object(deploy.telemetry,'readings'),patch.object(deploy,'BINARY',binary),patch.object(deploy,'upload_binary') as upload:
                with contextlib.redirect_stdout(io.StringIO()),self.assertRaises(deploy.InstallError):deploy.install(stop)
            upload.assert_not_called();stop.assert_not_called();gw.stop_publisher.assert_not_called()
            self.assertIsNone(storage.read('gateway_deployment.json'))
            self.assertEqual(storage.read('installation.json')['step'],'gateway_space')
