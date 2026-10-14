import json
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

import psutil

from core.repair_restart import RestartSupervisor, write_health
from core.diagnostic_commands import _ProcessTree


class RestartTests(unittest.TestCase):
    @staticmethod
    def cleanup_process(process):
        try:
            owned = process.children(recursive=True) + [process]
        except psutil.NoSuchProcess:
            return
        for child in reversed(owned):
            try:
                child.kill()
            except psutil.NoSuchProcess:
                pass
        psutil.wait_procs(owned, timeout=3)

    def test_healthy_replacement_requires_matching_token_and_live_process(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / 'main.py').write_text('''import argparse,json,os,time
from pathlib import Path
p=argparse.ArgumentParser()
p.add_argument('--health-file');p.add_argument('--health-token');p.add_argument('--diagnostic-mode',action='store_true')
a=p.parse_args()
for i in range(100):
 f=Path(a.health_file)
 f.write_text(json.dumps(dict(ready=True,pid=os.getpid(),token=a.health_token,heartbeat=time.time(),mode='diagnostic')))
 time.sleep(.13)
''', encoding='utf-8')
            supervisor = RestartSupervisor(root, diagnostic=True)
            try:
                result = supervisor.restart(threading.Event(), timeout=5)
                self.assertTrue(result['ok'], {**result, 'output': Path(result['log']).read_text() if 'log' in result else '',
                    'child_pid':supervisor.child.pid, 'parent_time':__import__('time').time(),
                    'health': [(str(p), p.read_text()) for p in (root/'logs'/'repair'/'runtime').glob('*.json')]})
                self.assertEqual(result['mode'], 'diagnostic')
                self.assertNotEqual(result['pid'], __import__('os').getpid())
                self.assertIsNone(supervisor.child.poll())
            finally:
                supervisor.stop_candidate()

    def test_crashed_replacement_is_not_healthy(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / 'main.py').write_text('raise SystemExit(7)', encoding='utf-8')
            supervisor = RestartSupervisor(root)
            result = supervisor.restart(threading.Event(), timeout=3)
            self.assertFalse(result['ok'])
            self.assertEqual(result['exit_code'], 7)

    def test_cancelled_restart_does_not_launch(self):
        with tempfile.TemporaryDirectory() as folder:
            stop = threading.Event(); stop.set()
            supervisor = RestartSupervisor(Path(folder))
            self.assertFalse(supervisor.restart(stop)['ok'])
            self.assertIsNone(supervisor.child)

    def test_health_path_cannot_escape_runtime_directory(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            with self.assertRaises(ValueError):
                write_health(root, root/'outside.json', 'token', 'diagnostic', True)
            path = root/'logs'/'repair'/'runtime'/'health.json'
            write_health(root, path, 'token', 'diagnostic', True)
            self.assertTrue(json.loads(path.read_text())['ready'])

    def test_crashed_launcher_stops_its_owned_descendants(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root/'main.py').write_text('''import subprocess,sys,time
from pathlib import Path
child = subprocess.Popen([sys.executable, '-c', 'import time;time.sleep(30)'])
Path('descendant.pid').write_text(str(child.pid))
time.sleep(.35)
raise SystemExit(7)
''', encoding='utf-8')
            supervisor = RestartSupervisor(root)
            descendant = None
            try:
                result = supervisor.restart(threading.Event(), timeout=5)
                self.assertFalse(result['ok'])
                self.assertEqual(result['exit_code'], 7)
                pid = int((root/'descendant.pid').read_text())
                try:
                    descendant = psutil.Process(pid)
                    deadline = time.monotonic() + 2
                    while descendant.is_running() and time.monotonic() < deadline:
                        time.sleep(.03)
                    self.assertFalse(descendant.is_running(), 'A failed replacement must not leave an owned descendant running')
                except psutil.NoSuchProcess:
                    pass
            finally:
                supervisor.stop_candidate()
                if descendant is not None:
                    self.cleanup_process(descendant)

    def test_committed_handoff_releases_healthy_parent_and_descendant(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root/'main.py').write_text('''import argparse,json,os,subprocess,sys,time
from pathlib import Path
p=argparse.ArgumentParser()
p.add_argument('--health-file');p.add_argument('--health-token')
a=p.parse_args()
child=subprocess.Popen([sys.executable, '-c', 'import time;time.sleep(30)'])
Path('descendant.pid').write_text(str(child.pid))
for i in range(100):
 Path(a.health_file).write_text(json.dumps(dict(ready=True,pid=os.getpid(),token=a.health_token,heartbeat=time.time(),mode='normal')))
 time.sleep(.13)
''', encoding='utf-8')
            supervisor = RestartSupervisor(root)
            owned = []
            launched = None
            try:
                result = supervisor.restart(threading.Event(), timeout=5)
                self.assertTrue(result['ok'], result)
                launched = supervisor.child
                parent = psutil.Process(supervisor.child.pid)
                descendant = psutil.Process(int((root/'descendant.pid').read_text()))
                owned = [parent, descendant]
                commit = getattr(supervisor, 'commit_handoff', None)
                self.assertTrue(callable(commit), 'Health-verified replacement needs an explicit ownership release')
                self.assertTrue(commit()['ok'])
                self.assertFalse(supervisor.handoff_pending)
                supervisor.stop_candidate()
                time.sleep(.2)
                self.assertTrue(parent.is_running(), 'Releasing the old supervisor must preserve the replacement')
                self.assertTrue(descendant.is_running(), 'Released replacement descendants must survive handoff')
            finally:
                supervisor.stop_candidate()
                for process in reversed(owned):
                    self.cleanup_process(process)
                if launched is not None:
                    launched.wait(timeout=3)

    def test_handoff_cannot_be_committed_before_health_verification(self):
        with tempfile.TemporaryDirectory() as folder:
            supervisor = RestartSupervisor(Path(folder))
            commit = getattr(supervisor, 'commit_handoff', None)
            self.assertTrue(callable(commit))
            self.assertFalse(commit()['ok'])

    def test_stale_or_cancelled_handoff_retains_owned_cleanup(self):
        for state in ('stale', 'cancelled'):
            with self.subTest(state=state), tempfile.TemporaryDirectory() as folder:
                root = Path(folder)
                (root/'main.py').write_text('''import argparse,json,os,time
from pathlib import Path
p=argparse.ArgumentParser()
p.add_argument('--health-file');p.add_argument('--health-token')
a=p.parse_args()
for i in range(2):
 Path(a.health_file).write_text(json.dumps(dict(ready=True,pid=os.getpid(),token=a.health_token,heartbeat=time.time(),mode='normal')))
 time.sleep(.2)
time.sleep(30)
''', encoding='utf-8')
                supervisor = RestartSupervisor(root)
                stop = threading.Event()
                process = None
                try:
                    result = supervisor.restart(stop, timeout=5)
                    self.assertTrue(result['ok'], result)
                    process = psutil.Process(supervisor.child.pid)
                    if state == 'cancelled':
                        stop.set()
                    else:
                        path = Path(result['health_file'])
                        record = json.loads(path.read_text())
                        record['heartbeat'] = 0
                        path.write_text(json.dumps(record))
                    self.assertFalse(supervisor.commit_handoff()['ok'])
                    supervisor.stop_candidate()
                    self.assertFalse(process.is_running(), 'An uncommitted replacement remains owned and must stop')
                finally:
                    supervisor.stop_candidate()
                    if process is not None:
                        self.cleanup_process(process)

    @unittest.skipUnless(os.name == 'nt', 'Windows suspended launch containment')
    def test_replacement_does_not_execute_before_job_assignment(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            marker = root/'executed.txt'
            (root/'main.py').write_text("from pathlib import Path\nPath('executed.txt').write_text('ran')\nraise SystemExit(7)\n")
            supervisor = RestartSupervisor(root)
            original = _ProcessTree.attach
            observed = []
            def delayed_attach(tree, process, **kwargs):
                time.sleep(.2)  # Model a scheduler delay after Popen returns.
                observed.append(marker.exists())
                return original(tree, process, **kwargs)
            try:
                with patch.object(_ProcessTree, 'attach', delayed_attach):
                    result = supervisor.restart(threading.Event(), timeout=3)
                self.assertEqual(observed, [False], 'Replacement must remain suspended until its job owns all descendants')
                self.assertEqual(result['exit_code'], 7)
                self.assertTrue(marker.is_file(), 'Contained child should run after it has been safely resumed')
            finally:
                supervisor.stop_candidate()


if __name__ == '__main__':
    unittest.main()
