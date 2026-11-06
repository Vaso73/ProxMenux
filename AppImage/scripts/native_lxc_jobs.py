"""Owner-bound checks and a durable single-create PVE task controller.

The default PVE transport is read-only. Tests inject an inert API; neither
the request body nor a Monitor environment variable can open the real gate.
No delete, cleanup, OCI registration or automatic create retry is provided.
"""
import copy
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import secrets
import sqlite3
import stat
import threading
import time
from contextlib import nullcontext
from native_lxc_policy import PolicyDenied

from native_lxc import NativeError, HAReadinessError, digest, plan
from native_lxc_pve import PveAPI, read_context
from pve_task_status import task_status_type

TTL = 120
_SIGNING_KEY = secrets.token_bytes(32)
_INSTANCE = secrets.token_hex(16)
_ACTIVE = set()
_ACTIVE_LOCK = threading.RLock()
FINAL = {'succeeded', 'failed_before_effect', 'partial', 'unknown', 'needs_recovery'}


def process_start(pid):
    try:
        # /proc stat's command name can contain spaces or parentheses.
        return Path('/proc/'+str(pid)+'/stat').read_text().rpartition(')')[2].split()[19]
    except (OSError, IndexError):
        return None


def _identifier(value, field='job_id'):
    if type(value) is not str or not re.fullmatch(r'[a-f0-9]{32}', value):
        raise NativeError('invalid_field', field=field)
    return value


class Store:
    def __init__(self, path):
        self.path = Path(path)

    def _connect(self, write=False):
        if self.path.is_symlink() or any(p.is_symlink() for p in self.path.parents):
            raise NativeError('journal_unavailable', 503)
        if write:
            self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            fd = os.open(self.path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
            os.close(fd)
        elif not self.path.exists():
            return None
        info = self.path.stat()
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
            raise NativeError('journal_unavailable', 503)
        connection = sqlite3.connect(str(self.path) if write else 'file:'+str(self.path)+'?mode=ro',
                                     uri=not write, timeout=5)
        connection.row_factory = sqlite3.Row
        if write:
            connection.execute('PRAGMA synchronous=FULL')
            connection.execute('''CREATE TABLE IF NOT EXISTS jobs (
                id TEXT PRIMARY KEY, owner TEXT NOT NULL, intent TEXT NOT NULL,
                request_hash TEXT NOT NULL, node TEXT NOT NULL, vmid INTEGER NOT NULL,
                status TEXT NOT NULL, payload TEXT NOT NULL, created REAL NOT NULL,
                UNIQUE(owner,intent))''')
            connection.execute('CREATE UNIQUE INDEX IF NOT EXISTS guest_reservation ON jobs(node,vmid)')
            connection.commit()
        return connection

    def lookup(self, owner, intent):
        conn = self._connect()
        if conn is None:
            return None
        try:
            return conn.execute('SELECT * FROM jobs WHERE owner=? AND intent=?', (owner, intent)).fetchone()
        finally:
            conn.close()

    def reserved_ids(self, node):
        conn = self._connect()
        if conn is None:
            return []
        try:
            return [row[0] for row in conn.execute('SELECT vmid FROM jobs WHERE node=?', (node,)).fetchall()]
        finally:
            conn.close()

    def admit(self, owner, intent, request_hash, value, now):
        conn = self._connect(True)
        try:
            conn.execute('BEGIN IMMEDIATE')
            old = conn.execute('SELECT * FROM jobs WHERE owner=? AND intent=?', (owner, intent)).fetchone()
            if old:
                if old['request_hash'] != request_hash:
                    raise NativeError('idempotency_conflict', 409)
                conn.rollback()
                return json.loads(old['payload']), False
            job = {'id': secrets.token_hex(16), 'node': value['node'], 'vmid': value['vmid'],
                   'intent': intent, 'status': 'queued', 'phase': 'queued', 'upid': None, 'error': None,
                   'plan': value, 'created_at': now, 'updated_at': now, 'result': None,
                   'worker_instance': _INSTANCE, 'worker_pid': os.getpid(), 'worker_start': process_start(os.getpid())}
            try:
                conn.execute('INSERT INTO jobs VALUES(?,?,?,?,?,?,?,?,?)',
                             (job['id'], owner, intent, request_hash, value['node'], value['vmid'],
                              job['status'], json.dumps(job), now))
            except sqlite3.IntegrityError:
                raise NativeError('vmid_reserved', 409) from None
            conn.commit()
            return job, True
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def put(self, job):
        conn = self._connect(True)
        try:
            with conn:
                changed = conn.execute('UPDATE jobs SET status=?,payload=? WHERE id=?',
                                       (job['status'], json.dumps(job), job['id'])).rowcount
                if changed != 1:
                    raise NativeError('journal_unavailable', 503)
        finally:
            conn.close()

    def get(self, owner, job_id):
        _identifier(job_id)
        conn = self._connect()
        if conn is None:
            raise NativeError('job_not_found', 404)
        try:
            row = conn.execute('SELECT payload FROM jobs WHERE owner=? AND id=?', (owner, job_id)).fetchone()
            if row is None:
                raise NativeError('job_not_found', 404)
            return json.loads(row['payload'])
        finally:
            conn.close()

    def jobs(self, owner):
        conn = self._connect()
        if conn is None:
            return []
        try:
            rows = conn.execute('SELECT payload FROM jobs WHERE owner=? ORDER BY created DESC LIMIT 100', (owner,)).fetchall()
            return [json.loads(row['payload']) for row in rows]
        finally:
            conn.close()


def _upid(raw, node, vmid, kind):
    if type(raw) is not str or len(raw) > 256:
        raise NativeError('task_identity_unknown', 503)
    parts = raw.split(':')
    kinds = kind if isinstance(kind, tuple) else (kind,)
    if (len(parts) != 9 or parts[0] != 'UPID' or parts[1] != node or parts[5] not in kinds
            or parts[6] != str(vmid) or parts[-1] != '' or not re.fullmatch(r'[A-Za-z0-9_@.-]+', parts[7])
            or not re.fullmatch(r'[0-9A-Fa-f]{8}', parts[2])
            or not re.fullmatch(r'[0-9A-Fa-f]{8,9}', parts[3])
            or not re.fullmatch(r'[0-9A-Fa-f]{8}', parts[4])):
        raise NativeError('task_identity_unknown', 503)
    return raw


def _matches_config(actual, expected):
    if type(actual) is not dict:
        return False
    for key, value in expected.items():
        if key == 'vmid':
            continue
        found = actual.get(key)
        if key in ('ostemplate', 'ha-managed', 'pool', 'start'):
            # PVE does not retain the archive identity in CT config. The
            # immutable journal and create UPID retain that association.
            continue
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            try:
                if float(found) == value:
                    continue
            except (ValueError, TypeError):
                pass
            return False
        if key in ('rootfs',) or re.fullmatch(r'mp\d+', key):
            if type(found) is not str:
                return False
            want = value.split(','); got = found.split(',')
            # PVE replaces STORAGE:SIZE with the actual allocated volid.
            if ':' in want[0] and not want[0].startswith('/'):
                if not got[0].startswith(want[0].split(':')[0]+':'):
                    return False
                size = next((v.split('=', 1)[1] for v in got if v.startswith('size=')), None)
                units = {'K': 1024, 'M': 1024**2, 'G': 1024**3, 'T': 1024**4}
                match = re.fullmatch(r'([0-9]+(?:\.[0-9]+)?)([KMGT])', size or '')
                if not match or float(match[1])*units[match[2]] != float(want[0].split(':', 1)[1])*1024**3:
                    return False
            elif got[0] != want[0]:
                return False
            options = dict(part.split('=', 1) for part in got[1:] if '=' in part)
            for part in want[1:]:
                k, expected = part.split('=', 1)
                default = {'replicate': '1', 'ro': '0', 'backup': '0'}.get(k)
                if options.get(k, default) != expected:
                    return False
            continue
        if key in ('net0', 'features', 'startup'):
            if found is None and key == 'features':
                found = ''
            if type(found) is not str:
                return False
            expected_parts = dict(x.split('=', 1) for x in value.split(','))
            actual_parts = dict(x.split('=', 1) for x in found.split(',') if '=' in x)
            if any(actual_parts.get(k, {'link_down': '0', 'nesting': '0', 'ip': 'manual', 'ip6': 'manual'}.get(k)) != v for k, v in expected_parts.items()):
                return False
            continue
        if found != value:
            return False
    return True


class Service:
    def __init__(self, api=None, store=None, context_provider=None, clock=time.time,
                 monotonic=time.monotonic, sleep=time.sleep, dispatch=None):
        self.api = api or PveAPI()
        self.store = store or Store('/var/lib/proxmenux/create-native-lxc/jobs.sqlite3')
        self.context_provider = context_provider or (lambda: read_context(self.api))
        self.clock, self.monotonic, self.sleep = clock, monotonic, sleep
        self.dispatch = dispatch or self._thread
        self.active = _ACTIVE
        self.lock = _ACTIVE_LOCK

    @staticmethod
    def _thread(function):
        threading.Thread(target=function, daemon=True, name='native-lxc').start()

    @property
    def can_execute(self):
        return self.api.mutations_enabled is True

    def context(self):
        value = self.context_provider()
        # Never allow a fixture or stale context to open the transport gate.
        value = copy.deepcopy(value)
        age = self.clock() - value['observed_at']
        if age < 0 or age > 30:
            raise NativeError('context_stale', 409)
        value['can_execute'] = self.can_execute and value.get('can_execute', False)
        return value

    def check(self, owner, draft):
        context = self.context()
        if self.clock() - context['observed_at'] < 0 or self.clock() - context['observed_at'] > 30:
            raise NativeError('context_stale', 409)
        value, normalized = plan(draft, context)
        if hasattr(self.api, 'validate_plan'):
            self.api.validate_plan(normalized, context)
        expires = self.clock() + TTL
        body = {'owner': owner, 'plan_hash': value['plan_hash'], 'expires_at': expires}
        signature = hmac.new(_SIGNING_KEY, json.dumps(body, sort_keys=True).encode(), hashlib.sha256).hexdigest()
        return {'plan': value, 'context': context, 'check': {**body, 'signature': signature},
                'expires_at': expires, 'server_time': self.clock(), 'can_execute': context['can_execute'] and self.can_execute}

    def _verify(self, owner, check, value):
        if type(check) is not dict or set(check) != {'owner', 'plan_hash', 'expires_at', 'signature'}:
            raise NativeError('check_invalid', 409)
        signature = check['signature']
        body = {k: check[k] for k in ('owner', 'plan_hash', 'expires_at')}
        expected = hmac.new(_SIGNING_KEY, json.dumps(body, sort_keys=True).encode(), hashlib.sha256).hexdigest()
        if type(signature) is not str or not hmac.compare_digest(signature, expected) or check['owner'] != owner or check['plan_hash'] != value['plan_hash']:
            raise NativeError('check_invalid', 409)
        if type(check['expires_at']) not in (int, float) or self.clock() >= check['expires_at']:
            raise NativeError('check_expired', 409)

    def submit(self, owner, body):
        if not self.can_execute:
            raise NativeError('execution_disabled', 503)
        if type(body) is not dict or set(body) != {'draft', 'check', 'intent', 'confirmations', 'credentials'}:
            raise NativeError('invalid_request', 400)
        intent = _identifier(body['intent'], 'intent')
        request_hash = digest({'draft': body['draft'], 'confirmations': body['confirmations']})
        old = self.store.lookup(owner, intent)
        if old:
            if old['request_hash'] != request_hash:
                raise NativeError('idempotency_conflict', 409)
            return self.get(owner, old['id'])
        context = self.context()
        value, normalized = plan(body['draft'], context)
        if hasattr(self.api, 'validate_plan'):
            self.api.validate_plan(normalized, context)
        self._verify(owner, body['check'], value)
        if type(body['confirmations']) is not list or sorted(body['confirmations']) != sorted(value['warnings']):
            raise NativeError('confirmation_required', 409)
        credentials = body['credentials']
        if type(credentials) is not dict or set(credentials) != {'password'}:
            raise NativeError('invalid_credentials', 422)
        password = credentials['password']
        if type(password) is not str or len(password.encode()) > 1024 or any(ord(c) < 32 for c in password):
            raise NativeError('invalid_credentials', 422)
        if normalized['password_set'] != bool(password) or (password and len(password) < 5):
            raise NativeError('invalid_credentials', 422)
        # Admission is durable before dispatch. A dispatch failure never causes
        # automatic create retry, and secrets are not written to the journal.
        guard = self.api.admission_guard(value.get('policy_revision')) if hasattr(self.api, 'admission_guard') else nullcontext()
        with guard:
            job, created = self.store.admit(owner, intent, request_hash, value, self.clock())
        if created:
            with self.lock:
                self.active.add(job['id'])
            secrets_body = {'password': password, 'ssh-public-keys': normalized['ssh_public_keys']}
            try:
                self.dispatch(lambda: self._run(job, secrets_body, normalized))
            except Exception:
                secrets_body.clear()
                with self.lock:
                    self.active.discard(job['id'])
                self._state(job, 'failed_before_effect', error='dispatch_failed')
        return self.get(owner, job['id'])

    def _state(self, job, status, *, error=None, phase=None, upid=None):
        job.update(status=status, error=error, updated_at=self.clock())
        if phase is not None:
            job['phase'] = phase
        if upid is not None:
            job['upid'] = upid
        self.store.put(job)

    def _task(self, job, params, path, kind, timeout):
        # Persist the phase before dispatch; a crash in the send/UPID gap is unknown.
        self._state(job, job['status'], phase=kind[0] if isinstance(kind, tuple) else kind)
        raw = self.api.request('POST', path, params)
        upid = _upid(raw, job['node'], job['vmid'], kind)
        self._state(job, job['status'], upid=upid)
        deadline = self.monotonic() + timeout
        while self.monotonic() < deadline:
            value = self.api.request('GET', '/nodes/'+job['node']+'/tasks/'+upid+'/status')
            if type(value) is not dict or value.get('status') not in ('running', 'stopped'):
                raise NativeError('task_result_unknown', 503)
            if value['status'] == 'stopped':
                outcome = task_status_type(value.get('exitstatus'))
                if outcome == 'warning':
                    job.setdefault('task_warnings', []).append({
                        'phase': job['phase'], 'upid': upid, 'exitstatus': value['exitstatus']})
                    self._state(job, job['status'])
                if outcome in ('ok', 'warning'):
                    return
                if outcome == 'error':
                    raise NativeError('task_failed', 503)
                raise NativeError('task_result_unknown', 503)
            self.sleep(2)
        raise NativeError('task_timeout', 504)

    def _run(self, job, credentials, draft):
        effect = False
        try:
            value = job['plan']; base = '/nodes/'+job['node']+'/lxc/'+str(job['vmid'])
            fresh, _ = plan(draft, self.context())
            if fresh['plan_hash'] != value['plan_hash']:
                raise NativeError('check_invalid', 409)
            self._state(job, 'creating', phase='template_preparation')
            template = self.api.prepare_template(job['node'], job['vmid'], value['create']['ostemplate'])
            params = {**value['create'], 'ostemplate': template,
                      **{k: v for k, v in credentials.items() if v}}
            effect = True
            try:
                self._task(job, params, '/nodes/'+job['node']+'/lxc', 'vzcreate', 900)
            finally:
                credentials.clear(); params.pop('password', None); params.pop('ssh-public-keys', None)
            config = self.api.request('GET', base+'/config')
            if not _matches_config(config, value['create']):
                raise NativeError('config_mismatch', 503)
            self._state(job, 'configuring', phase='configure')
            if value['create'].get('pool'):
                pool = self.api.request('GET', '/pools/'+value['create']['pool'])
                if type(pool) is not dict or not any(x.get('vmid') == job['vmid'] and x.get('node') == job['node'] for x in pool.get('members', [])):
                    raise NativeError('pool_readback_failed', 503)
            if value['firewall']['rules']:
                for position, rule in enumerate(value['firewall']['rules']):
                    self.api.request('POST', base+'/firewall/rules', {**rule, 'pos': position})
                self.api.request('PUT', base+'/firewall/options', value['firewall']['options'])
                options = self.api.request('GET', base+'/firewall/options')
                rules = self.api.request('GET', base+'/firewall/rules')
                if any(str(options.get(k)) != str(v) for k, v in value['firewall']['options'].items()) or type(rules) is not list or len(rules) != len(value['firewall']['rules']):
                    raise NativeError('firewall_readback_failed', 503)
                for actual, expected in zip(rules, value['firewall']['rules']):
                    if any(str(actual.get(k)) != str(v) for k, v in expected.items()):
                        raise NativeError('firewall_readback_failed', 503)
            if value['ha_managed']:
                ha = self.api.request('GET', '/cluster/ha/resources/ct:'+str(job['vmid']))
                if type(ha) is not dict or ha.get('sid') != 'ct:'+str(job['vmid']) or ha.get('state') != 'stopped':
                    raise NativeError('ha_readback_failed', 503)
            if value['start']:
                self._state(job, 'starting', phase='start')
                self._task(job, {}, base+'/status/start', ('vzstart', 'hastart') if value['ha_managed'] else 'vzstart', 120)
            current = self.api.request('GET', base+'/status/current')
            # HA's hastart task acknowledges the request, not a running CT.
            deadline = self.monotonic()+120
            while value['ha_managed'] and value['start'] and type(current) is dict and current.get('status') == 'stopped' and self.monotonic() < deadline:
                self.sleep(2)
                current = self.api.request('GET', base+'/status/current')
            if type(current) is not dict or current.get('status') != ('running' if value['start'] else 'stopped'):
                raise NativeError('status_mismatch', 503)
            # Boot startup is committed last. Any earlier partial/unknown
            # operation retains the initial onboot=0 without a rollback write.
            self.api.request('PUT', base+'/config', value['final_config'])
            if not _matches_config(self.api.request('GET', base+'/config'), value['final_config']):
                raise NativeError('config_mismatch', 503)
            job['result'] = {'node': job['node'], 'vmid': job['vmid'], 'status': current['status']}
            self._state(job, 'succeeded', phase='complete')
        except Exception as exc:
            code = exc.code if isinstance(exc, NativeError) else 'operation_failed'
            # A local transport readiness refusal precedes the actual POST.
            # Do not confuse it with an uncertain response after dispatch.
            if isinstance(exc, (HAReadinessError, PolicyDenied)) and job.get('upid') is None:
                effect = False
            state = 'failed_before_effect' if not effect else ('unknown' if code in ('pve_timeout', 'pve_unavailable', 'task_identity_unknown', 'task_result_unknown', 'task_timeout', 'journal_unavailable') else 'partial')
            try:
                self._state(job, state, error=code)
            except Exception:
                # Durable previous phase remains a conservative unknown after restart.
                pass
        finally:
            credentials.clear()
            with self.lock:
                self.active.discard(job['id'])

    def get(self, owner, job_id):
        job = self.store.get(owner, job_id)
        with self.lock:
            active = job['id'] in self.active
        other_alive = (job.get('worker_instance') != _INSTANCE and type(job.get('worker_pid')) is int
                       and job.get('worker_start') is not None
                       and process_start(job['worker_pid']) == job['worker_start']
                       and self.clock()-job['updated_at'] < 3600)
        if job['status'] not in FINAL and not active and not other_alive:
            job['status'] = 'needs_recovery' if job['upid'] else 'unknown'
            job['error'] = 'worker_interrupted'
        return job

    def jobs(self, owner):
        return [self.get(owner, job['id']) for job in self.store.jobs(owner)]

    def observe(self, owner, job_id):
        job = self.get(owner, job_id)
        if job['upid']:
            value = self.api.request('GET', '/nodes/'+job['node']+'/tasks/'+job['upid']+'/status')
            if type(value) is not dict or value.get('status') not in ('running', 'stopped'):
                raise NativeError('task_result_unknown', 503)
            outcome = task_status_type(value.get('exitstatus'))
            job['observed_task'] = {'status': value['status'],
                                    'exitstatus': value.get('exitstatus') if outcome in ('ok', 'warning') else 'ERROR' if outcome == 'error' else None}
        # Read-only projection only. No replay, writes, cleanup or auto-resume.
        return job


_DEFAULT = None
_DEFAULT_LOCK = threading.Lock()


def service():
    global _DEFAULT
    with _DEFAULT_LOCK:
        if _DEFAULT is None:
            _DEFAULT = Service()
        return _DEFAULT
