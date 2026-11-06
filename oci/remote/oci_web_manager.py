#!/usr/bin/env python3
"""Authenticated transport for fixed native OCI actions; no lifecycle implementation."""
from contextlib import redirect_stdout, redirect_stderr
import hashlib
import importlib
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time
import traceback

# Python -I deliberately omits the script directory. The sibling transport
# belongs to this root-owned engine, not to site-packages or the caller's cwd.
_remote_directory = str(Path(__file__).resolve().parent)
if _remote_directory not in sys.path:
    sys.path.insert(0, _remote_directory)
import oci_web_wizard as transport

ENGINE = Path(__file__).resolve().parents[1]
ROOT = Path('/var/lib/proxmenux/oci/web-manager')
Error = transport.Error


def adapter(engine=ENGINE):
    source = str(Path(engine) / 'src')
    if source not in sys.path:
        sys.path.insert(0, source)
    return importlib.import_module('proxmenux_oci.web_manager')


class Store(transport.Store):
    def __init__(self, root=ROOT, *, uid=0, runner=transport.run_command, engine=ENGINE):
        super().__init__(root, uid=uid, runner=runner, engine=engine)

    def start_operation(self, owner, body):
        if (not isinstance(body, dict) or set(body) != {'operation', 'parameters', 'request_id', 'language', 'node'}
                or not isinstance(body['request_id'], str) or not transport.ID.fullmatch(body['request_id'])
                or body['language'] not in ('en', 'sk') or body['node'] != socket.gethostname()):
            raise transport.Error('invalid_request', 400)
        try:
            adapter(self.engine).validate(body['operation'], body['parameters'])
        except (ValueError, TypeError):
            raise transport.Error('invalid_request', 400) from None
        self.ready(create=True)
        job_id = hashlib.sha256((owner + '\0manager\0' + body['request_id']).encode()).hexdigest()[:32]
        fingerprint = hashlib.sha256(transport.encode(body)).hexdigest()
        with self.lock(self.root):
            path = self.root / job_id
            if path.exists():
                _, old = self.owned(owner, job_id)
                if old.get('request_hash') != fingerprint:
                    raise transport.Error('request_changed')
                return self.public(old)
            path.mkdir(mode=0o700)
            value = dict(body, id=job_id, app_id='manager', mode='default', owner=owner,
                request_hash=fingerprint, status='starting', question=None, result=None, error=None,
                created_at=int(time.time()), updated_at=int(time.time()))
            transport.write(path / 'state.json', value)
            try:
                self.run(['/usr/bin/systemd-run', '--quiet', '--no-block', '--collect', '--service-type=exec',
                    '--unit=proxmenux-oci-web-' + job_id, '--property=UMask=0022', '--property=LimitCORE=0',
                    '--property=KillMode=process', '--property=StandardInput=null',
                    '--property=StandardOutput=null', '--property=StandardError=null',
                    '/usr/bin/python3', '-I', '-B', str(self.engine / 'remote/oci_web_manager.py'), '--worker', job_id])
            except (OSError, subprocess.SubprocessError):
                value = self.change(job_id, status='recovery_required', error='worker_launch_uncertain')
        return self.public(value)

    @staticmethod
    def public(value):
        return {**transport.Store.public(value), 'parameters': value['parameters'], 'progress': value.get('progress')}

    def jobs(self, owner, *, operations=None):
        if not self.root.exists():
            return []
        self.ready()
        values = []
        for index, path in enumerate(self.root.iterdir()):
            if index >= 4096:
                raise transport.Error('history_too_large', 503)
            if not transport.ID.fullmatch(path.name):
                continue
            if len(values) >= 1000:
                raise transport.Error('history_too_large', 503)
            try:
                if operations is not None:
                    _, state = self.owned(owner, path.name)
                    if state.get('operation') not in operations:
                        continue
                values.append(self.get(owner, path.name))
            except transport.Error as error:
                if error.code != 'not_found':
                    raise
        return sorted(values, key=lambda value: value['created_at'], reverse=True)


class Boundary(transport.ConfigurationBoundary):
    """No lifecycle before a native review. Native lock metadata is the sole exception."""
    def __init__(self, folder, project):
        super().__init__(folder, registry_inspection=True)
        self.project = Path(project)
        self.definition_network = False
        self.effects = False
        self.denied = False

    def audit(self, event, args):
        try:
            self._audit(event, args)
        except transport.Error:
            self.denied = True
            raise

    def _audit(self, event, args):
        if not self.executing and event == 'subprocess.Popen':
            argv = args[1]
            if isinstance(argv, (list, tuple)) and argv:
                name, tail = Path(argv[0]).name, list(argv[1:])
                if ((name == 'pct' and len(tail) == 2 and tail[0] == 'config' and str(tail[1]).isdigit())
                        or (name == 'pvesh' and tail[:2] == ['get', '/cluster/resources'])
                        or (name.startswith('python') and len(tail) >= 3 and
                            tail[:2] == [str(self.project / 'remote/oci_image_cache.py'), 'list']
                            and all(str(v).isdigit() for v in tail[2:]))
                        or (name.startswith('python') and len(tail) == 2 and
                            tail == [str(self.project / 'remote/oci_restore_recovery.py'), 'plan'])):
                    return
        if self.definition_network and event in ('socket.connect', 'socket.getaddrinfo'):
            return
        if not self.executing:
            # The existing registry lock normalises its own modes before taking
            # flock. It may never write a contract, guest configuration or key.
            native_root = Path('/usr/local/share/proxmenux/oci/instances')
            try:
                target = Path(args[0]).resolve() if event in ('open', 'os.mkdir', 'os.chmod') and not isinstance(args[0], int) else None
                if target == native_root and native_root.is_dir() and event in ('os.mkdir', 'os.chmod'):
                    if event != 'os.chmod' or args[1] == 0o700:
                        return
                if target == native_root / '.lock' and event in ('open', 'os.chmod'):
                    if event != 'os.chmod' or args[1] == 0o600:
                        return
            except (TypeError, ValueError):
                pass
        elif event == 'subprocess.Popen':
            self.effects = True
        elif self.executing and event in ('os.remove', 'os.rename', 'os.mkdir'):
            paths = args[:2] if event == 'os.rename' else args[:1]
            if any(not Path(path).resolve().is_relative_to(self.folder) for path in paths):
                self.effects = True
        elif self.executing and event == 'open':
            path, _, flags = args
            if flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND):
                if not isinstance(path, int) and not Path(path).resolve().is_relative_to(self.folder):
                    self.effects = True
        super().audit(event, args)


class UI(transport.WebUI):
    requires_management_review = True
    defer_image_cleanup = True
    all_definition_sources = True

    def __init__(self, store, job_id, boundary, revalidate, operation, *, clock=time.time, sleep=time.sleep):
        super().__init__(store, job_id, boundary, revalidate, clock=clock, sleep=sleep, custom=True)
        self.allow_registry_sign_in = False
        self.operation = operation
        self.initial_choice = None
        self.initial_selection = None
        self.notices = []

    def question(self, kind, text, *, default=None, options=(), required=False, title=None, execution=False, limit=None, show_tags=False):
        from proxmenux_oci.ui import UserCancelled, BackRequested
        self.revalidate()
        question = dict(id=transport.uuid.uuid4().hex, kind=kind, text=transport.screen(text)[:48000],
            title=transport.screen(title) if title else None, default=None if kind in ('password', 'multiline') else default,
            options=[[v[0], transport.screen(v[1])] for v in options if v[0]], required=required,
            back=self.back_enabled, expires_at=int(self.clock()) + (120 if execution else 900), execution=execution)
        if kind == 'multiline':
            question['limit_bytes'] = min(limit or transport.INPUT_LIMIT, transport.INPUT_LIMIT)
        if show_tags:
            question['show_tags'] = True
        self.store.change(self.job_id, status='question', question=question)
        path = self.store.folder(self.job_id)
        while self.clock() < question['expires_at']:
            reply = None
            with self.store.lock(path):
                if (path / 'answer.json').exists():
                    reply = transport.read(path / 'answer.json', self.store.uid, transport.ANSWER_LIMIT)
                    (path / 'answer.json').unlink()
            if reply is None:
                self.sleep(.25)
                continue
            if reply.get('cancel'):
                raise UserCancelled()
            if reply.get('question_id') != question['id']:
                raise transport.Error('question_changed')
            transport.validate_answer(question, reply['value'])
            if reply['value'] == {'back': True}:
                raise BackRequested()
            self.revalidate(final=execution and reply['value'] is True)
            if execution and reply['value'] is True:
                self.boundary.executing = True
            self.store.change(self.job_id, status='installing' if self.boundary.executing else 'configuring', question=None,
                              execution_deadline=int(self.clock()) + 4 * 3600)
            return reply['value']
        raise transport.Error('question_expired')

    def installation_review(self, text, title=None, **kwargs):
        return self.effect_review(text)

    def effect_review(self, text):
        return self.question('review', text, default=False, execution=True)

    def prepare_management(self, vmid):
        # Carry/recovery verification belongs to the native menu and can mount
        # a stopped CT. Never bypass it or do it silently on opening a page.
        from proxmenux_oci.i18n import translate
        return self.effect_review(translate('Check the selected LXC and refresh its recovery record before continuing? A stopped LXC may be mounted for this check. Replacing or deleting the application requires another confirmation.') + f'\n\nLXC {vmid}')

    def review(self, text, title=None, **kwargs):
        return self.question('review', text, title=title, default=False,
                             execution=self.operation not in ('catalog.install', 'custom.install', 'registry.install'))

    def confirm(self, text, default=False, **kwargs):
        execution = self.operation not in ('catalog.install', 'custom.install', 'registry.install')
        return self.question('confirm', text, title=kwargs.get('title'),
                             default=False if execution else default, execution=execution)

    def choose(self, text, options, default=None, **kwargs):
        if self.operation == 'custom.install' and {v[0] for v in options} == {'paste', 'file', 'url', 'run', 'image', 'oci-compose-paste', 'oci-compose', 'oci-compose-url'}:
            from proxmenux_oci.i18n import translate
            labels = {'paste': translate('Paste Compose text'), 'file': translate('Compose file on this server'),
                'url': translate('Compose from a web address'), 'run': translate('Paste a docker run command'),
                'image': translate('Image reference'), 'oci-compose-paste': translate('Paste oci-compose text'),
                'oci-compose': translate('oci-compose file on this server'),
                'oci-compose-url': translate('oci-compose from a web address')}
            options = [(key, labels[key]) for key, _ in options]
        if self.initial_choice and self.initial_choice in {v[0] for v in options}:
            value, self.initial_choice = self.initial_choice, None
            return value
        from proxmenux_oci.ui import ReadableTags
        return self.question('choose', text, options=options, default=default,
                             title=kwargs.get('title'), show_tags=isinstance(options, ReadableTags))

    def checklist(self, text, options, defaults, **kwargs):
        if self.initial_selection is not None:
            value, self.initial_selection = self.initial_selection, None
            if not set(value) <= {v[0] for v in options}:
                raise transport.Error('selection_changed')
            return value
        return super().checklist(text, options, defaults, **kwargs)

    def definition_url(self, url, limit=None):
        from proxmenux_oci.custom import _read_url, MAX_COMPOSE_BYTES
        if not self.question('review', str(url), default=False, execution=False):
            from proxmenux_oci.ui import UserCancelled
            raise UserCancelled()
        self.boundary.definition_network = True
        try:
            return _read_url(url, limit or MAX_COMPOSE_BYTES)
        finally:
            self.boundary.definition_network = False

    def info(self, text):
        self.store.change(self.job_id, progress=transport.screen(text)[:300])

    def message(self, text, title=None):
        self.notices = [*self.notices[-9:], transport.screen(text)[:6000]]
        return super().message(text, title)


def worker(store, job_id, *, dispatch=None, install_hook=sys.addaudithook):
    store.ready()
    path = store.folder(job_id)
    initial = transport.read(path / 'state.json', store.uid)
    if initial['status'] != 'starting':
        raise transport.Error('worker_already_started')
    claim = os.open(path / '.worker-claim', os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
    os.close(claim)
    boundary = Boundary(path, store.engine)
    try:
        sys.dont_write_bytecode = True
        install_hook(boundary.audit)
        expected = transport.revision(store.engine)
        source = adapter(store.engine)
        from proxmenux_oci import i18n
        i18n._language, i18n._cache = initial['language'], None
        def revalidate(final=False):
            if initial['node'] != socket.gethostname() or transport.revision(store.engine) != expected:
                raise transport.Error('source_changed')
            if final and initial['operation'] in ('catalog.install', 'custom.install', 'registry.install'):
                transport.install_host_ready()
        revalidate()
        if initial['operation'] in ('catalog.install', 'custom.install', 'registry.install'):
            transport.install_host_ready()
        ui = UI(store, job_id, boundary, revalidate, initial['operation'])
        store.change(job_id, status='configuring')
        result = (dispatch or source.run_operation)(store.engine, ui, initial['operation'], initial['parameters'])
        if boundary.denied:
            raise transport.Error('configuration_effect_blocked')
        if not result:
            store.change(job_id, status='recovery_required' if boundary.effects else 'cancelled', question=None)
            return
        public = {'operation': initial['operation'], 'notices': ui.notices}
        if initial['operation'] == 'rclone.mount' and isinstance(result, dict):
            public['notices'] = [*ui.notices, *[transport.screen(str(result[key]))[:2000]
                for key in ('remote', 'read_write_path', 'read_only_path') if result.get(key)]]
        if isinstance(result, dict) and (result.get('vmid') or result.get('stack_vmids')):
            public['installation'] = transport.public_result(result)
            if result.get('credentials'):
                transport.write(path / 'credentials.json', result['credentials'])
        store.change(job_id, status='completed', question=None, result=public)
    except BaseException as error:
        cancelled = type(error).__name__ == 'UserCancelled'
        expired = isinstance(error, transport.Error) and error.code == 'question_expired'
        transport.write(path / 'failure.json', {'type': type(error).__name__, 'frames': [
            [Path(f.filename).name, f.lineno, f.name] for f in traceback.extract_tb(error.__traceback__)]})
        store.change(job_id, status='recovery_required' if boundary.effects else
            'cancelled' if cancelled else 'expired' if expired else 'failed', question=None,
            error=error.code if isinstance(error, transport.Error) else 'engine_failed')
    finally:
        reply = path / 'answer.json'
        if reply.exists():
            reply.unlink()


if __name__ == '__main__':
    if len(sys.argv) != 3 or sys.argv[1] != '--worker' or not transport.ID.fullmatch(sys.argv[2]):
        raise SystemExit(2)
    with open(os.devnull, 'w') as sink, redirect_stdout(sink), redirect_stderr(sink):
        worker(Store(), sys.argv[2])
