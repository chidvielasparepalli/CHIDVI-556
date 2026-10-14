"""Fixed, visible repair diagnostics; no model-provided shell or Python programs."""
from __future__ import annotations

import os
import json
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
import tempfile
import urllib.error
import urllib.parse
import urllib.request

from core.repair_permissions import redact


_CREATE_SUSPENDED = 0x00000004
_COMPILE = "import pathlib,sys; [(compile(pathlib.Path(p).read_bytes(),p,'exec'),print('Compiled '+p)) for p in sys.argv[1:]]"
_IMPORT = "import importlib,os,sys; sys.path.insert(0,os.getcwd()); importlib.import_module(sys.argv[1]); print('Imported '+sys.argv[1])"
_TESTS = "import os,sys,unittest; sys.path.insert(0,os.getcwd()); suite=unittest.defaultTestLoader.discover(sys.argv[1],pattern=sys.argv[2]); result=unittest.TextTestRunner(verbosity=2).run(suite); sys.exit(0 if result.wasSuccessful() else 1)"
_WHEEL_ENVIRONMENT = """import json,sys
try:
 from packaging.tags import sys_tags
except ImportError:
 from pip._vendor.packaging.tags import sys_tags
print(json.dumps({'python':'.'.join(map(str,sys.version_info[:3])),'tags':[str(tag) for tag in sys_tags()]}))
"""
_CONTRACT = """import importlib.util,os,re,sys
sys.path.insert(0,os.getcwd())
spec=importlib.util.spec_from_file_location('_chidvi_repair_contract',sys.argv[1])
module=importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
tool=getattr(module,'TOOL',None)
if not isinstance(tool,dict) or not isinstance(tool.get('name'),str) or not re.fullmatch(r'[a-zA-Z_][a-zA-Z0-9_]{0,63}',tool['name']):
 raise ValueError('TOOL must declare a valid identifier name')
if not isinstance(tool.get('description'),str) or not tool['description'].strip():
 raise ValueError('TOOL description must be nonempty')
parameters=tool.get('parameters',{'type':'OBJECT'})
if not isinstance(parameters,dict) or parameters.get('type')!='OBJECT':
 raise ValueError('TOOL parameters must be an OBJECT schema')
if not callable(tool.get('handler')):
 raise ValueError('TOOL handler must be callable')
print('TOOL contract valid: '+tool['name'])
"""
_POWERSHELL = {
    'processes': 'Get-Process | Select-Object Id,ProcessName | ConvertTo-Json -Compress',
    'ports': 'Get-NetTCPConnection | Select-Object LocalAddress,LocalPort,State,OwningProcess | ConvertTo-Json -Compress',
    'system': '[pscustomobject]@{OS=[Environment]::OSVersion.VersionString;Is64Bit=[Environment]::Is64BitOperatingSystem;PowerShell=$PSVersionTable.PSVersion.ToString()} | ConvertTo-Json -Compress',
}


class _NoMetadataRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise urllib.error.URLError('Package metadata redirects are prohibited.')


def _pypi_metadata(url, deadline, cancel_event):
    """One fixed public endpoint, no credentials/proxies/redirects, bounded JSON."""
    maximum = 1024 * 1024
    deadline = min(deadline, time.monotonic() + 5)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoMetadataRedirect())
    request = urllib.request.Request(url, headers={'Accept': 'application/json', 'User-Agent': 'CHIDVI-Repair/1.0'})
    if cancel_event is not None and cancel_event.is_set():
        raise InterruptedError('Dependency preflight cancelled.')
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError('Dependency metadata lookup timed out.')
    with opener.open(request, timeout=min(1, remaining)) as response:
        if (response.status != 200 or response.geturl() != url
                or response.headers.get('Content-Type', '').split(';')[0].strip().lower() != 'application/json'):
            raise ValueError('Dependency metadata must be a successful JSON response from the requested PyPI endpoint.')
        if int(response.headers.get('Content-Length', '0')) > maximum:
            raise ValueError('Dependency metadata exceeds the 1 MiB limit.')
        data = bytearray()
        while True:
            if cancel_event is not None and cancel_event.is_set():
                raise InterruptedError('Dependency preflight cancelled.')
            if time.monotonic() >= deadline:
                raise TimeoutError('Dependency metadata lookup timed out.')
            chunk = response.read1(min(16384, maximum + 1 - len(data)))
            if not chunk:
                break
            data.extend(chunk)
            if len(data) > maximum:
                raise ValueError('Dependency metadata exceeds the 1 MiB limit.')
    value = json.loads(data)
    if not isinstance(value, dict):
        raise ValueError('Dependency metadata is not a JSON object.')
    return value


def _declared_license(info):
    try:
        from packaging.licenses import canonicalize_license_expression
    except ImportError:
        try:
            from pip._vendor.packaging.licenses import canonicalize_license_expression
        except ImportError:
            canonicalize_license_expression = None
    for value in (info.get('license_expression'), info.get('license')):
        if isinstance(value, str) and 0 < len(value.strip()) <= 500 and 'licenseref' not in value.lower():
            if canonicalize_license_expression is not None:
                try:
                    return canonicalize_license_expression(value.strip())
                except ValueError:
                    pass
            if value.strip() in {'MIT', 'Apache-2.0', 'BSD-2-Clause', 'BSD-3-Clause', 'ISC', 'MPL-2.0', 'CC0-1.0', 'Unlicense'}:
                return value.strip()
    classifiers = info.get('classifiers', [])
    if isinstance(classifiers, list):
        for value in classifiers:
            if isinstance(value, str) and value.startswith('License :: OSI Approved :: ') and len(value) <= 250:
                return value
    raise ValueError('Dependency metadata has no recognized declared license; installation is blocked.')


class _ProcessTree:
    """A Windows job / POSIX process group contains only this invocation's children."""
    def __init__(self):
        self.job = None
        self._released = False
        if os.name == 'nt':
            import ctypes
            from ctypes import wintypes
            class Basic(ctypes.Structure):
                _fields_ = [('process_time', ctypes.c_longlong), ('job_time', ctypes.c_longlong),
                            ('flags', wintypes.DWORD), ('min_working', ctypes.c_size_t), ('max_working', ctypes.c_size_t),
                            ('active', wintypes.DWORD), ('affinity', ctypes.c_size_t), ('priority', wintypes.DWORD), ('scheduling', wintypes.DWORD)]
            class IO(ctypes.Structure):
                _fields_ = [(name, ctypes.c_ulonglong) for name in ('read_ops', 'write_ops', 'other_ops', 'read_bytes', 'write_bytes', 'other_bytes')]
            class Extended(ctypes.Structure):
                _fields_ = [('basic', Basic), ('io', IO), ('process_memory', ctypes.c_size_t), ('job_memory', ctypes.c_size_t),
                            ('peak_process', ctypes.c_size_t), ('peak_job', ctypes.c_size_t)]
            self._extended_type = Extended
            self.api = ctypes.WinDLL('kernel32', use_last_error=True)
            self.api.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
            self.api.CreateJobObjectW.restype = wintypes.HANDLE
            self.api.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
            self.api.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
            self.api.CloseHandle.argtypes = [wintypes.HANDLE]
            self.api.OpenThread.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
            self.api.OpenThread.restype = wintypes.HANDLE
            self.api.GetProcessIdOfThread.argtypes = [wintypes.HANDLE]
            self.api.GetProcessIdOfThread.restype = wintypes.DWORD
            self.api.ResumeThread.argtypes = [wintypes.HANDLE]
            self.api.ResumeThread.restype = wintypes.DWORD
            self.job = self.api.CreateJobObjectW(None, None)
            info = Extended()
            info.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            if not self.job or not self.api.SetInformationJobObject(self.job, 9, ctypes.byref(info), ctypes.sizeof(info)):
                self.close(None)
                raise OSError('Cannot create an owned diagnostic process job.')

    def attach(self, process, resume=False):
        if os.name != 'nt':
            return
        try:
            if not self.job or not self.api.AssignProcessToJobObject(self.job, int(process._handle)):
                raise OSError('Cannot contain the diagnostic child process.')
            if resume:
                import psutil
                threads = psutil.Process(process.pid).threads()
                if len(threads) != 1:
                    raise OSError('Cannot identify the suspended child primary thread safely.')
                handle = self.api.OpenThread(0x0802, False, threads[0].id)
                if not handle:
                    raise OSError('Cannot open the suspended child primary thread.')
                try:
                    if self.api.GetProcessIdOfThread(handle) != process.pid:
                        raise OSError('Suspended thread does not belong to the owned child.')
                    if self.api.ResumeThread(handle) != 1:
                        raise OSError('Cannot safely resume the contained child primary thread.')
                finally:
                    self.api.CloseHandle(handle)
        except Exception as exc:
            self.close(process)
            if process.poll() is None:
                process.kill()
            process.wait(timeout=5)
            if isinstance(exc, OSError):
                raise
            raise OSError('Cannot safely start the contained child process.') from exc

    def close(self, process):
        if self._released:
            return
        if self.job:
            self.api.CloseHandle(self.job)
            self.job = None
        elif os.name != 'nt' and process is not None:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass

    def release(self):
        """Detach a health-verified replacement; failed releases remain owned."""
        if self._released:
            return
        if self.job:
            import ctypes
            info = self._extended_type()
            if not self.api.SetInformationJobObject(self.job, 9, ctypes.byref(info), ctypes.sizeof(info)):
                raise OSError('Cannot clear the replacement job cleanup policy.')
            if not self.api.CloseHandle(self.job):
                info.basic.flags = 0x2000
                self.api.SetInformationJobObject(self.job, 9, ctypes.byref(info), ctypes.sizeof(info))
                raise OSError('Cannot release the replacement job handle.')
            self.job = None
        self._released = True


class CommandService:
    def __init__(self, workspace: Path, broker, emit=None):
        self.workspace = Path(workspace).resolve(strict=True)
        if self.workspace != broker.workspace:
            raise ValueError('Command service and permission broker must share a workspace.')
        self.broker, self.emit = broker, emit
        self._install_attempts = set()
        self._install_lock = threading.Lock()

    def _event(self, event):
        if self.emit:
            self.emit({key: redact(value) if isinstance(value, str) else value for key, value in event.items()})

    def _python(self):
        relative = '.venv/Scripts/python.exe' if os.name == 'nt' else '.venv/bin/python'
        candidate = self.broker.path(relative)
        return str(candidate if candidate.is_file() else Path(sys.executable).resolve(strict=True))

    def _path(self, value, *, suffix=None, directory=False):
        path = self.broker.path(value)
        self.broker.require('source_read', path)
        if not (path.is_dir() if directory else path.is_file()):
            raise ValueError('Diagnostic path does not exist or has the wrong file type.')
        if suffix and path.suffix.lower() != suffix:
            raise ValueError(f'Diagnostic requires a {suffix} file.')
        return str(path)

    def _dependency_preflight(self, package, target, deadline, cancel_event):
        try:
            from packaging.utils import canonicalize_name, parse_wheel_filename
            from packaging.version import Version
            from packaging.specifiers import SpecifierSet
        except ImportError:
            from pip._vendor.packaging.utils import canonicalize_name, parse_wheel_filename
            from pip._vendor.packaging.version import Version
            from pip._vendor.packaging.specifiers import SpecifierSet
        name, version_text = package.split('==')
        version = Version(version_text)
        name = canonicalize_name(name)
        with self._install_lock:
            pin = (name, version)
            if pin in self._install_attempts:
                raise ValueError('This package pin already had an installation attempt in this command-service session; investigate the result before starting a fresh session.')
            self._install_attempts.add(pin)
        remaining = deadline - time.monotonic()
        if remaining < .1:
            raise TimeoutError('Dependency preflight timed out.')
        environment = self.run({'kind': 'python_environment', 'timeout': min(10, remaining), 'max_output': 65536}, cancel_event)
        if environment.get('cancelled'):
            raise InterruptedError('Dependency preflight cancelled.')
        if environment.get('timed_out'):
            raise TimeoutError('Dependency interpreter inspection timed out.')
        if not environment['ok'] or environment['output_truncated']:
            raise ValueError('Cannot determine compatible wheel tags for the project virtual environment.')
        interpreter = json.loads(environment['stdout'])
        if not isinstance(interpreter, dict) or not isinstance(interpreter.get('tags'), list) or not interpreter['tags']:
            raise ValueError('Project interpreter compatibility metadata is incomplete.')
        tags = {tag: rank for rank, tag in enumerate(interpreter['tags']) if isinstance(tag, str)}
        python_version = Version(interpreter['python'])
        endpoint = 'https://pypi.org/pypi/' + urllib.parse.quote(name, safe='') + '/' + urllib.parse.quote(str(version), safe='') + '/json'
        document = _pypi_metadata(endpoint, deadline, cancel_event)
        info, urls = document.get('info'), document.get('urls')
        if not isinstance(info, dict) or not isinstance(urls, list) or not 1 <= len(urls) <= 1000:
            raise ValueError('Dependency metadata is missing release information or wheel files.')
        if canonicalize_name(str(info.get('name', ''))) != name or Version(str(info.get('version', ''))) != version:
            raise ValueError('Dependency metadata does not match the requested name and version.')
        license_name = _declared_license(info)
        candidates = []
        for file in urls:
            if not isinstance(file, dict) or file.get('packagetype') != 'bdist_wheel' or file.get('yanked') is not False:
                continue
            try:
                filename, url = file['filename'], file['url']
                if not isinstance(filename, str) or len(filename) > 255 or not isinstance(url, str):
                    continue
                wheel_name, wheel_version, _, wheel_tags = parse_wheel_filename(filename)
                compatible = [tags[str(tag)] for tag in wheel_tags if str(tag) in tags]
                if wheel_name != name or wheel_version != version or not compatible:
                    continue
                requirement = file.get('requires_python') or info.get('requires_python')
                if requirement is not None and (not isinstance(requirement, str) or python_version not in SpecifierSet(requirement)):
                    continue
                parsed = urllib.parse.urlsplit(url)
                if (parsed.scheme != 'https' or parsed.hostname != 'files.pythonhosted.org' or parsed.port not in (None, 443)
                        or parsed.username is not None or parsed.password is not None or parsed.query or parsed.fragment
                        or not parsed.path.startswith('/packages/') or urllib.parse.unquote(parsed.path.rsplit('/', 1)[-1]) != filename):
                    continue
                size, digest = file.get('size'), file.get('digests', {}).get('sha256')
                if type(size) is not int or not 0 < size <= 2 * 1024 ** 3 or not isinstance(digest, str) or not re.fullmatch(r'[0-9a-fA-F]{64}', digest):
                    continue
                candidates.append((min(compatible), size, filename, url, digest.lower()))
            except (ValueError, TypeError, KeyError, AttributeError):
                continue
        if not candidates:
            raise ValueError('No non-yanked compatible wheel has trusted source, size and SHA256 metadata; installation is blocked.')
        _, size, filename, url, digest = min(candidates)
        free = shutil.disk_usage(target).free
        temp_free = shutil.disk_usage(tempfile.gettempdir()).free
        required = size * 5 + 64 * 1024 * 1024
        if free < required or temp_free < required:
            raise ValueError(f'Insufficient disk space for dependency installation: estimated reserve {required} bytes, target free {free}, temporary free {temp_free}.')
        return {'package': f'{name}=={version}', 'metadata_source': endpoint, 'source': url,
                'filename': filename, 'license': license_name, 'sha256': digest,
                'download_size_bytes': size, 'target_python': str(python_version),
                'disk_free_bytes': free, 'temporary_disk_free_bytes': temp_free,
                'required_disk_estimate_bytes': required,
                'disk_estimate_basis': '5 times compressed wheel size plus 64 MiB; installed size is not supplied by PyPI.'}

    def _build(self, spec, *, cancel_event=None, deadline=None):
        kind = spec.get('kind', spec.get('preset'))
        fields = {'python_version': set(), 'python_environment': set(), 'dependencies': {'operation', 'package'}, 'compile': {'paths'},
                  'tests': {'start_dir', 'pattern'}, 'import': {'module'}, 'tool_contract': {'path'},
                  'powershell_diagnostic': {'diagnostic'}, 'health': set(), 'editor': {'paths'},
                  'administrator': set()}
        if kind not in fields or set(spec) - (fields[kind] | {'kind', 'preset', 'timeout', 'max_output'}):
            raise ValueError('Unknown diagnostic preset or unsupported fields; arbitrary commands are prohibited.')
        if 'kind' in spec and 'preset' in spec and spec['kind'] != spec['preset']:
            raise ValueError('Conflicting diagnostic preset names.')
        self.broker.require('tests' if kind == 'tests' else 'editor' if kind == 'editor' else 'diagnostics')
        python = [self._python(), '-I', '-B', '-u', '-X', 'utf8']
        if kind == 'python_version':
            return python + ['--version']
        if kind == 'python_environment':
            return python + ['-c', _WHEEL_ENVIRONMENT]
        if kind == 'dependencies':
            operation = spec.get('operation', 'check')
            if operation == 'install':
                self.broker.require('dependencies', reason='Inspect trusted package source, license, size, compatibility and disk evidence before a pinned project installation.')
                package = spec.get('package')
                if not isinstance(package, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,79}==[A-Za-z0-9][A-Za-z0-9.!+_-]{0,79}', package):
                    raise ValueError('Installation requires one exact name==version pin; URLs and pip options are prohibited.')
                interpreter = self.broker.path('.venv/Scripts/python.exe' if os.name == 'nt' else '.venv/bin/python')
                if not interpreter.is_file() or not self.broker.path('.venv/pyvenv.cfg').is_file():
                    raise ValueError('Installation requires an existing project .venv; system Python is never modified.')
                sites = [self.workspace / '.venv/Lib/site-packages'] if os.name == 'nt' else list((self.workspace / '.venv/lib').glob('python*/site-packages'))
                if len(sites) != 1 or not self.broker.path(sites[0]).is_dir():
                    raise ValueError('Cannot identify the project .venv site-packages directory.')
                target = str(self.broker.path(sites[0]))
                metadata = self._dependency_preflight(package, target, deadline, cancel_event)
                return {'dependency': metadata, 'command': python + ['-m', 'pip', '--isolated', '--disable-pip-version-check',
                        'install', '--no-input', '--no-deps', '--no-cache-dir', '--target', target, '--only-binary=:all:',
                        '--no-index', '--require-hashes', metadata['source'] + '#sha256=' + metadata['sha256']]}
            if operation not in {'list', 'check'}:
                raise ValueError('Dependency diagnostics support pip list/check only; no installation command is exposed.')
            if 'package' in spec:
                raise ValueError('A package argument is only valid for an approved installation.')
            return python + ['-m', 'pip', '--disable-pip-version-check', operation] + (['--format=json'] if operation == 'list' else [])
        if kind == 'compile':
            paths = spec.get('paths')
            if not isinstance(paths, list) or not 1 <= len(paths) <= 50:
                raise ValueError('Compile requires between 1 and 50 Python paths.')
            return python + ['-c', _COMPILE] + [self._path(path, suffix='.py') for path in paths]
        if kind == 'tests':
            start = self._path(spec.get('start_dir', 'tests'), directory=True)
            pattern = spec.get('pattern', 'test_*.py')
            if not isinstance(pattern, str) or not re.fullmatch(r'[\w*?.-]{1,100}\.py', pattern):
                raise ValueError('Test pattern must be a Python filename pattern, without directories.')
            return python + ['-c', _TESTS, start, pattern]
        if kind == 'import':
            module = spec.get('module')
            if not isinstance(module, str) or not re.fullmatch(r'[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*', module):
                raise ValueError('A dotted Python module name is required.')
            self.broker.path(module.replace('.', '/') + '.py')
            self.broker.path(module.replace('.', '/'))
            return python + ['-c', _IMPORT, module]
        if kind == 'tool_contract':
            return python + ['-c', _CONTRACT, self._path(spec.get('path'), suffix='.py')]
        if kind == 'powershell_diagnostic':
            diagnostic = spec.get('diagnostic', 'system')
            if os.name != 'nt' or diagnostic not in _POWERSHELL:
                raise ValueError('Choose a supported Windows diagnostic: processes, ports, or system.')
            executable = Path(os.environ.get('SystemRoot', r'C:\Windows')) / 'System32/WindowsPowerShell/v1.0/powershell.exe'
            return [str(executable), '-NoLogo', '-NoProfile', '-NonInteractive', '-Command', _POWERSHELL[diagnostic]]
        if kind == 'administrator':
            self.broker.require('administrator')
        if kind == 'editor':
            raise ValueError('Use the desktop Repair panel Open VS Code action; interactive editors are not bounded diagnostic children.')
        if kind == 'health':
            raise ValueError('Use the application health callback; no startup shell command is exposed.')
        raise ValueError('Unsupported diagnostic.')

    def run(self, spec: dict, cancel_event=None) -> dict:
        result = {'ok': False, 'stdout': '', 'stderr': '', 'exit_code': None, 'timed_out': False,
                  'cancelled': False, 'command': '', 'output_truncated': False}
        process = tree = None
        try:
            if not isinstance(spec, dict):
                raise ValueError('A structured diagnostic specification is required.')
            timeout, limit = spec.get('timeout', 30), spec.get('max_output', 16384)
            if type(timeout) not in {int, float} or not .1 <= timeout <= 300 or type(limit) is not int or not 256 <= limit <= 65536:
                raise ValueError('Timeout must be 0.1–300 seconds and output limit 256–65536 bytes per stream.')
            deadline = time.monotonic() + timeout
            if cancel_event is not None and cancel_event.is_set():
                result['cancelled'] = True
                return result
            if spec.get('kind', spec.get('preset')) == 'administrator':
                decision = self.broker.check('administrator')
                return {**result, 'error': decision['reason'], 'approval_required': True, 'class': 'C'}
            command = self._build(spec, cancel_event=cancel_event, deadline=deadline)
            if isinstance(command, dict):
                result['dependency'] = command['dependency']
                command = command['command']
                self._event({'type': 'dependency_preflight', 'dependency': result['dependency'],
                             'message': 'Dependency source, license, size, hash, target compatibility and disk preflight passed.'})
                self.broker.require('dependencies', reason='Install the exact metadata-verified wheel after preflight.')
            if cancel_event is not None and cancel_event.is_set():
                result['cancelled'] = True
                return result
            if time.monotonic() >= deadline:
                raise TimeoutError('Diagnostic time budget expired before process launch.')
            result['command'] = redact(subprocess.list2cmdline(command))
            self._event({'type': 'command_started', 'command': result['command']})
            environment = {key: value for key, value in os.environ.items()
                           if not re.search(r'api.?key|token|password|secret|credential|^PYTHON|^PIP_', key, re.I)}
            environment['PYTHONIOENCODING'] = 'utf-8'
            environment['PYTHONUNBUFFERED'] = '1'
            environment['PIP_CONFIG_FILE'] = os.devnull
            tree = _ProcessTree()
            process = subprocess.Popen(command, cwd=self.workspace, env=environment, stdin=subprocess.DEVNULL,
                                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, shell=False,
                                       creationflags=subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW | _CREATE_SUSPENDED if os.name == 'nt' else 0,
                                       start_new_session=os.name != 'nt')
            tree.attach(process, resume=os.name == 'nt')
            buffers = {'stdout': bytearray(), 'stderr': bytearray()}
            truncated = threading.Event()
            def drain(name, stream):
                try:
                    while True:
                        data = stream.read(4096)
                        if not data:
                            break
                        remaining = limit - len(buffers[name])
                        buffers[name].extend(data[:max(remaining, 0)])
                        if len(data) > remaining:
                            truncated.set()
                finally:
                    stream.close()
            readers = [threading.Thread(target=drain, args=(name, getattr(process, name)), daemon=True)
                       for name in buffers]
            for reader in readers:
                reader.start()
            while process.poll() is None:
                if cancel_event is not None and cancel_event.is_set():
                    result['cancelled'] = True
                    break
                if time.monotonic() >= deadline:
                    result['timed_out'] = True
                    break
                time.sleep(.025)
            tree.close(process)
            process.wait(timeout=5)
            for reader in readers:
                reader.join(timeout=2)
            for name, data in buffers.items():
                output = redact(data.decode('utf-8', errors='replace'))
                if len(output) > limit:
                    truncated.set()
                result[name] = output[:limit]
            result['exit_code'] = process.returncode
            result['output_truncated'] = truncated.is_set()
            result['ok'] = process.returncode == 0 and not result['cancelled'] and not result['timed_out']
        except (ValueError, TypeError, PermissionError, OSError, subprocess.SubprocessError) as exc:
            result['error'] = redact(str(exc))
            result['stderr'] = result['error']
            result['cancelled'] = isinstance(exc, InterruptedError)
            result['timed_out'] = isinstance(exc, TimeoutError)
            if getattr(exc, 'decision', None):
                result['approval_required'] = exc.decision['approval_required']
                result['class'] = exc.decision['class']
        finally:
            if tree:
                tree.close(process)
            if process is not None and process.poll() is None:
                process.kill()
                process.wait(timeout=5)
            if process is not None:
                for stream in (process.stdout, process.stderr):
                    if stream is not None and not stream.closed:
                        stream.close()
            self._event({'type': 'command_finished', **result})
        return result
