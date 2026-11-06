"""Native PVE LXC configuration, validation and one canonical create plan.

No subprocess, filesystem mutation, OCI import or allocation lives here.
Passwords are deliberately absent from the draft and the resulting plan.
"""
import base64
import copy
import hashlib
import ipaddress
import json
import math
import re


class NativeError(Exception):
    def __init__(self, code, status=422, field=None):
        self.code, self.status, self.field = code, status, field
        super().__init__(code)


class HAReadinessError(NativeError):
    """Local admission refusal before sending a guest-creating request."""


def require_ha_ready(readiness):
    """New HA admission only; historical plans remain readable."""
    if type(readiness) is dict and readiness == {'state': 'ready', 'reason': 'ready'}:
        return
    if (type(readiness) is dict and set(readiness) == {'state', 'reason'}
            and readiness.get('state') == 'unavailable'
            and readiness.get('reason') in ('services_inactive', 'no_quorum')):
        raise HAReadinessError('ha_not_ready', 422, 'ha_managed')
    raise HAReadinessError('ha_status_unknown', 503, 'ha_managed')


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'),
                                     ensure_ascii=True, allow_nan=False).encode()).hexdigest()


def strict_json(raw, limit=131072):
    def unique(pairs):
        value = {}
        for key, item in pairs:
            if key in value:
                raise ValueError()
            value[key] = item
        return value
    try:
        if len(raw) > limit:
            raise NativeError('body_too_large', 413)
        return json.loads(raw, object_pairs_hook=unique,
                          parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
    except (ValueError, UnicodeError, RecursionError):
        raise NativeError('invalid_request', 400) from None


def exact(value, fields, field='draft'):
    if type(value) is not dict or set(value) != set(fields.split()):
        raise NativeError('invalid_field', field=field)


def integer(value, low, high, field, optional=False):
    if optional and value is None:
        return None
    if type(value) is not int or not low <= value <= high:
        raise NativeError('invalid_field', field=field)
    return value


def number(value, low, high, field, optional=False):
    if optional and value is None:
        return None
    if type(value) not in (int, float) or not math.isfinite(value) or not low <= value <= high:
        raise NativeError('invalid_field', field=field)
    return value


def text(value, pattern, limit, field, empty=False):
    if type(value) is not str or len(value) > limit or (not value and not empty) or (value and not re.fullmatch(pattern, value)):
        raise NativeError('invalid_field', field=field)
    return value


def boolean(value, field):
    if type(value) is not bool:
        raise NativeError('invalid_field', field=field)
    return value


def dns_name(value, field, empty=False):
    text(value, r'[A-Za-z0-9.-]+', 253, field, empty)
    if value and any(not re.fullmatch(r'[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?', label)
                     for label in value.rstrip('.').split('.')):
        raise NativeError('invalid_field', field=field)
    return value


def target_path(value, field):
    text(value, r'/[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)*', 255, field)
    if any(part in ('.', '..') for part in value.split('/')) or value == '/var' or re.match(r'^/(etc|proc|sys|dev|root|boot|run)(/|$)', value):
        raise NativeError('unsafe_mount', field=field)
    return value


def ssh_keys(raw):
    if type(raw) is not str or len(raw.encode()) > 32768 or 'PRIVATE KEY' in raw:
        raise NativeError('invalid_ssh_key', field='ssh_public_keys')
    keys = []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split()
        if len(parts) < 2 or parts[0] not in ('ssh-ed25519', 'ssh-rsa', 'ecdsa-sha2-nistp256',
                                             'ecdsa-sha2-nistp384', 'ecdsa-sha2-nistp521',
                                             'sk-ssh-ed25519@openssh.com', 'sk-ecdsa-sha2-nistp256@openssh.com'):
            raise NativeError('invalid_ssh_key', field='ssh_public_keys')
        try:
            decoded = base64.b64decode(parts[1], validate=True)
            length = int.from_bytes(decoded[:4], 'big')
            if length != len(parts[0]) or decoded[4:4+length].decode() != parts[0] or len(decoded) <= length+8:
                raise ValueError()
        except (ValueError, UnicodeError):
            raise NativeError('invalid_ssh_key', field='ssh_public_keys') from None
        keys.append(line)
    if len(keys) > 32:
        raise NativeError('invalid_ssh_key', field='ssh_public_keys')
    return '\n'.join(keys)


def defaults(context):
    return {
        'node': context['node'], 'vmid': context.get('nextid'), 'hostname': '',
        'unprivileged': True, 'nesting': True, 'pool': '', 'ha_managed': False,
        'tags': [], 'password_set': False, 'ssh_public_keys': '', 'template': '',
        'rootfs': {'storage': '', 'size_gib': 8, 'acl': None, 'quota': False,
                   'replicate': True, 'mount_options': []}, 'mounts': [],
        'cpu': {'cores': 1, 'limit': None, 'units': None},
        'memory': {'memory': 512, 'swap': 512},
        'network': {'name': 'eth0', 'hwaddr': '', 'bridge': '', 'tag': None,
                    'firewall': True, 'ipv4': {'mode': 'none', 'address': '', 'gateway': ''},
                    'ipv6': {'mode': 'none', 'address': '', 'gateway': ''},
                    'link_down': False, 'mtu': None, 'rate': None, 'host_managed': False},
        'dns': {'nameservers': [], 'searchdomains': []}, 'onboot': False,
        'startup': {'order': None, 'up': None, 'down': None}, 'start': False,
        'firewall': {'mode': 'inherit', 'ssh_sources': [], 'log': False}, 'devices': [],
    }


def _storage(context, name, field):
    text(name, r'[A-Za-z][A-Za-z0-9_.-]*', 64, field)
    matches = [x for x in context['storages'] if x['id'] == name and 'rootdir' in x['content'] and x['active']]
    if len(matches) != 1:
        raise NativeError('storage_unavailable', field=field)
    return matches[0]


def _disk(value, context, field, root=False, unprivileged=True):
    base = 'storage size_gib acl quota replicate mount_options'
    exact(value, base if root else base+' id kind path_id target ro backup keepattrs', field)
    if not root:
        integer(value['id'], 0, 255, field+'.id')
        if value['kind'] not in ('volume', 'bind'):
            raise NativeError('invalid_field', field=field+'.kind')
        target_path(value['target'], field+'.target')
        for key in ('ro', 'backup', 'keepattrs'):
            boolean(value[key], field+'.'+key)
    bind = not root and value['kind'] == 'bind'
    for key in ('quota', 'replicate'):
        boolean(value[key], field+'.'+key)
    if bind:
        if (value['path_id'] not in context.get('bind_paths', {}) or not value['ro']
                or value['backup'] or value['quota'] or value['acl'] is not None
                or value['keepattrs'] or value['storage'] or value['size_gib'] is not None):
            raise NativeError('bind_unavailable', field=field)
        source = context['bind_paths'][value['path_id']]
        if source.get('state') != 'available' or not source.get('permissions_verified'):
            raise NativeError('bind_unavailable', field=field)
    else:
        if not root and value['path_id']:
            raise NativeError('invalid_field', field=field+'.path_id')
        storage = _storage(context, value['storage'], field+'.storage')
        number(value['size_gib'], 1, 1048576, field+'.size_gib')
        for key in ('acl', 'quota', 'replicate'):
            if key == 'acl' and value[key] is None:
                continue
            boolean(value[key], field+'.'+key)
        if value['quota'] and (unprivileged or not storage.get('quota', False)):
            raise NativeError('quota_unsupported', field=field+'.quota')
        if value['acl'] is not None and not storage.get('acl', False):
            raise NativeError('acl_unsupported', field=field+'.acl')
        if value['acl'] is False and storage.get('acl_disable') is not True:
            raise NativeError('acl_disable_unsupported', field=field+'.acl')
        if not root and value['keepattrs'] and not context['capabilities'].get('keepattrs'):
            raise NativeError('capability_unavailable', field=field+'.keepattrs')
    if type(value['mount_options']) is not list or len(value['mount_options']) > 6 or any(type(x) is not str or x not in ('discard', 'lazytime', 'noatime', 'nodev', 'noexec', 'nosuid') for x in value['mount_options']) or len(set(value['mount_options'])) != len(value['mount_options']):
        raise NativeError('invalid_field', field=field+'.mount_options')
    if not root and value['ro'] and any(x not in ('nodev', 'noexec', 'nosuid') for x in value['mount_options']):
        raise NativeError('invalid_field', field=field+'.mount_options')


def _address(value, version):
    exact(value, 'mode address gateway', 'network.ipv'+str(version))
    modes = ('none', 'static', 'dhcp') + (('auto',) if version == 6 else ())
    if value['mode'] not in modes:
        raise NativeError('invalid_field', field='network.ipv'+str(version))
    for name in ('address', 'gateway'):
        text(value[name], r'[0-9a-fA-F:./]+', 64, 'network.ipv'+str(version)+'.'+name, True)
    # Ignore stale inactive addresses exactly as PVE NetworkInputPanel does.
    if value['mode'] != 'static':
        value['address'] = value['gateway'] = ''
    else:
        try:
            if '/' not in value['address'] or ipaddress.ip_interface(value['address']).version != version:
                raise ValueError()
            if value['gateway'] and ipaddress.ip_address(value['gateway']).version != version:
                raise ValueError()
        except ValueError:
            raise NativeError('invalid_address', field='network.ipv'+str(version)) from None


def validate(raw, context):
    """Return a fresh normalized draft. Never mutate the caller or its context."""
    if type(raw) is not dict:
        raise NativeError('invalid_request', 400)
    value = copy.deepcopy(raw)
    exact(value, 'node vmid hostname unprivileged nesting pool ha_managed tags password_set ssh_public_keys template rootfs mounts cpu memory network dns onboot startup start firewall devices')
    if value['node'] != context['node']:
        raise NativeError('node_forbidden', 403)
    if context.get('state') != 'available':
        raise NativeError('context_unavailable', 503)
    integer(value['vmid'], 100, 999999999, 'vmid')
    if value['vmid'] in context['occupied_ids']:
        raise NativeError('vmid_conflict', 409, 'vmid')
    dns_name(value['hostname'], 'hostname', True)
    for key in ('unprivileged', 'nesting', 'ha_managed', 'password_set', 'onboot', 'start'):
        boolean(value[key], key)
    if not value['unprivileged'] and value['nesting']:
        raise NativeError('invalid_field', field='nesting')
    if value['ha_managed'] and not context['capabilities'].get('ha'):
        raise NativeError('capability_unavailable', field='ha_managed')
    if value['ha_managed']:
        require_ha_ready(context.get('ha_readiness'))
    text(value['pool'], r'[A-Za-z][A-Za-z0-9_.-]*', 64, 'pool', True)
    if value['pool'] and value['pool'] not in context['pools']:
        raise NativeError('pool_unavailable', field='pool')
    if type(value['tags']) is not list or len(value['tags']) > 32:
        raise NativeError('invalid_field', field='tags')
    for tag in value['tags']:
        text(tag, r'[A-Za-z0-9_][A-Za-z0-9_.+-]*', 64, 'tags')
    value['tags'] = sorted(set(value['tags']))
    value['ssh_public_keys'] = ssh_keys(value['ssh_public_keys'])
    if not value['password_set'] and not value['ssh_public_keys']:
        raise NativeError('access_required', field='password_set')
    templates = [x for x in context['templates'] if x['id'] == value['template'] and x['downloaded'] and x['arch'] == context['arch']]
    if len(templates) != 1:
        raise NativeError('template_unavailable', field='template')
    _disk(value['rootfs'], context, 'rootfs', root=True, unprivileged=value['unprivileged'])
    if type(value['mounts']) is not list or len(value['mounts']) > 256:
        raise NativeError('invalid_field', field='mounts')
    paths, ids, allocation = [], [], {}
    for i, mount in enumerate(value['mounts']):
        _disk(mount, context, 'mounts.'+str(i), unprivileged=value['unprivileged'])
        if mount['id'] in ids or any(mount['target'] == p or mount['target'].startswith(p+'/') or p.startswith(mount['target']+'/') for p in paths):
            raise NativeError('mount_conflict', field='mounts')
        paths.append(mount['target']); ids.append(mount['id'])
    for disk in [value['rootfs'], *[m for m in value['mounts'] if m['kind'] == 'volume']]:
        allocation[disk['storage']] = allocation.get(disk['storage'], 0) + disk['size_gib'] * 1024**3
    for storage, size in allocation.items():
        item = _storage(context, storage, 'rootfs.storage')
        if item['available'] is None:
            raise NativeError('capacity_unknown', 503)
        if size > item['available']:
            raise NativeError('capacity_insufficient')
    exact(value['cpu'], 'cores limit units', 'cpu')
    integer(value['cpu']['cores'], 1, 8192, 'cpu.cores', True)
    number(value['cpu']['limit'], 0, 8192, 'cpu.limit', True)
    integer(value['cpu']['units'], 8, 10000 if context['cgroup'] == 2 else 500000, 'cpu.units', True)
    exact(value['memory'], 'memory swap', 'memory')
    integer(value['memory']['memory'], 16, 2**31-1, 'memory.memory')
    integer(value['memory']['swap'], 0, 2**31-1, 'memory.swap')
    net = value['network']
    if net is not None:
        exact(net, 'name hwaddr bridge tag firewall ipv4 ipv6 link_down mtu rate host_managed', 'network')
        text(net['name'], r'[A-Za-z][A-Za-z0-9_.-]*', 15, 'network.name')
        text(net['hwaddr'], r'(?:[0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}', 17, 'network.hwaddr', True)
        bridges = [x for x in context['bridges'] if x['id'] == net['bridge']]
        if len(bridges) != 1:
            raise NativeError('bridge_unavailable', field='network.bridge')
        integer(net['tag'], 1, 4094, 'network.tag', True)
        # A single PVE tag also supports legacy Linux bridges and OVS.
        # vlan_aware is not a tag capability flag. PVE selects the native
        # attachment mechanism at start; a plan must not activate it here.
        for key in ('firewall', 'link_down', 'host_managed'):
            boolean(net[key], 'network.'+key)
        if net['host_managed'] and not context['capabilities'].get('host_managed'):
            raise NativeError('capability_unavailable', field='network.host_managed')
        integer(net['mtu'], 576, 65535, 'network.mtu', True)
        number(net['rate'], 0, 10240, 'network.rate', True)
        _address(net['ipv4'], 4); _address(net['ipv6'], 6)
        if net['host_managed'] and net['ipv6']['mode'] == 'auto':
            raise NativeError('invalid_field', field='network.host_managed')
    exact(value['dns'], 'nameservers searchdomains', 'dns')
    if type(value['dns']['nameservers']) is not list or len(value['dns']['nameservers']) > 64:
        raise NativeError('invalid_field', field='dns.nameservers')
    for item in value['dns']['nameservers']:
        try:
            if type(item) is not str:
                raise ValueError()
            if '%' in item:
                address, iface = item.split('%', 1)
                if not address.lower().startswith('fe80:') or ipaddress.ip_address(address).version != 6 or not re.fullmatch(r'[A-Za-z0-9_][A-Za-z0-9_.:-]{0,14}', iface):
                    raise ValueError()
            else:
                ipaddress.ip_address(item)
        except ValueError:
            raise NativeError('invalid_address', field='dns.nameservers') from None
    if type(value['dns']['searchdomains']) is not list or len(value['dns']['searchdomains']) > 6:
        raise NativeError('invalid_field', field='dns.searchdomains')
    for item in value['dns']['searchdomains']:
        dns_name(item, 'dns.searchdomains')
    exact(value['startup'], 'order up down', 'startup')
    for key in value['startup']:
        integer(value['startup'][key], 0, 2**31-1, 'startup.'+key, True)
    exact(value['firewall'], 'mode ssh_sources log', 'firewall')
    firewall = value['firewall']
    boolean(firewall['log'], 'firewall.log')
    if firewall['mode'] not in ('inherit', 'ssh_only') or type(firewall['ssh_sources']) is not list or len(firewall['ssh_sources']) > 16:
        raise NativeError('invalid_field', field='firewall')
    if firewall['mode'] == 'ssh_only':
        if not context['capabilities'].get('firewall_active') or net is None or not net['firewall'] or not firewall['ssh_sources']:
            raise NativeError('firewall_unavailable', field='firewall')
        for source in firewall['ssh_sources']:
            try:
                if type(source) is not str or '/' not in source:
                    raise ValueError()
                ipaddress.ip_network(source, strict=True)
            except ValueError:
                raise NativeError('invalid_address', field='firewall.ssh_sources') from None
    else:
        firewall['ssh_sources'] = []; firewall['log'] = False
    if value['devices'] != []:
        raise NativeError('devices_unsupported', field='devices')
    return value


def _mount_params(mount, context, root=False):
    bind = not root and mount['kind'] == 'bind'
    source = context['bind_paths'][mount['path_id']]['path'] if bind else mount['storage']+':'+str(mount['size_gib'])
    values = [source]
    if not root:
        values += ['mp='+mount['target'], 'ro='+str(int(mount['ro'])), 'backup='+str(int(mount['backup']))]
        if mount['keepattrs']:
            values.append('keepattrs=1')
    if mount['acl'] is not None:
        values.append('acl='+str(int(mount['acl'])))
    if mount['quota']:
        values.append('quota=1')
    values.append('replicate='+str(int(mount['replicate'])))
    if mount['mount_options']:
        values.append('mountoptions='+';'.join(sorted(mount['mount_options'])))
    return ','.join(values)


def plan(raw, context):
    value = validate(raw, context)
    params = {'vmid': value['vmid'], 'ostemplate': value['template'], 'unprivileged': int(value['unprivileged']),
              'features': 'nesting='+str(int(value['nesting'])), 'start': 0, 'onboot': 0,
              'rootfs': _mount_params(value['rootfs'], context, True), **value['memory']}
    if value['ha_managed']:
        params['ha-managed'] = 1
    if value['hostname']:
        params['hostname'] = value['hostname']
    if value['pool']:
        params['pool'] = value['pool']
    if value['tags']:
        params['tags'] = ';'.join(value['tags'])
    for key, target in [('cores', 'cores'), ('limit', 'cpulimit'), ('units', 'cpuunits')]:
        if value['cpu'][key] is not None:
            params[target] = value['cpu'][key]
    for mount in value['mounts']:
        params['mp'+str(mount['id'])] = _mount_params(mount, context)
    net = value['network']
    if net is not None:
        fields = {'name': net['name'], 'bridge': net['bridge'], 'firewall': int(net['firewall']),
                  'ip': net['ipv4']['address'] if net['ipv4']['mode'] == 'static' else ('manual' if net['ipv4']['mode'] == 'none' else net['ipv4']['mode']),
                  'ip6': net['ipv6']['address'] if net['ipv6']['mode'] == 'static' else ('manual' if net['ipv6']['mode'] == 'none' else net['ipv6']['mode']),
                  'link_down': int(net['link_down'])}
        for key in ('hwaddr', 'tag', 'mtu', 'rate'):
            if net[key] not in (None, ''):
                fields[key] = net[key]
        for key, addr in [('gw', 'ipv4'), ('gw6', 'ipv6')]:
            if net[addr]['mode'] == 'static' and net[addr]['gateway']:
                fields[key] = net[addr]['gateway']
        if net['host_managed']:
            fields['host-managed'] = 1
        params['net0'] = ','.join(k+'='+str(v) for k, v in fields.items())
    if value['dns']['nameservers']:
        params['nameserver'] = ' '.join(value['dns']['nameservers'])
    if value['dns']['searchdomains']:
        params['searchdomain'] = ' '.join(value['dns']['searchdomains'])
    warnings = []
    if not value['unprivileged']:
        warnings.append('privileged')
    if any(x['kind'] == 'bind' for x in value['mounts']):
        warnings.append('bind_backup')
    firewall = value['firewall']
    rules = []
    if firewall['mode'] == 'ssh_only':
        warnings.append('firewall_restricts')
        rules = [{'type': 'in', 'action': 'ACCEPT', 'enable': 1, 'proto': 'tcp', 'dport': '22', 'source': source}
                 for source in firewall['ssh_sources']]
    final = {'onboot': int(value['onboot'])}
    startup = ','.join(k+'='+str(v) for k, v in value['startup'].items() if v is not None)
    if startup:
        final['startup'] = startup
    result = {'schema': 'native.lxc.plan.v1', 'node': value['node'], 'vmid': value['vmid'],
              'create': params, 'final_config': final, 'start': value['start'],
              'ha_managed': value['ha_managed'], 'warnings': warnings,
              'access': {'password_set': value['password_set'], 'ssh_key_count': len(value['ssh_public_keys'].splitlines()),
                         'ssh_keys_sha256': hashlib.sha256(value['ssh_public_keys'].encode()).hexdigest()},
              'firewall': {'mode': firewall['mode'], 'rules': rules,
                           'options': {'enable': 1, 'policy_in': 'DROP', 'policy_out': 'ACCEPT',
                                       'log_level_in': 'info' if firewall['log'] else 'nolog'} if rules else {}},
              'context_revision': context['revision'], 'reserved': False}
    write_policy = context.get('write_policy', {})
    if write_policy.get('state') == 'enabled':
        result['policy_revision'] = write_policy['revision']
    result['plan_hash'] = digest(result)
    return result, value
