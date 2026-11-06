"""Local opt-in policy. No HTTP input or environment can activate a writer."""
from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import socket
import stat
import sys
import time

from native_lxc import NativeError, digest, strict_json

POLICY = Path('/etc/proxmenux/native-lxc-web.json')
MACHINE = Path('/etc/machine-id')
# Persist alongside the root policy: a normal host reboot must not silently
# remove the admission lock or require a second activation.
LOCK = Path('/etc/proxmenux/native-lxc-policy.lock')
SCHEMA = 'native.lxc.activation.v2'
NODE = r'[A-Za-z0-9][A-Za-z0-9.-]{0,62}'
POOL = r'[A-Za-z][A-Za-z0-9_.-]{0,63}'


class PolicyDenied(NativeError):
    """Known refusal before dispatch, not an uncertain external task."""


@contextmanager
def safe_fd(path, *, flags=os.O_RDONLY, mode=None, uid=0, root=Path('/')):
    """Walk descriptors without following symlinks; no stat/open race."""
    relative = Path(path).relative_to(root)
    parts = relative.parts
    if not parts or any(p in ('.', '..') for p in parts):
        raise ValueError('path')
    descriptor = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in parts[:-1]:
            info = os.fstat(descriptor)
            if info.st_uid != uid or info.st_mode & 0o022:
                raise ValueError('parent')
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        info = os.fstat(descriptor)
        if info.st_uid != uid or info.st_mode & 0o022:
            raise ValueError('parent')
        fd = os.open(parts[-1], flags | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600, dir_fd=descriptor)
        try:
            info = os.fstat(fd)
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != uid
                    or (mode is not None and stat.S_IMODE(info.st_mode) != mode)
                    or (mode is None and info.st_mode & 0o022)):
                raise ValueError('file')
            yield fd
        finally:
            os.close(fd)
    finally:
        os.close(descriptor)


def read_secure(path, *, mode=0o600, limit=131072):
    with safe_fd(path, mode=mode) as fd:
        info = os.fstat(fd)
        if info.st_size > limit:
            raise ValueError('size')
        value = b''
        while len(value) <= limit:
            chunk = os.read(fd, min(8192, limit+1-len(value)))
            if not chunk:
                break
            value += chunk
        after = os.fstat(fd)
        if len(value) > limit or (info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns) != (after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns):
            raise ValueError('changed')
        return value


def machine_hash():
    raw = read_secure(MACHINE, mode=None, limit=128)
    if not re.fullmatch(rb'[a-f0-9]{32}\n?', raw):
        raise ValueError('identity')
    return hashlib.sha256(raw.strip()).hexdigest()


def ids(value, maximum=4096):
    if (type(value) is not list or len(value) > maximum
            or any(type(v) is not int or not 100 <= v <= 999999999 for v in value)
            or len(set(value)) != len(value)):
        raise ValueError('ids')
    return sorted(value)


def validate(value, node, host_hash):
    if type(value) is not dict or not re.fullmatch(NODE, node):
        raise ValueError('policy')
    keys = {'schema', 'enabled', 'node', 'host_id_sha256', 'allowed_vmids', 'protected_vmids', 'protected_pools'}
    if (set(value) != keys or value['schema'] != SCHEMA or type(value['enabled']) is not bool
            or value['node'] != node or not re.fullmatch(r'[a-f0-9]{64}', value.get('host_id_sha256', '') if type(value.get('host_id_sha256')) is str else '')
            or value['host_id_sha256'] != host_hash):
        raise ValueError('policy')
    ranges = value['allowed_vmids']
    if type(ranges) is not list or not 1 <= len(ranges) <= 64:
        raise ValueError('ranges')
    previous = 99
    for item in ranges:
        if (type(item) is not dict or set(item) != {'first', 'last'}
                or any(type(item[k]) is not int for k in ('first', 'last'))
                or not previous < item['first'] <= item['last'] <= 999999999):
            raise ValueError('ranges')
        previous = item['last']
    protected = ids(value['protected_vmids'])
    pools = value['protected_pools']
    if (type(pools) is not list or len(pools) > 64 or any(type(v) is not str or not re.fullmatch(POOL, v) for v in pools)
            or len(set(pools)) != len(pools)):
        raise ValueError('pools')
    canonical = {**value, 'protected_vmids': protected, 'protected_pools': sorted(pools)}
    return {**canonical, 'revision': digest(canonical), 'legacy': False}


def load_policy():
    try:
        value = strict_json(read_secure(POLICY), limit=131072)
        return validate(value, socket.gethostname().split('.')[0], machine_hash())
    except (OSError, ValueError, TypeError, NativeError):
        return None


def approval():
    value = load_policy()
    return value if value and value['enabled'] is True and lock_ready() else None


def lock_ready():
    """Readiness only: never create, repair or activate a policy from the web."""
    try:
        with safe_fd(LOCK, flags=os.O_RDWR, mode=0o600):
            return True
    except (OSError, ValueError):
        return False


def public_policy(value):
    if value and value['enabled'] and not lock_ready():
        return {'state': 'unavailable', 'reason': 'lock_unavailable', 'revision': value['revision']}
    return {'state': 'unavailable' if value is None else 'disabled' if not value['enabled'] else 'legacy_pilot' if value['legacy'] else 'enabled',
            'reason': 'unverified' if value is None else 'configured',
            'revision': value['revision'] if value else None}


def allow(value, vmid, pool='', revision=None):
    if not value or value['enabled'] is not True:
        raise PolicyDenied('execution_disabled', 503)
    if revision is not None and revision != value['revision']:
        raise PolicyDenied('policy_changed', 409)
    if type(vmid) is not int or not 100 <= vmid <= 999999999:
        raise PolicyDenied('invalid_field', 422, 'vmid')
    if vmid in value['protected_vmids'] or pool in value['protected_pools']:
        raise PolicyDenied('guest_protected', 403)
    if not any(r['first'] <= vmid <= r['last'] for r in value['allowed_vmids']):
        raise PolicyDenied('vmid_not_allowed', 422, 'vmid')


def first_free(ranges, occupied):
    blocked = sorted(set(occupied))
    for item in ranges:
        value = item['first']
        for used in blocked:
            if used < value:
                continue
            if used > value:
                break
            value += 1
        if value <= item['last']:
            return value
    return None


@contextmanager
def policy_lock(exclusive=False, timeout=5):
    deadline = time.monotonic() + timeout
    manager = safe_fd(LOCK, flags=os.O_RDWR, mode=0o600)
    try:
        fd = manager.__enter__()
    except (OSError, ValueError):
        raise PolicyDenied('policy_unavailable', 503) from None
    try:
        while True:
            if time.monotonic() >= deadline:
                raise PolicyDenied('policy_unavailable', 503)
            try:
                fcntl.flock(fd, (fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH) | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                time.sleep(min(.02, max(0, deadline - time.monotonic())))
            except OSError:
                raise PolicyDenied('policy_unavailable', 503) from None
        try:
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        manager.__exit__(None, None, None)


def bridge_check(body):
    if type(body) is not dict or set(body) != {'node', 'vmid', 'pool', 'revision'} or type(body['pool']) is not str:
        raise ValueError('request')
    with safe_fd(LOCK, flags=os.O_RDWR, mode=0o600):
        pass
    value = approval()
    if not value or body['node'] != value['node']:
        raise ValueError('node')
    if not value['legacy'] and body['revision'] != value['revision']:
        raise ValueError('revision')
    allow(value, body['vmid'], body['pool'])
    return {'ok': True, 'node': value['node'], 'revision': value['revision'], 'protected_pools': value['protected_pools'], 'legacy': value['legacy']}


if __name__ == '__main__':
    # Called by the fixed local Perl bridge; no credentials in argv/output.
    try:
        result = bridge_check(strict_json(sys.stdin.buffer.read(4097), limit=4096))
        print(json.dumps(result))
    except Exception:
        print('{"ok":false}')
        sys.exit(1)
