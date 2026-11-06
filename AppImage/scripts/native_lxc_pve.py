"""Bounded local PVE transport with root approval and admitted-job guards."""
import copy
import hashlib
import json
from pathlib import Path
import re
import selectors
import signal
import os
import socket
import sqlite3
import stat
import subprocess
import sys
import time
from contextlib import contextmanager, nullcontext

from native_lxc import NativeError, defaults, digest, strict_json, require_ha_ready
import native_lxc_policy as policy

WRITE_APPROVAL = Path('/etc/proxmenux/native-lxc-web.json')
JOURNAL = Path('/var/lib/proxmenux/create-native-lxc/jobs.sqlite3')
BRIDGE = Path(__file__).with_name('native_lxc_pve_bridge.pl')
TEMPLATE_HELPER = Path(__file__).with_name('lxc_template_compat.py')


def write_approval():
    """Explicit local root approval; never supplied by a request/environment."""
    return policy.approval()


class PveAPI:
    @contextmanager
    def context_session(self):
        """One discovery owns one process; no cross-request state or writes."""
        reader = _ReadSession()
        try:
            yield reader
        finally:
            reader.close()

    def read_many(self, reads, timeout=30):
        """Fresh GET/schema reads share initialization, never the write path."""
        if (type(reads) is not list or not 1 <= len(reads) <= 64
                or any(type(r) is not dict or set(r) != {'method', 'path', 'params'}
                       or r['method'] not in ('GET', 'SCHEMA') or type(r['path']) is not str
                       or type(r['params']) is not dict for r in reads)):
            raise NativeError('pve_unavailable', 503)
        try:
            raw = self._capture(json.dumps({'reads': reads}).encode(), timeout)
            reply = strict_json(raw, limit=4*1024*1024)
            if type(reply) is not dict or set(reply) != {'ok', 'data'} or reply['ok'] is not True:
                raise ValueError()
            rows = reply['data']
            if type(rows) is not list or len(rows) != len(reads):
                raise ValueError()
            for row in rows:
                if type(row) is not dict or not ((row.get('ok') is True and set(row) == {'ok', 'data'})
                        or (row.get('ok') is False and row == {'ok': False, 'error': 'pve_unavailable'})):
                    raise ValueError()
            return rows
        except subprocess.TimeoutExpired:
            raise NativeError('pve_timeout', 504) from None
        except (OSError, ValueError, NativeError):
            raise NativeError('pve_unavailable', 503) from None

    @contextmanager
    def admission_guard(self, revision):
        with policy.policy_lock():
            value = write_approval()
            if not value:
                raise policy.PolicyDenied('execution_disabled', 503)
            if not value.get('legacy', True) and value['revision'] != revision:
                raise policy.PolicyDenied('policy_changed', 409)
            yield

    def validate_plan(self, draft, context):
        value = write_approval()
        if value and not value.get('legacy', True):
            if context.get('write_policy', {}).get('state') != 'enabled':
                raise policy.PolicyDenied('policy_unavailable', 503)
            policy.allow(value, draft.get('vmid'), draft.get('pool', ''), context.get('write_policy', {}).get('revision'))

    @property
    def mutations_enabled(self):
        return write_approval() is not None

    def _authorize_write(self, method, path, params, deadline=None):
        if 'force' in params or 'restore' in params:
            raise policy.PolicyDenied('execution_disabled', 503)
        approval = write_approval()
        node = approval.get('node', '') if approval else ''
        match = re.fullmatch(r'/nodes/'+re.escape(node)+r'/lxc(?:/([1-9][0-9]{2,8})/(config|status/start|firewall/(?:options|rules)))?', path)
        if approval is None or not match or method not in ('POST', 'PUT'):
            raise policy.PolicyDenied('execution_disabled', 503)
        vmid = params.get('vmid') if match[1] is None else int(match[1])
        if type(vmid) is not int or vmid in approval['protected_vmids']:
            raise policy.PolicyDenied('guest_protected', 403)
        expected = 'POST' if match[2] in (None, 'status/start', 'firewall/rules') else 'PUT'
        if method != expected:
            raise policy.PolicyDenied('execution_disabled', 503)
        # Follow-up effects belong only to this process's admitted new guest.
        try:
            if any(p.is_symlink() for p in (JOURNAL, *JOURNAL.parents)):
                raise ValueError()
            info = JOURNAL.stat()
            if info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o600:
                raise ValueError()
            conn = sqlite3.connect('file:'+str(JOURNAL)+'?mode=ro', uri=True)
            try:
                row = conn.execute('SELECT payload FROM jobs WHERE node=? AND vmid=?', (node, vmid)).fetchone()
            finally:
                conn.close()
            job = strict_json(row[0].encode()) if row else None
            if (not job or job['worker_pid'] != os.getpid() or job['status'] not in ('creating', 'configuring', 'starting')
                    or job.get('worker_start') != Path('/proc/self/stat').read_text().rpartition(')')[2].split()[19]):
                raise ValueError()
        except (OSError, ValueError, KeyError, IndexError, sqlite3.Error, NativeError):
            raise policy.PolicyDenied('admission_required', 403) from None
        if not approval.get('legacy', True):
            policy.allow(approval, vmid, params.get('pool', ''), job['plan'].get('policy_revision', ''))
            protected = protected_members(self, approval, deadline or time.monotonic()+5)
            if vmid in protected:
                raise policy.PolicyDenied('guest_protected', 403)
        return approval

    def request(self, method, path, params=None, timeout=10):
        value = write_approval() if method not in ('GET', 'SCHEMA') else None
        deadline = time.monotonic()+timeout
        lock = policy.policy_lock(timeout=timeout) if value and not value.get('legacy', True) else nullcontext()
        with lock:
            return self._call(method, path, params or {}, max(.001, deadline-time.monotonic()))

    def prepare_template(self, node, vmid, template):
        # Check approval and ownership BEFORE volume resolution, hashing or IO.
        self._authorize_write('POST', '/nodes/'+node+'/lxc', {'vmid': vmid})
        from lxc_template_compat import CACHE, KNOWN, VOLUME
        if type(template) is not str or not VOLUME.fullmatch(template):
            raise NativeError('template_preparation_failed', 503)
        try:
            raw = self._capture(json.dumps({'template': template}).encode(), 300,
                                ['/usr/bin/python3', str(TEMPLATE_HELPER)])
            reply = strict_json(raw, limit=4096)
            keys = {'version', 'template', 'action', 'source_sha256', 'artifact_sha256'}
            if (type(reply) is not dict or set(reply) != keys or reply['version'] != 1
                    or any(type(reply[k]) is not str for k in keys-{'version'})
                    or any(not re.fullmatch('[a-f0-9]{64}', reply[k]) for k in ('source_sha256', 'artifact_sha256'))):
                raise ValueError()
            if reply['action'] == 'unchanged_unrecognized':
                if reply['template'] != template or reply['source_sha256'] != reply['artifact_sha256'] or reply['source_sha256'] in KNOWN:
                    raise ValueError()
            elif reply['action'] in ('prepared', 'cached'):
                if reply['source_sha256'] not in KNOWN or reply['template'] != str(CACHE / reply['source_sha256'] / 'template.tar.zst'):
                    raise ValueError()
            else:
                raise ValueError()
            # Admission can have changed while the bounded helper was running.
            self._authorize_write('POST', '/nodes/'+node+'/lxc', {'vmid': vmid})
            return reply['template']
        except (OSError, ValueError, NativeError, subprocess.TimeoutExpired):
            raise NativeError('template_preparation_failed', 503) from None

    def _call(self, method, path, params, timeout=10):
        deadline = time.monotonic()+timeout
        approval = None
        if method not in ('GET', 'SCHEMA'):
            approval = self._authorize_write(method, path, params, deadline)
        if method == 'POST' and params.get('ha-managed') == 1:
            # Preparation may have taken time since worker discovery. Recheck
            # immediately before sending the HA create, never enable services.
            node = path.split('/')[2]
            require_ha_ready(read_ha_readiness(self, node, True))
        try:
            # Secrets, when a future transport is qualified, use stdin only.
            body = {'method': method, 'path': path, 'params': params}
            if approval and not approval.get('legacy', True):
                body['policy_revision'] = approval['revision']
            remaining = deadline-time.monotonic()
            if remaining <= 0:
                raise policy.PolicyDenied('policy_unavailable', 503)
            raw = self._capture(json.dumps(body).encode(), remaining)
            reply = strict_json(raw, limit=4*1024*1024)
            if type(reply) is not dict or reply.get('ok') is not True or set(reply) != {'ok', 'data'}:
                raise ValueError()
            return reply['data']
        except policy.PolicyDenied:
            raise
        except subprocess.TimeoutExpired:
            raise NativeError('pve_timeout', 504) from None
        except (OSError, ValueError, NativeError):
            raise NativeError('pve_unavailable', 503) from None

    @staticmethod
    def _capture(body, timeout, command=None):
        process = subprocess.Popen(command or ['/usr/bin/perl', str(BRIDGE)], stdin=subprocess.PIPE,
                                   stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                   start_new_session=True,
                                   env={'PATH': '/usr/sbin:/usr/bin:/sbin:/bin', 'LANG': 'C.UTF-8'})
        selector = selectors.DefaultSelector(); output = bytearray(); sent = 0
        end = time.monotonic() + timeout
        try:
            os.set_blocking(process.stdin.fileno(), False); os.set_blocking(process.stdout.fileno(), False)
            selector.register(process.stdin, selectors.EVENT_WRITE, 'stdin')
            selector.register(process.stdout, selectors.EVENT_READ, 'stdout')
            while selector.get_map() or process.poll() is None:
                remaining = end-time.monotonic()
                if remaining <= 0:
                    raise subprocess.TimeoutExpired('pve', timeout)
                for key, _ in selector.select(min(remaining, .05)):
                    if key.data == 'stdin':
                        sent += os.write(key.fileobj.fileno(), body[sent:sent+65536])
                        if sent == len(body):
                            selector.unregister(key.fileobj); key.fileobj.close()
                    else:
                        chunk = os.read(key.fileobj.fileno(), 65536)
                        if not chunk:
                            selector.unregister(key.fileobj)
                        elif len(output)+len(chunk) > 4*1024*1024:
                            raise ValueError()
                        else:
                            output.extend(chunk)
            if process.returncode:
                raise ValueError()
            return bytes(output)
        finally:
            selector.close()
            try: os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError: pass
            process.wait(timeout=1)
            if not process.stdin.closed: process.stdin.close()
            process.stdout.close()


class _ReadSession(PveAPI):
    def __init__(self):
        self.process = None
        self.frames = 0

    def _capture(self, body, timeout, command=None):
        if command is not None or len(body) > 131072 or self.frames >= 64:
            raise ValueError()
        self.frames += 1
        if self.process is None:
            self.process = subprocess.Popen(['/usr/bin/perl', str(BRIDGE), '--read-session'],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                start_new_session=True,
                env={'PATH': '/usr/sbin:/usr/bin:/sbin:/bin', 'LANG': 'C.UTF-8'})
            os.set_blocking(self.process.stdin.fileno(), False)
            os.set_blocking(self.process.stdout.fileno(), False)
        process = self.process
        selector = selectors.DefaultSelector(); output = bytearray(); sent = 0
        frame = body + b'\n'; end = time.monotonic() + timeout
        try:
            selector.register(process.stdin, selectors.EVENT_WRITE, 'stdin')
            selector.register(process.stdout, selectors.EVENT_READ, 'stdout')
            while True:
                remaining = end-time.monotonic()
                if remaining <= 0:
                    raise subprocess.TimeoutExpired('pve', timeout)
                for key, _ in selector.select(min(remaining, .05)):
                    if key.data == 'stdin':
                        sent += os.write(key.fileobj.fileno(), frame[sent:sent+65536])
                        if sent == len(frame):
                            selector.unregister(key.fileobj)
                    else:
                        chunk = os.read(key.fileobj.fileno(), 65536)
                        if not chunk or len(output)+len(chunk) > 4*1024*1024:
                            raise ValueError()
                        output.extend(chunk)
                        if b'\n' in output:
                            if not output.endswith(b'\n') or output.count(b'\n') != 1:
                                raise ValueError()
                            return bytes(output[:-1])
        finally:
            selector.close()

    def close(self):
        if self.process is None:
            return
        process, self.process = self.process, None
        try: os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError: pass
        process.wait(timeout=1)
        process.stdin.close(); process.stdout.close()


def _list(value):
    if type(value) is not list or len(value) > 100000 or any(type(x) is not dict for x in value):
        raise NativeError('pve_output_invalid', 503)
    return value


def _flag(value):
    if value in (0, 1, False, True, '0', '1'):
        return value in (1, True, '1')
    raise NativeError('pve_output_invalid', 503)


def _optional(api, path, params=None, timeout=10):
    try:
        return {'state': 'available', 'data': api.request('GET', path, params or {}, timeout=timeout)}
    except NativeError:
        return {'state': 'unavailable', 'data': None}


def read_ha_readiness(api, node, supported, timeout=5):
    """Bounded GET-only runtime readiness, separate from API support.

    Shapes come from the real PVE cluster/status and node/services outputs.
    Empty resources or watchdog standby are deliberately not prerequisites.
    """
    if not supported:
        return {'state': 'unsupported', 'reason': 'unsupported'}
    unknown = {'state': 'unknown', 'reason': 'unverified'}
    deadline = time.monotonic() + timeout
    def read(path):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise NativeError('pve_timeout', 504)
        result = api.request('GET', path, timeout=remaining)
        if time.monotonic() >= deadline:
            raise NativeError('pve_timeout', 504)
        return _list(result)
    try:
        clusters = [x for x in read('/cluster/status') if x.get('type') == 'cluster']
        if len(clusters) != 1 or type(clusters[0].get('quorate')) not in (int, bool) or clusters[0]['quorate'] not in (0, 1):
            return unknown
        if not clusters[0]['quorate']:
            return {'state': 'unavailable', 'reason': 'no_quorum'}
        services = read('/nodes/' + node + '/services')
        states = []
        for name in ('pve-ha-crm', 'pve-ha-lrm'):
            rows = [x for x in services if x.get('service') == name]
            if len(rows) != 1:
                return unknown
            row = rows[0]
            if (row.get('state') not in ('running', 'dead', 'stopped', 'failed', 'exited', 'auto-restart', 'start', 'start-pre', 'stop', 'stop-post', 'stop-sigterm')
                    or row.get('active-state') not in ('active', 'inactive', 'failed', 'activating', 'deactivating', 'reloading', 'maintenance')):
                return unknown
            states.append(row['state'] == 'running' and row['active-state'] == 'active')
        return {'state': 'ready', 'reason': 'ready'} if all(states) else {'state': 'unavailable', 'reason': 'services_inactive'}
    except (NativeError, OSError, ValueError, TypeError, subprocess.TimeoutExpired):
        return unknown


def protected_members(api, approval, deadline):
    def read(path):
        remaining = deadline-time.monotonic()
        if remaining <= 0:
            raise policy.PolicyDenied('policy_unavailable', 503)
        return api.request('GET', path, timeout=min(5, remaining))
    if not approval['protected_pools']:
        return set()
    pools = _list(read('/pools'))
    if any(type(p.get('poolid')) is not str for p in pools):
        raise policy.PolicyDenied('policy_unavailable', 503)
    names = [p['poolid'] for p in pools]
    if len(set(names)) != len(names):
        raise policy.PolicyDenied('policy_unavailable', 503)
    result = set()
    for name in approval['protected_pools']:
        if name not in names:
            continue
        value = read('/pools/'+name)
        if type(value) is not dict or type(value.get('members')) is not list:
            raise policy.PolicyDenied('policy_unavailable', 503)
        for item in value['members']:
            if type(item) is not dict or item.get('type') not in ('lxc', 'qemu', 'storage'):
                raise policy.PolicyDenied('policy_unavailable', 503)
            if item['type'] in ('lxc', 'qemu'):
                if type(item.get('vmid')) is not int or not 100 <= item['vmid'] <= 999999999:
                    raise policy.PolicyDenied('policy_unavailable', 503)
                result.add(item['vmid'])
    if time.monotonic() >= deadline:
        raise policy.PolicyDenied('policy_unavailable', 503)
    return result


class ContextReads(PveAPI):
    """A single discovery's fresh replies, never shared across requests."""
    def __init__(self, api, node):
        self.api, self.rows = api, {}
        self.deadline = time.monotonic()+30
        prefix = '/nodes/'+node
        queries = [('GET', p, {}) for p in ('/version', '/nodes', prefix+'/status',
                   prefix+'/storage', prefix+'/network', '/cluster/nextid', '/pools')]
        queries += [('SCHEMA', prefix+'/lxc', {}), ('GET', '/cluster/resources', {'type': 'vm'})]
        self.fill(queries)
        extra = []
        schema = self.rows[self.key('SCHEMA', prefix+'/lxc', {})]
        if (schema.get('ok') and type(schema['data']) is dict
                and type(schema['data'].get('properties')) is list
                and 'ha-managed' in schema['data']['properties']):
            extra += [('GET', '/cluster/status', {}), ('GET', prefix+'/services', {})]
        storage = self.rows[self.key('GET', prefix+'/storage', {})]
        if storage.get('ok') and type(storage['data']) is list:
            for item in storage['data']:
                if (type(item) is dict and type(item.get('storage')) is str
                        and re.fullmatch(r'[A-Za-z][A-Za-z0-9_.-]{0,63}', item['storage'])
                        and type(item.get('content')) is str and 'vztmpl' in item['content'].split(',')
                        and _flag(item.get('active', 0)) and _flag(item.get('enabled', 0))):
                    extra.append(('GET', prefix+'/storage/'+item['storage']+'/content', {'content':'vztmpl'}))
        approval = policy.approval()
        pools = self.rows[self.key('GET', '/pools', {})]
        if approval and not approval['legacy'] and pools.get('ok') and type(pools['data']) is list:
            names = {x.get('poolid') for x in pools['data'] if type(x) is dict and type(x.get('poolid')) is str}
            extra += [('GET', '/pools/'+name, {}) for name in approval['protected_pools'] if name in names]
        if extra:
            self.fill(extra)

    @staticmethod
    def key(method, path, params):
        return method, path, json.dumps(params, sort_keys=True)

    def fill(self, queries):
        for offset in range(0, len(queries), 64):
            remaining = self.deadline-time.monotonic()
            if remaining <= 0:
                raise NativeError('context_timeout', 504)
            part = queries[offset:offset+64]
            replies = self.api.read_many([{'method':m,'path':p,'params':v} for m,p,v in part],timeout=remaining)
            self.rows.update({self.key(*query):reply for query,reply in zip(part,replies)})

    def request(self, method, path, params=None, timeout=10):
        if method not in ('GET','SCHEMA'):
            raise NativeError('execution_disabled',503)
        key = self.key(method,path,params or {})
        if key not in self.rows:
            raise NativeError('pve_unavailable',503)
        row = self.rows[key]
        if not row['ok']:
            raise NativeError('pve_unavailable',503)
        return copy.deepcopy(row['data'])


def read_context(api, node=None, clock=time.time):
    node = node or socket.gethostname().split('.')[0]
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9.-]{0,62}', node):
        raise NativeError('node_unavailable', 503)
    # Tests and alternate readers retain their own transport; no per-instance
    # cache is installed on the shared production PveAPI object.
    if type(api) is PveAPI:
        with api.context_session() as reader:
            api = ContextReads(reader, node)
    return _read_context(api, node, clock)


def _read_context(api, node=None, clock=time.time):
    node = node or socket.gethostname().split('.')[0]
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9.-]{0,62}', node):
        raise NativeError('node_unavailable', 503)
    prefix = '/nodes/'+node
    start = time.monotonic()
    def read(path, params=None):
        if time.monotonic() - start >= 30:
            raise NativeError('context_timeout', 504)
        return api.request('GET', path, params or {}, timeout=max(.01, min(10, 30-(time.monotonic()-start))))
    version = read('/version')
    nodes = _list(read('/nodes'))
    current = [n for n in nodes if n.get('node') == node]
    if len(current) != 1 or current[0].get('status') != 'online':
        raise NativeError('node_unavailable', 503)
    status = read(prefix+'/status')
    if type(version) is not dict or type(status) is not dict:
        raise NativeError('pve_output_invalid', 503)
    schema = api.request('SCHEMA', prefix+'/lxc', timeout=max(.01, min(10, 30-(time.monotonic()-start))))
    if type(schema) is not dict or type(schema.get('properties')) is not list:
        raise NativeError('schema_unavailable', 503)
    arch = status.get('cpuinfo', {}).get('arch')
    # PVE's status cpuinfo does not always expose architecture. The local
    # bridge returns uname via the schema envelope; never infer amd64.
    arch = schema.get('architecture', arch)
    arch = {'x86_64': 'amd64', 'aarch64': 'arm64'}.get(arch, arch)
    if arch not in ('amd64', 'arm64'):
        raise NativeError('architecture_unsupported', 503)
    storages, templates = [], []
    for item in _list(read(prefix+'/storage')):
        name = item.get('storage')
        if type(name) is not str or not re.fullmatch(r'[A-Za-z][A-Za-z0-9_.-]{0,63}', name):
            raise NativeError('pve_output_invalid', 503)
        if type(item.get('content')) is not str:
            raise NativeError('pve_output_invalid', 503)
        content = sorted(item['content'].split(','))
        active = _flag(item.get('active', 0)) and _flag(item.get('enabled', 0))
        avail = item.get('avail')
        if avail is not None and (type(avail) not in (int, float) or avail < 0):
            raise NativeError('pve_output_invalid', 503)
        storages.append({'id': name, 'type': item.get('type', ''), 'content': content,
                         'active': active, 'available': avail,
                         'acl': item.get('type') in ('dir', 'zfspool', 'btrfs', 'lvmthin', 'lvm', 'rbd'),
                         # PVE ignores noacl on RAW/ext4. A storage type alone
                         # does not qualify the format/FS of a future volume.
                         'acl_disable': False,
                         'quota': item.get('type') in ('dir', 'lvm', 'lvmthin')})
        if active and 'vztmpl' in content:
            for template in _list(read(prefix+'/storage/'+name+'/content', {'content': 'vztmpl'})):
                volid = template.get('volid')
                if type(volid) is not str or not re.fullmatch(re.escape(name)+r':vztmpl/[A-Za-z0-9_.+-]+\.(?:tar(?:\.(?:gz|xz|zst))?|tgz)', volid):
                    raise NativeError('pve_output_invalid', 503)
                match = re.search(r'_(amd64|arm64)\.', volid)
                # Some official archives include a version suffix after arch.
                template_arch = match[1] if match else None
                templates.append({'id': volid, 'name': volid.split('/')[-1], 'storage': name,
                                  'arch': template_arch, 'downloaded': True,
                                  'size': template.get('size'), 'ctime': template.get('ctime')})
    bridges = []
    for item in _list(read(prefix+'/network')):
        if item.get('type') not in ('bridge', 'OVSBridge') or not item.get('iface'):
            continue
        name = item['iface']
        if not re.fullmatch(r'[A-Za-z0-9_.:-]{1,32}', name):
            raise NativeError('pve_output_invalid', 503)
        bridges.append({'id': name, 'vlan_aware': _flag(item.get('bridge_vlan_aware', 0)),
                        'active': _flag(item.get('active', 0))})
    resources = _list(read('/cluster/resources', {'type': 'vm'}))
    occupied = []
    for item in resources:
        vmid = item.get('vmid')
        if type(vmid) is not int or not 100 <= vmid <= 999999999:
            raise NativeError('pve_output_invalid', 503)
        occupied.append(vmid)
    nextid = read('/cluster/nextid')
    if type(nextid) is str and nextid.isdigit():
        nextid = int(nextid)
    if type(nextid) is not int or not 100 <= nextid <= 999999999 or nextid in occupied:
        raise NativeError('pve_output_invalid', 503)
    approval = write_approval() if isinstance(api, PveAPI) else None
    if approval:
        occupied.extend(approval['protected_vmids'])
        while nextid in occupied and nextid < 999999999:
            nextid += 1
    pools = _optional(api, '/pools', timeout=max(.01, min(10, 30-(time.monotonic()-start))))
    if time.monotonic()-start >= 30:
        raise NativeError('context_timeout', 504)
    pool_ids = []
    if pools['state'] == 'available':
        for item in _list(pools['data']):
            name = item.get('poolid')
            if type(name) is not str or not re.fullmatch(r'[A-Za-z][A-Za-z0-9_.-]{0,63}', name):
                raise NativeError('pve_output_invalid', 503)
            pool_ids.append(name)
    capabilities = {'ha': 'ha-managed' in schema['properties'],
                    'host_managed': 'host-managed' in schema.get('network_fields', []),
                    'keepattrs': 'keepattrs' in schema.get('mount_fields', []),
                    # A configured enable is not proof of an active firewall.
                    # No host service probe or implicit enable is performed.
                    'firewall_active': False, 'devices': False, 'bind_mounts': False}
    storages.sort(key=lambda item: item['id'])
    templates.sort(key=lambda item: item['id'])
    bridges.sort(key=lambda item: item['id'])
    pool_ids.sort()
    ha_readiness = read_ha_readiness(api, node, capabilities['ha'], timeout=max(.01, min(5, 30-(time.monotonic()-start))))
    context = {'schema': 'native.lxc.context.v1', 'state': 'available', 'node': node, 'arch': arch,
               'version': version, 'cgroup': current[0].get('cgroup-mode', 2),
               'storages': storages, 'templates': templates, 'bridges': bridges,
               'pools': pool_ids, 'pool_state': pools['state'], 'occupied_ids': sorted(set(occupied)),
               'nextid': nextid, 'bind_paths': {}, 'capabilities': capabilities, 'ha_readiness': ha_readiness,
               'can_execute': bool(api.mutations_enabled), 'can_download': False,
               'observed_at': clock(), 'reserved': False}
    current_policy = policy.load_policy() if isinstance(api, PveAPI) else None
    if isinstance(api, PveAPI):
        context['write_policy'] = policy.public_policy(current_policy)
    suggested = nextid
    if isinstance(api, PveAPI) and context['write_policy']['state'] in ('unavailable', 'disabled'):
        context['can_execute'] = False
        suggested = None
    elif current_policy and not current_policy['legacy']:
        from native_lxc_jobs import Store
        try:
            blocked = set(occupied) | set(current_policy['protected_vmids'])
            if current_policy['enabled']:
                blocked.update(protected_members(api, current_policy, min(start+30, time.monotonic()+5)))
                blocked.update(Store(JOURNAL).reserved_ids(node))
                suggested = policy.first_free(current_policy['allowed_vmids'], blocked)
                if suggested is None:
                    context['can_execute'] = False
                    context['write_policy']['reason'] = 'ids_exhausted'
            else:
                suggested = None
        except (NativeError, OSError, ValueError):
            suggested = None
            context['can_execute'] = False
            context['write_policy'].update(state='unavailable', reason='protected_state_unknown')
    if context['cgroup'] not in (1, 2):
        raise NativeError('pve_output_invalid', 503)
    # Time/nextid/volatile storage capacity do not invalidate an otherwise
    # identical capability revision. HA runtime readiness is also checked anew
    # for HA admission; its changes must not invalidate ordinary non-HA plans.
    stable = {key: context[key] for key in ('node', 'arch', 'version', 'cgroup', 'pools', 'pool_state', 'capabilities')}
    stable['storages'] = [{k: v for k, v in x.items() if k != 'available'} for x in storages]
    stable['templates'] = templates; stable['bridges'] = bridges
    if 'write_policy' in context:
        # Exhaustion changes after this job reserves the last ID. It is a
        # volatile admission hint, not a change to the signed host policy.
        stable['write_policy'] = {key: context['write_policy'][key] for key in ('state', 'revision')}
    context['revision'] = digest(stable)
    context['defaults'] = defaults(context)
    context['defaults']['vmid'] = suggested
    return context
