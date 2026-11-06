#!/usr/bin/env python3
"""Transport for the existing OCI wizard. No installer or lifecycle here.

The worker calls cli.install_template exactly once. The engine's existing
builder, installer dispatch, records and completion callbacks remain owners.
This module can be loaded by Monitor without importing the OCI package.
"""
from contextlib import contextmanager, redirect_stdout, redirect_stderr
import fcntl
import hashlib
import importlib
import json
import os
from pathlib import Path
import re
import socket
import stat
import subprocess
import sys
import tempfile
import time
import traceback
import uuid

ROOT = Path('/var/lib/proxmenux/oci/web-wizard')
ENGINE = Path(__file__).resolve().parents[1]
ACTIVE = {'starting', 'configuring', 'question', 'installing', 'finishing'}
FINAL = {'completed', 'cancelled', 'expired', 'failed', 'recovery_required'}
ID = re.compile(r'[a-f0-9]{32}')
APP = re.compile(r'[a-z0-9][a-z0-9._-]{0,127}')
LIMIT = 256 * 1024
INPUT_LIMIT = 2 * 1024 * 1024
ANSWER_LIMIT = 6 * INPUT_LIMIT + LIMIT


class Error(RuntimeError):
    def __init__(self, code, status=409):
        self.code, self.status = code, status
        super().__init__(code)


def encode(value):
    return (json.dumps(value, ensure_ascii=True, separators=(',', ':')) + '\n').encode()


def screen(value):
    return re.sub(r'\\Z.', '', re.sub(r'\x1b\[[0-9;?]*[ -/]*[@-~]', '', str(value)))


def private(path, uid, directory=False):
    info = path.lstat()
    if (info.st_uid != uid or stat.S_ISLNK(info.st_mode)
            or info.st_mode & 0o077 or
            not (stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode))):
        raise Error('unsafe_store', 503)


def read(path, uid, limit=None):
    limit = LIMIT if limit is None else limit
    private(path, uid)
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, 'rb') as source:
        raw = source.read(limit + 1)
    if len(raw) > limit:
        raise Error('invalid_state', 503)
    try:
        return json.loads(raw)
    except (ValueError, UnicodeError):
        raise Error('invalid_state', 503) from None


def write(path, value, limit=None):
    limit = LIMIT if limit is None else limit
    raw = encode(value)
    if len(raw) > limit:
        raise Error('state_too_large', 503)
    fd, name = tempfile.mkstemp(prefix='.write-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as target:
            target.write(raw)
            target.flush()
            os.fsync(target.fileno())
        os.replace(name, path)
        directory = os.open(path.parent, os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def run_command(args):
    return subprocess.run(args, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                          stderr=subprocess.DEVNULL, check=True, timeout=15)


class Store:
    def __init__(self, root=ROOT, *, uid=0, runner=run_command, engine=ENGINE):
        self.root, self.uid, self.run, self.engine = Path(root), uid, runner, Path(engine)

    def ready(self, create=False):
        if os.geteuid() != self.uid:
            raise Error('engine_unavailable', 503)
        if create:
            self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        private(self.root, self.uid, True)

    def folder(self, job_id):
        if not isinstance(job_id, str) or not ID.fullmatch(job_id):
            raise Error('invalid_request', 400)
        path = self.root / job_id
        try:
            private(path, self.uid, True)
        except FileNotFoundError:
            raise Error('not_found', 404) from None
        return path

    @contextmanager
    def lock(self, path):
        fd = os.open(path / '.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            if os.fstat(fd).st_uid != self.uid or os.fstat(fd).st_mode & 0o077:
                raise Error('unsafe_store', 503)
            # HTTP answer writes and the worker's next poll can overlap.
            # Brief contention is normal, not an engine failure. Bound the
            # wait so an abandoned/blocked holder cannot hang the API.
            deadline = time.monotonic() + 1
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise Error('busy') from None
                    time.sleep(0.01)
            yield
        finally:
            os.close(fd)

    def owned(self, owner, job_id):
        self.ready()
        path = self.folder(job_id)
        value = read(path / 'state.json', self.uid)
        if value.get('owner') != owner:
            raise Error('not_found', 404)
        return path, value

    def change(self, job_id, **updates):
        path = self.folder(job_id)
        with self.lock(path):
            value = read(path / 'state.json', self.uid)
            value.update(updates, updated_at=int(time.time()))
            write(path / 'state.json', value)
        return value

    def current(self, owner, app_id, *, operation='catalog'):
        if not isinstance(app_id, str) or not APP.fullmatch(app_id):
            raise Error('invalid_request', 400)
        if not self.root.exists():
            return None
        self.ready()
        key = hashlib.sha256((owner + '\0' + ('custom\0' if operation == 'custom' else '') + app_id).encode()).hexdigest()
        index = self.root / ('current-' + key + '.json')
        try:
            job_id = read(index, self.uid)['id']
        except FileNotFoundError:
            return None
        return self.get(owner, job_id)

    def current_custom(self, owner):
        return self.current(owner, 'custom-image', operation='custom')

    def custom_job(self, owner, job_id):
        value = self.get(owner, job_id)
        if value.get('operation') != 'custom':
            raise Error('not_found', 404)
        return value

    def start_custom(self, owner, body):
        if not isinstance(body, dict) or set(body) != {'request_id', 'language', 'node'}:
            raise Error('invalid_request', 400)
        return self.start(owner, dict(body, app_id='custom-image', mode='default'), operation='custom')

    def start(self, owner, body, *, operation='catalog'):
        if (not isinstance(body, dict) or set(body) != {'app_id', 'request_id', 'mode', 'language', 'node'}
                or not isinstance(body['app_id'], str) or not APP.fullmatch(body['app_id'])
                or not isinstance(body['request_id'], str) or not ID.fullmatch(body['request_id'])
                or body['mode'] not in ('default', 'advanced') or body['language'] not in ('en', 'sk')
                or body['node'] != socket.gethostname()):
            raise Error('invalid_request', 400)
        self.ready(create=True)
        if operation not in ('catalog', 'custom') or (operation == 'custom' and body['app_id'] != 'custom-image'):
            raise Error('invalid_request', 400)
        key = hashlib.sha256((owner + '\0' + ('custom\0' if operation == 'custom' else '') + body['app_id']).encode()).hexdigest()
        job_id = hashlib.sha256((owner + '\0' + body['request_id']).encode()).hexdigest()[:32]
        fingerprint = hashlib.sha256(encode(body if operation == 'catalog' else dict(body, operation=operation))).hexdigest()
        with self.lock(self.root):
            path = self.root / job_id
            if path.exists():
                _, old = self.owned(owner, job_id)
                if old.get('request_hash') != fingerprint:
                    raise Error('request_changed')
                return self.public(old)
            current = self.current(owner, body['app_id'], operation=operation)
            if current and current['status'] in ACTIVE | {'recovery_required'}:
                return current
            path.mkdir(mode=0o700)
            value = dict(body, id=job_id, owner=owner, request_hash=fingerprint,
                         status='starting', question=None, result=None, error=None,
                         created_at=int(time.time()), updated_at=int(time.time()))
            if operation == 'custom':
                value['operation'] = operation
            write(path / 'state.json', value)
            write(self.root / ('current-' + key + '.json'), {'id': job_id})
            try:
                self.run(['/usr/bin/systemd-run', '--quiet', '--no-block', '--collect',
                          '--service-type=exec', '--unit=proxmenux-oci-web-' + job_id,
                          # The native Proxmox archive reader needs the same
                          # file modes as a Menu/CLI install. Transport files
                          # use explicit 0600/0700, independently of umask.
                          '--property=UMask=0022', '--property=LimitCORE=0',
                          '--property=KillMode=process', '--property=StandardInput=null',
                          '--property=StandardOutput=null', '--property=StandardError=null',
                          '/usr/bin/python3', '-I', '-B', str(self.engine / 'remote/oci_web_wizard.py'),
                          '--worker', job_id])
            except (OSError, subprocess.SubprocessError):
                value = self.change(job_id, status='recovery_required', error='worker_launch_uncertain')
        return self.public(value)

    @staticmethod
    def public(value):
        keys = ('id', 'app_id', 'mode', 'language', 'node', 'status', 'question',
                'result', 'error', 'created_at', 'updated_at')
        # Observation metadata, never persisted: a browser clock can differ
        # from this host, which owns question expiry and final confirmation.
        return {**{key: value.get(key) for key in keys}, 'server_time': time.time(),
                'operation': value.get('operation', 'catalog')}

    def get(self, owner, job_id):
        _, value = self.owned(owner, job_id)
        if (value['status'] == 'installing' and value.get('execution_deadline') is not None
                and time.time() >= value['execution_deadline']):
            # A late child may still finish. Never kill/replay it or declare its
            # resources safe to remove merely because the observer timed out.
            return self.public(dict(value, status='recovery_required', question=None, error='operation_timeout'))
        if value['status'] in ACTIVE and time.time() - value['updated_at'] > 15:
            try:
                self.run(['/usr/bin/systemctl', 'is-active', '--quiet', 'proxmenux-oci-web-' + job_id + '.service'])
            except subprocess.CalledProcessError as error:
                if error.returncode in (3, 4):
                    value = dict(value, status='recovery_required', question=None, error='worker_interrupted')
            except (OSError, subprocess.SubprocessError):
                value = dict(value, question=None, error='worker_status_unknown')
        return self.public(value)

    def credentials(self, owner, job_id):
        path, value = self.owned(owner, job_id)
        if value['status'] != 'completed':
            raise Error('result_missing')
        try:
            return read(path / 'credentials.json', self.uid)
        except FileNotFoundError:
            return []

    def answer(self, owner, job_id, body):
        path, _ = self.owned(owner, job_id)
        if (not isinstance(body, dict) or set(body) not in ({'question_id', 'value'}, {'cancel'})
                or (set(body) == {'cancel'} and body['cancel'] is not True)):
            raise Error('invalid_request', 400)
        with self.lock(path):
            value = read(path / 'state.json', self.uid)
            if value['status'] != 'question' or not value.get('question'):
                raise Error('question_changed')
            question = value['question']
            if question['expires_at'] <= time.time():
                raise Error('question_expired')
            if (path / 'answer.json').exists():
                raise Error('answer_pending')
            if 'cancel' not in body:
                if body['question_id'] != question['id']:
                    raise Error('question_changed')
                validate_answer(question, body['value'])
            write(path / 'answer.json', body, ANSWER_LIMIT if question['kind'] == 'multiline' else LIMIT)
        return self.public(value)


def validate_answer(question, value):
    kind = question['kind']
    if value == {'back': True}:
        if not question.get('back'):
            raise Error('invalid_answer', 400)
        return
    if kind in ('confirm', 'review', 'message'):
        valid = type(value) is bool
    elif kind in ('ask', 'password'):
        valid = isinstance(value, str) and len(value) <= 4096 and '\0' not in value
        if valid and question.get('required'):
            valid = bool(value) if kind == 'password' else bool(value.strip() or question.get('default'))
    elif kind == 'multiline':
        try:
            valid = (isinstance(value, str) and '\0' not in value and
                     len(value.encode('utf-8')) <= min(question.get('limit_bytes', INPUT_LIMIT), INPUT_LIMIT))
        except (UnicodeError, TypeError):
            valid = False
    elif kind in ('choose', 'detail_menu'):
        valid = value is None or (isinstance(value, str) and value in {v[0] for v in question['options']})
    elif kind == 'checklist':
        valid = (isinstance(value, list) and len(value) == len(set(v for v in value if isinstance(v, str)))
                 and all(isinstance(v, str) and v in {v[0] for v in question['options']} for v in value))
    else:
        valid = False
    if not valid:
        raise Error('invalid_answer', 400)


class ConfigurationBoundary:
    """Worker-local audit guard until the existing final review is accepted.

    Only known read-only host commands are allowed. Unknown capabilities fail
    closed; a new installer path is never used to work around a blocked probe.
    """
    def __init__(self, folder, *, registry_inspection=False):
        self.folder, self.executing = Path(folder).resolve(), False
        self.registry_inspection = registry_inspection

    def audit(self, event, args):
        if self.executing:
            return
        if event == 'subprocess.Popen':
            argv = args[1]
            allowed = isinstance(argv, (list, tuple)) and bool(argv)
            name = Path(argv[0]).name if allowed else ''
            tail = list(argv[1:]) if allowed else []
            allowed = (
                (name == 'pvesh' and tail[:1] == ['get'] and len(tail) >= 2
                 and tail[1] in ('/nodes/localhost/storage', '/nodes/localhost/network'))
                or (name == 'timedatectl' and tail == ['show', '-p', 'Timezone', '--value'])
                or (name == 'getfacl' and len(tail) == 2 and tail[0] == '-cpn')
                or (name == 'nvidia-smi' and tail == ['-L'])
                or (name == 'lspci' and tail == ['-nn'])
                or (name == 'lsusb' and not tail)
                or (name == 'ping' and len(tail) == 7 and tail[:5] == ['-c', '1', '-W', '1', '-I'])
                or (name == 'ip' and len(tail) == 6 and tail[:3] == ['-4', 'neigh', 'show'] and tail[4] == 'dev')
            ) if allowed else False
            if name == 'skopeo' and self.registry_inspection:
                from proxmenux_oci.registry_auth import validate_image, RegistryError
                try:
                    reference = tail[-1] if tail else ''
                    valid = (len(tail) in (2, 3) and tail[0] == 'inspect'
                             and (len(tail) == 2 or tail[1] in ('--raw', '--config'))
                             and isinstance(reference, str) and reference.startswith('docker://'))
                    if valid:
                        validate_image(reference[len('docker://'):])
                    allowed = valid
                except (RegistryError, ValueError, TypeError):
                    allowed = False
            if not allowed:
                raise Error('configuration_effect_blocked')
        elif event in ('socket.connect', 'socket.getaddrinfo', 'os.system', 'os.posix_spawn', 'os.fork'):
            raise Error('configuration_effect_blocked')
        elif event == 'open':
            path, mode, flags = args
            writing = bool(flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND))
            if writing and not isinstance(path, int):
                self.writable(path)
        elif event in ('os.mkdir', 'os.remove', 'os.rmdir', 'os.chmod', 'os.chown', 'os.symlink', 'os.link'):
            self.writable(args[0])
        elif event == 'os.rename':
            self.writable(args[0])
            self.writable(args[1])

    def writable(self, path):
        try:
            if Path(path).resolve().is_relative_to(self.folder):
                return
        except (TypeError, ValueError):
            pass
        raise Error('configuration_effect_blocked')


def revision(engine):
    digest = hashlib.sha256()
    for folder in ('src', 'remote', 'catalog', 'schemas'):
        for path in sorted((engine / folder).rglob('*')):
            if path.suffix in ('.py', '.sh', '.json') and '__pycache__' not in path.parts:
                digest.update(str(path.relative_to(engine)).encode() + b'\0' + path.read_bytes())
    return digest.hexdigest()


class WebUI:
    back_enabled = False

    def __init__(self, store, job_id, boundary, revalidate, *, clock=time.time, sleep=time.sleep, custom=False):
        self.store, self.job_id, self.boundary, self.revalidate = store, job_id, boundary, revalidate
        self.clock, self.sleep = clock, sleep
        self.definition_input, self.allow_registry_sign_in = custom, not custom

    def question(self, kind, text, *, default=None, options=(), required=False, title=None, execution=False, limit=None):
        from proxmenux_oci.ui import UserCancelled, BackRequested
        # Post-install optional image cleanup is not part of creation approval.
        # Keep the existing engine default (no removal) without a second action.
        if self.boundary.executing:
            if kind == 'confirm':
                return False
            if kind == 'message':
                return True
            raise Error('unexpected_completion_question')
        self.revalidate()
        question = dict(id=uuid.uuid4().hex, kind=kind, text=screen(text)[:48000], title=screen(title) if title else None,
                        default=None if kind in ('password', 'multiline') else default,
                        options=[[v[0], screen(v[1])] for v in options if v[0]], required=required,
                        back=self.back_enabled, expires_at=int(self.clock()) + (120 if kind == 'review' else 900),
                        execution=execution)
        if kind == 'multiline':
            question['limit_bytes'] = min(limit or INPUT_LIMIT, INPUT_LIMIT)
        self.store.change(self.job_id, status='question', question=question)
        path = self.store.folder(self.job_id)
        while self.clock() < question['expires_at']:
            reply = None
            with self.store.lock(path):
                if (path / 'answer.json').exists():
                    reply = read(path / 'answer.json', self.store.uid, ANSWER_LIMIT if kind == 'multiline' else LIMIT)
                    (path / 'answer.json').unlink()
            if reply is None:
                self.sleep(0.25)
                continue
            if reply.get('cancel'):
                raise UserCancelled('cancelled')
            if reply.get('question_id') != question['id']:
                raise Error('question_changed')
            value = reply['value']
            validate_answer(question, value)
            if value == {'back': True}:
                raise BackRequested()
            self.revalidate(final=execution and kind == 'review' and value is True)
            if execution and kind == 'review' and value is True:
                self.store.change(self.job_id, status='installing', question=None,
                                  execution_deadline=int(self.clock()) + 4 * 3600)
                self.boundary.executing = True
            else:
                self.store.change(self.job_id, status='configuring', question=None)
            return value
        raise Error('question_expired')

    def ask(self, text, default=None, required=True):
        value = self.question('ask', text, default=default, required=required).strip()
        value = value or (str(default) if default is not None else '')
        if required and not value:
            raise Error('invalid_answer', 400)
        return value

    def password(self, text, required=True):
        value = self.question('password', text, required=required)
        if required and not value:
            raise Error('invalid_answer', 400)
        return value

    def confirm(self, text, default=False, **kwargs):
        return self.question('confirm', text, default=default, title=kwargs.get('title'))

    def choose(self, text, options, default=None, **kwargs):
        return self.question('choose', text, options=options, default=default, title=kwargs.get('title'))

    def checklist(self, text, options, defaults, **kwargs):
        return self.question('checklist', text, options=options, default=list(defaults), title=kwargs.get('title'))

    def detail_menu(self, text, options, default=None, **kwargs):
        return self.question('detail_menu', text, options=options, default=default, title=kwargs.get('title'))

    def review(self, text, title=None, question=None, default=True):
        return self.question('review', text, title=title, default=False)

    def installation_review(self, text, title=None, question=None, default=True):
        return self.question('review', text, title=title, default=False, execution=True)

    def multiline(self, text, *, limit=INPUT_LIMIT):
        if not self.definition_input:
            raise Error('unsupported_question')
        return self.question('multiline', text, limit=limit)

    def definition_file(self, text, *, limit=INPUT_LIMIT, required=True):
        value = self.multiline(text, limit=limit)
        if required and not value.strip():
            raise Error('invalid_answer', 400)
        return value

    def message(self, text, title=None):
        self.question('message', text, title=title)

    def info(self, text):
        self.message(text)


def public_result(value):
    if not isinstance(value, dict):
        raise Error('result_missing')
    vmid = value.get('vmid')
    members = value.get('stack_vmids') or {}
    if ((vmid is not None and (type(vmid) is not int or vmid < 100))
            or not isinstance(members, dict) or not all(isinstance(k, str) and type(v) is int and v >= 100 for k, v in members.items())
            or not (vmid is not None or members)):
        raise Error('result_invalid')
    urls = value.get('urls') or []
    return {'vmid': vmid, 'stack_vmids': members,
            'urls': [dict(label=str(v.get('label', 'Web UI'))[:200], url=v['url']) for v in urls
                     if isinstance(v, dict) and isinstance(v.get('url'), str)
                     and re.fullmatch(r'https?://[^\s/@]+(?::\d+)?(?:/[^\s]*)?', v['url'])],
            'credentials_available': bool(value.get('credentials'))}


def install_host_ready():
    """The common host availability guard for both authenticated web adapters."""
    from proxmenux_oci import host
    if not host.storages('rootdir') or not host.storages('vztmpl') or not host.bridges():
        raise Error('host_metadata_unavailable')


def worker(store, job_id, *, load=None, install_hook=sys.addaudithook):
    store.ready()
    path = store.folder(job_id)
    initial = read(path / 'state.json', store.uid)
    if initial['status'] != 'starting':
        raise Error('worker_already_started')
    claim = os.open(path / '.worker-claim', os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
    os.close(claim)
    custom_operation = initial.get('operation') == 'custom'
    boundary = ConfigurationBoundary(path, registry_inspection=custom_operation)
    try:
        # Install before OCI imports: importing a missing catalog must not sync.
        sys.dont_write_bytecode = True
        install_hook(boundary.audit)
        store.change(job_id, status='configuring')
        if load is None:
            sys.path.insert(0, str(store.engine / 'src'))
            i18n = importlib.import_module('proxmenux_oci.i18n')
            i18n._language, i18n._cache = initial['language'], None
            cli = importlib.import_module('proxmenux_oci.cli')
            catalog = cli.Catalog(store.engine, read_only=True)
        else:
            cli, catalog = load()
        template = None if custom_operation else catalog.compose(initial['app_id'])
        if template is not None and not template.get('compatibility', {}).get('automatic_install_candidate'):
            raise Error('application_blocked')
        expected_revision = revision(store.engine)
        expected_template = hashlib.sha256(encode(template)).hexdigest()

        def host_ready():
            # Use the engine's existing host providers. Empty/failed metadata
            # cannot silently become an installable default in the web flow.
            install_host_ready()

        host_ready()

        def revalidate(final=False):
            if (initial['node'] != socket.gethostname() or revision(store.engine) != expected_revision or
                    (not custom_operation and hashlib.sha256(encode(catalog.compose(initial['app_id']))).hexdigest() != expected_template)):
                raise Error('source_changed')
            if final:
                host_ready()

        ui = WebUI(store, job_id, boundary, revalidate, custom=custom_operation)
        notice = None if custom_operation else cli.external_services_notice(template)
        if notice and not ui.confirm(notice, False):
            store.change(job_id, status='cancelled', question=None)
            return
        if custom_operation:
            from proxmenux_oci.custom import install_custom
            result = install_custom(ui)
        else:
            result = cli.install_template(ui, template, initial['app_id'], initial['mode'])
        if not boundary.executing:
            store.change(job_id, status='cancelled', question=None)
            return
        public = public_result(result)
        if result.get('credentials'):
            write(path / 'credentials.json', result['credentials'])
        store.change(job_id, status='completed', question=None, result=public)
    except BaseException as error:
        cancelled = type(error).__name__ == 'UserCancelled'
        expired = isinstance(error, Error) and error.code == 'question_expired'
        code = error.code if isinstance(error, Error) else ('cancelled' if cancelled else 'engine_failed')
        # Private diagnostics contain locations, never exception messages,
        # locals, question answers, command lines or credentials.
        write(path / 'failure.json', {'type': type(error).__name__, 'frames': [
            [Path(frame.filename).name, frame.lineno, frame.name]
            for frame in traceback.extract_tb(error.__traceback__)]})
        store.change(job_id, status=('recovery_required' if boundary.executing else
                                    'cancelled' if cancelled else 'expired' if expired else 'failed'),
                     question=None, error=code)
    finally:
        reply = path / 'answer.json'
        if reply.exists():
            reply.unlink()


if __name__ == '__main__':
    if len(sys.argv) != 3 or sys.argv[1] != '--worker' or not ID.fullmatch(sys.argv[2]):
        raise SystemExit(2)
    # No terminal output, prompts or credentials are copied to a web log.
    with open(os.devnull, 'w') as sink, redirect_stdout(sink), redirect_stderr(sink):
        worker(Store(), sys.argv[2])
