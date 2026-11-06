"""Inert PVE contracts derived from the upstream API schemas, not a live host."""
import copy
from pathlib import Path
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'AppImage/scripts'))
from native_lxc import defaults, digest, NativeError


def context(now=None):
    result = {'schema': 'native.lxc.context.v1', 'state': 'available', 'node': 'testnode',
              'arch': 'amd64', 'version': {'version': '9.0.0'}, 'cgroup': 2,
              'storages': [{'id': 'local', 'content': ['vztmpl'], 'type': 'dir', 'active': True, 'available': 100*1024**3, 'acl': True, 'quota': True},
                           {'id': 'local-zfs', 'content': ['rootdir'], 'type': 'zfspool', 'active': True, 'available': 100*1024**3, 'acl': True, 'quota': False}],
              'templates': [{'id': 'local:vztmpl/debian-13-standard_13.0-1_amd64.tar.zst', 'name': 'debian-13-standard_13.0-1_amd64.tar.zst', 'storage': 'local', 'arch': 'amd64', 'downloaded': True}],
              'bridges': [{'id': 'vmbr0', 'vlan_aware': True, 'active': True}],
              'pools': ['lab'], 'pool_state': 'available', 'occupied_ids': [106, 4100, 4101, 4102],
              'nextid': 4900, 'bind_paths': {}, 'capabilities': {'ha': True, 'host_managed': True, 'keepattrs': True, 'firewall_active': True, 'devices': False, 'bind_mounts': False},
              'ha_readiness': {'state': 'ready', 'reason': 'ready'},
              'can_execute': True, 'can_download': False, 'observed_at': time.time() if now is None else now, 'reserved': False}
    result['revision'] = digest({k: v for k, v in result.items() if k != 'observed_at'})
    result['defaults'] = defaults(result)
    return result


def draft(ctx=None):
    ctx = ctx or context()
    value = defaults(ctx)
    value['hostname'] = 'native-test'; value['password_set'] = True
    value['template'] = ctx['templates'][0]['id']; value['rootfs']['storage'] = 'local-zfs'
    value['network']['bridge'] = 'vmbr0'
    return value


class FakePVE:
    mutations_enabled = True
    def __init__(self):
        self.calls = []; self.preparations = []; self.config = None; self.running = False; self.fail = None
        self.task_statuses = []; self.rules = []; self.options = {}; self.ha = False
        self.pool = None; self.create_reply = None; self.start_reply = None

    def prepare_template(self, node, vmid, template):
        self.preparations.append((node, vmid, template))
        return template

    def request(self, method, path, params=None, **kwargs):
        # Record only sanitized metadata; even fixture passwords never enter evidence.
        params = params or {}
        self.calls.append((method, path, {k: v for k, v in params.items() if k not in ('password', 'ssh-public-keys')}))
        if self.fail and self.fail(method, path, params):
            raise NativeError('pve_timeout', 504)
        if method == 'POST' and path == '/nodes/testnode/lxc':
            self.config = {k: v for k, v in params.items() if k not in ('password', 'ssh-public-keys', 'pool', 'ha-managed')}
            self.pool = params.get('pool'); self.ha = bool(params.get('ha-managed'))
            self.config.pop('ostemplate', None)
            self.config.pop('start', None)  # PVE consumes this API control field.
            for key, value in list(self.config.items()):
                if key == 'rootfs' or key.startswith('mp'):
                    fields = value.split(',')
                    if not fields[0].startswith('/'):
                        storage, size = fields[0].split(':')
                        fields[0] = storage+':subvol-4900-disk-'+('0' if key == 'rootfs' else str(int(key[2:])+1))
                        fields.append('size='+size+'G')
                    self.config[key] = ','.join(fields)
            return self.create_reply if self.create_reply is not None else 'UPID:testnode:00001234:00005678:69000000:vzcreate:4900:root@pam:'
        if method == 'POST' and path.endswith('/status/start'):
            self.running = True
            return self.start_reply or 'UPID:testnode:00001234:00005678:69000001:'+('hastart' if self.ha else 'vzstart')+':4900:root@pam:'
        if method == 'GET' and '/tasks/' in path:
            return self.task_statuses.pop(0) if self.task_statuses else {'status': 'stopped', 'exitstatus': 'OK'}
        if method == 'GET' and path.endswith('/config'):
            return copy.deepcopy(self.config)
        if method == 'GET' and path.startswith('/pools/'):
            return {'members': [{'vmid': 4900, 'node': 'testnode'}] if self.pool == path.split('/')[-1] else []}
        if method == 'GET' and path.startswith('/cluster/ha/resources/'):
            return {'sid': 'ct:4900', 'state': 'stopped'} if self.ha else None
        if method == 'PUT' and path.endswith('/config'):
            self.config.update(params); return None
        if method == 'POST' and path.endswith('/firewall/rules'):
            self.rules.append({k: v for k, v in params.items() if k != 'pos'}); return None
        if method == 'PUT' and path.endswith('/firewall/options'):
            self.options = dict(params); return None
        if method == 'GET' and path.endswith('/firewall/rules'):
            return copy.deepcopy(self.rules)
        if method == 'GET' and path.endswith('/firewall/options'):
            return copy.deepcopy(self.options)
        if method == 'GET' and path.endswith('/status/current'):
            return {'status': 'running' if self.running else 'stopped'}
        raise AssertionError((method, path))
