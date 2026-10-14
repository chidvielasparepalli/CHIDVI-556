"""Fixed diagnostic presets execute real, bounded child processes."""
from pathlib import Path
import tempfile
import threading
import time
import unittest
import json
import io
import shutil
import venv
from unittest.mock import patch

from core.diagnostic_commands import CommandService, _ProcessTree
from core.repair_permissions import PermissionBroker


class DiagnosticCommandTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.events = []
        self.broker = PermissionBroker(self.root)
        self.service = CommandService(self.root, self.broker, emit=self.events.append)

    def tearDown(self):
        self.temp.cleanup()

    def write(self, relative, source):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(source, encoding='utf-8')

    def installation_environment(self):
        import os
        import sys
        import packaging
        venv.EnvBuilder(with_pip=False).create(self.root / '.venv')
        site = '.venv/Lib/site-packages' if os.name == 'nt' else f'.venv/lib/python{sys.version_info.major}.{sys.version_info.minor}/site-packages'
        shutil.copytree(Path(packaging.__file__).parent, self.root / site / 'packaging')
        self.write(site + '/pip/__init__.py', '')
        self.write(site + '/pip/__main__.py', "import json,pathlib,sys\npathlib.Path('pip-request.json').write_text(json.dumps(sys.argv[1:]))\nprint('local test installer invoked')\n")
        return site

    @staticmethod
    def package_metadata():
        return {'info': {'name': 'fixture-package', 'version': '1.2.3', 'license_expression': 'MIT',
                         'requires_python': '>=3.9'}, 'urls': [{
            'filename': 'fixture_package-1.2.3-py3-none-any.whl', 'packagetype': 'bdist_wheel',
            'url': 'https://files.pythonhosted.org/packages/fixture/fixture_package-1.2.3-py3-none-any.whl',
            'size': 12345, 'digests': {'sha256': 'a' * 64}, 'yanked': False, 'requires_python': '>=3.9'}]}

    @staticmethod
    def metadata_response(metadata):
        class Response(io.BytesIO):
            status = 200
            def geturl(self):
                return 'https://pypi.org/pypi/fixture-package/1.2.3/json'
        payload = metadata if isinstance(metadata, bytes) else json.dumps(metadata).encode()
        response = Response(payload)
        response.headers = {'Content-Type': 'application/json', 'Content-Length': str(len(payload))}
        return response

    def test_python_reports_real_version_and_visible_command(self):
        result = self.service.run({'kind': 'python_version'})
        self.assertTrue(result['ok'], result)
        self.assertIn('Python ', result['stdout'])
        self.assertEqual(result['exit_code'], 0)
        self.assertTrue(result['command'])
        self.assertTrue(any(e.get('type') == 'command_started' for e in self.events))

    def test_fast_windows_child_cannot_exit_before_containment(self):
        import os
        if os.name != 'nt':
            self.skipTest('Windows process creation race')
        original = _ProcessTree.attach
        def delayed_attach(tree, process, *args, **kwargs):
            time.sleep(.15)  # Simulate losing the scheduler immediately after CreateProcess.
            return original(tree, process, *args, **kwargs)
        with patch.object(_ProcessTree, 'attach', delayed_attach):
            for _ in range(12):
                result = self.service.run({'kind': 'python_version'})
                self.assertTrue(result['ok'], result)
                self.assertEqual(result['exit_code'], 0)

    def test_failed_windows_containment_never_executes_child_source(self):
        import os
        if os.name != 'nt':
            self.skipTest('Windows Job Object failure boundary')
        self.write('probe.py', "from pathlib import Path\nPath('executed-before-containment').write_text('unsafe')\n")
        original = _ProcessTree.attach
        processes = []
        def failed_attach(tree, process, *args, **kwargs):
            processes.append(process)
            time.sleep(.2)
            with patch.object(tree.api, 'AssignProcessToJobObject', return_value=0):
                return original(tree, process, *args, **kwargs)
        with patch.object(_ProcessTree, 'attach', failed_attach):
            result = self.service.run({'kind': 'import', 'module': 'probe'})
        self.assertFalse(result['ok'])
        self.assertFalse((self.root / 'executed-before-containment').exists())
        self.assertTrue(processes)
        self.assertTrue(all(process.poll() is not None for process in processes))
        self.assertTrue(all(process.stdout.closed and process.stderr.closed for process in processes))

    def test_windows_system_powershell_preset_returns_actual_machine_metadata(self):
        import os
        if os.name != 'nt':
            self.skipTest('Windows PowerShell preset')
        result = self.service.run({'kind': 'powershell_diagnostic', 'diagnostic': 'system'})
        self.assertTrue(result['ok'], result)
        metadata = json.loads(result['stdout'])
        self.assertIsInstance(metadata['Is64Bit'], bool)
        self.assertTrue(metadata['OS'])
        self.assertTrue(metadata['PowerShell'])

    def test_pinned_install_requires_desktop_opt_in_and_uses_only_project_venv(self):
        spec = {'kind': 'dependencies', 'operation': 'install', 'package': 'fixture-package==1.2.3'}
        with patch('urllib.request.OpenerDirector.open', side_effect=AssertionError('Unapproved metadata network request')):
            denied = self.service.run(spec)
        self.assertFalse(denied['ok'])
        self.assertTrue(denied.get('approval_required'))
        self.broker.set_preapproved('dependencies', True)
        absent = self.service.run(spec)
        self.assertFalse(absent['ok'])
        self.assertIn('venv', absent['error'].lower())
        site = self.installation_environment()
        metadata = self.package_metadata()
        with patch('urllib.request.OpenerDirector.open', return_value=self.metadata_response(metadata)):
            installed = self.service.run(spec)
        self.assertTrue(installed['ok'], installed)
        args = json.loads((self.root / 'pip-request.json').read_text())
        self.assertIn('--no-deps', args)
        self.assertIn('--no-index', args)
        self.assertEqual(Path(args[args.index('--target') + 1]), self.root / site)
        self.assertEqual(args[-1], metadata['urls'][0]['url'] + '#sha256=' + 'a' * 64)
        self.assertEqual(installed['dependency']['license'], 'MIT')
        self.assertEqual(installed['dependency']['download_size_bytes'], 12345)
        evidence = next(index for index, event in enumerate(self.events) if event.get('type') == 'dependency_preflight')
        invocation = next(index for index, event in enumerate(self.events) if event.get('type') == 'command_started' and ' install ' in event.get('command', ''))
        self.assertLess(evidence, invocation)
        with patch('urllib.request.OpenerDirector.open', side_effect=AssertionError('Successful install was retried')):
            self.assertFalse(self.service.run(spec)['ok'])
        for package in ('unversioned', '--target=outside', 'https://example.test/package.whl'):
            self.assertFalse(self.service.run({**spec, 'package': package})['ok'])

    def test_missing_unsafe_or_incompatible_package_metadata_blocks_installation(self):
        self.installation_environment()
        self.broker.set_preapproved('dependencies', True)
        spec = {'kind': 'dependencies', 'operation': 'install', 'package': 'fixture-package==1.2.3'}
        mutations = (
            lambda data: data['info'].update(license_expression=None),
            lambda data: data['info'].update(version='9.9.9'),
            lambda data: data['urls'][0].update(size=None),
            lambda data: data['urls'][0].update(digests={}),
            lambda data: data['urls'][0].update(yanked=True),
            lambda data: data['urls'][0].update(requires_python='>=99'),
            lambda data: data['urls'][0].update(filename='fixture_package-1.2.3-cp27-cp27m-manylinux1_x86_64.whl'),
            lambda data: data['urls'][0].update(url='https://example.test/package.whl'),
        )
        for mutate in mutations:
            self.service = CommandService(self.root, self.broker, emit=self.events.append)
            metadata = self.package_metadata()
            mutate(metadata)
            with self.subTest(metadata=metadata), patch('urllib.request.OpenerDirector.open', return_value=self.metadata_response(metadata)):
                result = self.service.run(spec)
                self.assertFalse(result['ok'], result)
                self.assertFalse((self.root / 'pip-request.json').exists(), 'Installer ran without valid evidence')

    def test_insufficient_disk_and_oversized_metadata_prevent_installation(self):
        self.installation_environment()
        self.broker.set_preapproved('dependencies', True)
        spec = {'kind': 'dependencies', 'operation': 'install', 'package': 'fixture-package==1.2.3'}
        from collections import namedtuple
        usage = namedtuple('usage', 'total used free')(100, 99, 1)
        with patch('urllib.request.OpenerDirector.open', return_value=self.metadata_response(self.package_metadata())), patch('shutil.disk_usage', return_value=usage):
            result = self.service.run(spec)
        self.assertFalse(result['ok'])
        self.assertIn('disk', result['error'].lower())
        self.service = CommandService(self.root, self.broker, emit=self.events.append)
        with patch('urllib.request.OpenerDirector.open', return_value=self.metadata_response(b'x' * (1024 * 1024 + 1))):
            result = self.service.run(spec)
        self.assertFalse(result['ok'])
        self.assertIn('metadata', result['error'].lower())
        self.assertFalse((self.root / 'pip-request.json').exists())

    def test_failed_metadata_attempt_cannot_repeat_for_same_pin_in_session(self):
        self.installation_environment()
        self.broker.set_preapproved('dependencies', True)
        spec = {'kind': 'dependencies', 'operation': 'install', 'package': 'fixture-package==1.2.3'}
        metadata = self.package_metadata()
        metadata['info']['license_expression'] = None
        with patch('urllib.request.OpenerDirector.open', return_value=self.metadata_response(metadata)):
            first = self.service.run(spec)
        self.assertFalse(first['ok'])
        with patch('urllib.request.OpenerDirector.open', side_effect=AssertionError('Repeated metadata network attempt')):
            again = self.service.run({**spec, 'package': 'Fixture_Package==1.2.3.0'})
        self.assertFalse(again['ok'])
        self.assertIn('already', again['error'].lower())
        self.assertFalse((self.root / 'pip-request.json').exists())

    def test_cancellation_after_preflight_does_not_start_installer(self):
        self.installation_environment()
        self.broker.set_preapproved('dependencies', True)
        cancel = threading.Event()
        def emit(event):
            self.events.append(event)
            if event.get('type') == 'dependency_preflight':
                cancel.set()
        self.service.emit = emit
        with patch('urllib.request.OpenerDirector.open', return_value=self.metadata_response(self.package_metadata())):
            result = self.service.run({'kind': 'dependencies', 'operation': 'install', 'package': 'fixture-package==1.2.3'}, cancel_event=cancel)
        self.assertTrue(result['cancelled'])
        self.assertFalse((self.root / 'pip-request.json').exists())

    def test_metadata_timeout_does_not_start_installer(self):
        self.installation_environment()
        self.broker.set_preapproved('dependencies', True)
        with patch('urllib.request.OpenerDirector.open', side_effect=TimeoutError('Metadata timeout fixture')):
            result = self.service.run({'kind': 'dependencies', 'operation': 'install', 'package': 'fixture-package==1.2.3'})
        self.assertFalse(result['ok'])
        self.assertTrue(result['timed_out'])
        self.assertFalse((self.root / 'pip-request.json').exists())

    def test_arbitrary_shell_unknown_fields_and_unapproved_install_are_rejected(self):
        for spec in ({'kind': 'powershell', 'script': 'whoami'}, {'kind': 'python_version', 'args': ['-c', 'print(1)']}, {'kind': 'dependencies', 'operation': 'install', 'package': 'anything'}, {'kind': 'import', 'module': 'os;exit()'}):
            with self.subTest(spec=spec):
                result = self.service.run(spec)
                self.assertFalse(result['ok'])
                self.assertIsNone(result['exit_code'])
        admin = self.service.run({'kind': 'administrator'})
        self.assertFalse(admin['ok'])
        self.assertTrue(admin['approval_required'])

    def test_compile_catches_syntax_error_without_running_source(self):
        self.write('broken.py', "raise RuntimeError('must not execute')\ndef broken(:\n pass\n")
        result = self.service.run({'kind': 'compile', 'paths': ['broken.py']})
        self.assertFalse(result['ok'])
        self.assertIn('SyntaxError', result['stderr'])
        self.write('broken.py', "raise RuntimeError('must not execute')\n")
        self.assertTrue(self.service.run({'kind': 'compile', 'paths': ['broken.py']})['ok'])

    def test_tool_contract_fails_before_handler_binding_and_passes_after(self):
        source = "TOOL={'name':'repair_fixture','description':'Repair fixture'}\ndef repair_fixture(parameters): return 'ok'\n"
        self.write('actions/fixture.py', source)
        before = self.service.run({'kind': 'tool_contract', 'path': 'actions/fixture.py'})
        self.assertFalse(before['ok'])
        self.assertIn('handler', before['stderr'])
        self.write('actions/fixture.py', source + "TOOL['handler']=repair_fixture\n")
        after = self.service.run({'kind': 'tool_contract', 'path': 'actions/fixture.py'})
        self.assertTrue(after['ok'], after)
        self.assertEqual(after['exit_code'], 0)

    def test_tool_contract_catches_invalid_schema_even_with_callable_handler(self):
        self.write('fixture.py', "def fixture(parameters): pass\nTOOL={'name':'fixture','description':'fixture','parameters': {'type':'ARRAY'},'handler':fixture}\n")
        result = self.service.run({'kind': 'tool_contract', 'path': 'fixture.py'})
        self.assertFalse(result['ok'])
        self.assertIn('parameters', result['stderr'])

    def test_known_credentials_are_redacted_from_result_and_command_events(self):
        self.write('config/api_keys.json', '{"gemini_api_key":"raw-sensitive-fixture-567890"}')
        self.broker = PermissionBroker(self.root)
        self.service = CommandService(self.root, self.broker, emit=self.events.append)
        self.write('probe.py', "print('raw-sensitive-fixture-567890')\nprint('password=another-sensitive-fixture')\n")
        result = self.service.run({'kind': 'import', 'module': 'probe'})
        self.assertTrue(result['ok'], result)
        self.assertNotIn('raw-sensitive-fixture-567890', str(result) + str(self.events))
        self.assertNotIn('another-sensitive-fixture', str(result) + str(self.events))
        self.assertIn('[REDACTED]', result['stdout'])

    def test_unittest_preset_reports_real_failing_assertion(self):
        self.write('tests/test_fixture.py', 'import unittest\nclass Case(unittest.TestCase):\n def test_failure(self): self.assertEqual(2, 3)\n')
        result = self.service.run({'kind': 'tests', 'start_dir': 'tests', 'pattern': 'test_fixture.py'})
        self.assertFalse(result['ok'])
        self.assertIn('2 != 3', result['stderr'])

    def test_import_captures_both_streams_and_actual_exit_status(self):
        self.write('probe.py', "import sys\nprint('stdout-marker')\nprint('stderr-marker', file=sys.stderr)\nraise SystemExit(7)\n")
        result = self.service.run({'kind': 'import', 'module': 'probe'})
        self.assertEqual(result['exit_code'], 7)
        self.assertIn('stdout-marker', result['stdout'])
        self.assertIn('stderr-marker', result['stderr'])

    def test_noisy_child_is_drained_but_retained_output_is_bounded(self):
        self.write('noisy.py', "import sys\nprint('x'*100000)\nprint('y'*100000, file=sys.stderr)\n")
        result = self.service.run({'kind': 'import', 'module': 'noisy', 'max_output': 1024})
        self.assertTrue(result['ok'], result)
        self.assertLessEqual(len(result['stdout']), 1100)
        self.assertLessEqual(len(result['stderr']), 1100)
        self.assertTrue(result['output_truncated'])

    def test_running_child_times_out_and_can_be_cancelled(self):
        self.write('slow.py', "import time\nprint('started', flush=True)\ntime.sleep(30)\n")
        timed = self.service.run({'kind': 'import', 'module': 'slow', 'timeout': .3})
        self.assertTrue(timed['timed_out'])
        self.assertFalse(timed['ok'])
        cancel = threading.Event()
        timer = threading.Timer(.3, cancel.set)
        timer.start()
        start = time.monotonic()
        try:
            cancelled = self.service.run({'kind': 'import', 'module': 'slow'}, cancel_event=cancel)
        finally:
            timer.cancel()
        self.assertTrue(cancelled['cancelled'])
        self.assertFalse(cancelled['timed_out'])
        self.assertLess(time.monotonic() - start, 5)

    def test_pre_cancelled_run_does_not_start_child(self):
        cancel = threading.Event()
        cancel.set()
        result = self.service.run({'kind': 'python_version'}, cancel_event=cancel)
        self.assertTrue(result['cancelled'])
        self.assertIsNone(result['exit_code'])
        self.assertFalse(any(e.get('type') == 'command_started' for e in self.events))

    def test_cancel_terminates_owned_descendant_not_only_python_launcher(self):
        self.write('family.py', "import pathlib,subprocess,sys,time\nchild=subprocess.Popen([sys.executable,'-c','import time; time.sleep(30)'])\npathlib.Path('child.pid').write_text(str(child.pid))\ntime.sleep(30)\n")
        cancel = threading.Event()
        timer = threading.Timer(1, cancel.set)
        timer.start()
        try:
            result = self.service.run({'kind': 'import', 'module': 'family'}, cancel_event=cancel)
        finally:
            timer.cancel()
        self.assertTrue(result['cancelled'])
        import psutil
        pid = int((self.root / 'child.pid').read_text())
        self.assertFalse(psutil.pid_exists(pid), 'Owned child survived cancellation')

    def test_failed_windows_handoff_retains_cleanup_ownership(self):
        import os
        import subprocess
        import sys
        if os.name != 'nt':
            self.skipTest('Windows Job Object failure path')
        tree = _ProcessTree()
        process = subprocess.Popen([sys.executable, '-c', 'import time;time.sleep(30)'],
                                   creationflags=subprocess.CREATE_NO_WINDOW)
        try:
            tree.attach(process)
            with patch.object(tree.api, 'SetInformationJobObject', return_value=0):
                with self.assertRaises(OSError):
                    tree.release()
            self.assertIsNone(process.poll())
            tree.close(process)
            process.wait(timeout=3)
            self.assertIsNotNone(process.returncode)
        finally:
            tree.close(process)
            if process.poll() is None:
                process.kill()
                process.wait(timeout=3)


if __name__ == '__main__':
    unittest.main()
