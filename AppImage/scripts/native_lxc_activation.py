#!/usr/bin/env python3
"""Root-only local policy administration; never imported by HTTP routes."""
import argparse
import copy
import json
import os
from pathlib import Path
import secrets
import socket
import sys
import time

import native_lxc_policy as policy
from native_lxc import NativeError, strict_json

QUIESCENCE_SECONDS = 30


def ensure_directory(folder):
    # The fixed root-owned configuration parent is verified before creation.
    parent = os.open(folder.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        info = os.fstat(parent)
        if info.st_uid != 0 or info.st_mode & 0o022:
            raise ValueError('parent')
        try:
            os.mkdir(folder.name, 0o700, dir_fd=parent)
        except FileExistsError:
            pass
    finally:
        os.close(parent)


def initialize_lock():
    ensure_directory(policy.LOCK.parent)
    # safe_fd verifies existing directory ownership/mode too.
    with policy.safe_fd(policy.LOCK, flags=os.O_RDWR | os.O_CREAT, mode=0o600):
        pass


def atomic_policy(value):
    """Under the caller's exclusive lock: verified backup then atomic replace."""
    previous = policy.read_secure(policy.POLICY)
    encoded = (json.dumps(value, sort_keys=True) + '\n').encode()
    policy.validate(value, socket.gethostname().split('.')[0], None if set(value) == {'node', 'enabled', 'protected_vmids'} else policy.machine_hash())
    parent = policy.POLICY.parent
    directory = os.open(parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    name = policy.POLICY.name + '.pending-' + secrets.token_hex(12)
    backup = policy.POLICY.name + '.backup-' + secrets.token_hex(12)
    replaced = False
    try:
        for item, content in ((backup, previous), (name, encoded)):
            fd = os.open(item, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600, dir_fd=directory)
            try:
                with os.fdopen(fd, 'wb', closefd=False) as stream:
                    stream.write(content); stream.flush(); os.fsync(fd)
            finally:
                os.close(fd)
        if policy.read_secure(parent / backup) != previous:
            raise ValueError('backup readback')
        os.fsync(directory)
        os.replace(name, policy.POLICY.name, src_dir_fd=directory, dst_dir_fd=directory)
        replaced = True
        os.fsync(directory)
        if policy.read_secure(policy.POLICY) != encoded:
            raise ValueError('readback')
        return str(parent / backup)
    except Exception:
        if replaced:
            # A verified previous file is restored atomically. If recovery
            # fails, remove only this activation target to fail closed.
            try:
                if policy.read_secure(parent / backup) != previous:
                    raise ValueError('backup readback')
                fd = os.open(name, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600, dir_fd=directory)
                try:
                    with os.fdopen(fd, 'wb', closefd=False) as stream:
                        stream.write(previous); stream.flush(); os.fsync(fd)
                finally:
                    os.close(fd)
                os.replace(name, policy.POLICY.name, src_dir_fd=directory, dst_dir_fd=directory)
                os.fsync(directory)
                if policy.read_secure(policy.POLICY) != previous:
                    raise ValueError('rollback readback')
            except Exception:
                os.unlink(policy.POLICY.name, dir_fd=directory)
                os.fsync(directory)
        raise
    finally:
        try:
            os.unlink(name, dir_fd=directory)
        except FileNotFoundError:
            pass
        os.close(directory)


def quiescent(api, store):
    from native_lxc_jobs import process_start, FINAL
    # Check every owner, not merely the current browser's history.
    conn = store._connect()
    try:
        rows = [] if conn is None else conn.execute('SELECT payload FROM jobs').fetchall()
    finally:
        if conn is not None:
            conn.close()
    # A real PVE interpreter startup is paid for every historical unresolved
    # task plus the active-task list. One shared budget, never reset per row.
    deadline = time.monotonic()+QUIESCENCE_SECONDS
    def request(path, params=None):
        remaining = deadline-time.monotonic()
        if remaining <= 0:
            raise ValueError('deadline')
        result = api.request('GET', path, params or {}, timeout=remaining)
        if time.monotonic() >= deadline:
            raise ValueError('deadline')
        return result
    for row in rows:
        job = strict_json(row[0].encode())
        if (job.get('status') not in FINAL and job.get('worker_start') is not None
                and process_start(job.get('worker_pid')) == job['worker_start']):
            raise ValueError('active worker')
        if job.get('upid') and job.get('status') not in ('succeeded', 'failed_before_effect'):
            state = request('/nodes/'+job['node']+'/tasks/'+job['upid']+'/status')
            if type(state) is not dict or state.get('status') != 'stopped':
                raise ValueError('unverified task')
    active = request('/nodes/'+socket.gethostname().split('.')[0]+'/tasks', {'source': 'active'})
    if type(active) is not list or active:
        raise ValueError('active tasks')


def configure(requested, *, disable=False, api=None, store=None):
    if os.geteuid() != 0:
        raise PermissionError('root required')
    from native_lxc_pve import PveAPI, JOURNAL
    from native_lxc_jobs import Store
    if not disable:
        policy.validate(requested, socket.gethostname().split('.')[0], policy.machine_hash())
    initialize_lock()
    with policy.policy_lock(exclusive=True):
        old = policy.load_policy()
        if old is None:
            # A first installation must still be possible; a malformed
            # existing file is never silently replaced.
            if policy.POLICY.exists():
                raise ValueError('invalid existing policy')
        if disable:
            if old is None:
                raise ValueError('no policy')
            original = strict_json(policy.read_secure(policy.POLICY))
            requested = {**original, 'enabled': False}
        else:
            policy.validate(requested, socket.gethostname().split('.')[0], policy.machine_hash())
            if old and (not set(old['protected_vmids']) <= set(requested['protected_vmids'])
                        or not set(old['protected_pools']) <= set(requested['protected_pools'])
                        or (old['legacy'] and 'main' not in requested['protected_pools'])):
                raise ValueError('protection reduction')
            quiescent(api or PveAPI(), store or Store(JOURNAL))
        # First installation has no prior bytes to back up. Create a disabled
        # v2 baseline atomically, then use the same backed-up publication path.
        if old is None:
            baseline = {**requested, 'enabled': False}
            ensure_directory(policy.POLICY.parent)
            with policy.safe_fd(policy.POLICY, flags=os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode=0o600) as fd:
                os.write(fd, (json.dumps(baseline)+'\n').encode()); os.fsync(fd)
        backup = atomic_policy(requested)
        return {'state': policy.public_policy(policy.load_policy()), 'backup': backup}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('status', 'apply', 'disable', 'initialize-lock'))
    parser.add_argument('--file', type=Path)
    parser.add_argument('--confirm', action='store_true')
    args = parser.parse_args()
    if args.action == 'status':
        print(json.dumps(policy.public_policy(policy.load_policy()))); return
    if os.geteuid() != 0 or not args.confirm:
        raise PermissionError('explicit root confirmation required')
    if args.action == 'initialize-lock':
        initialize_lock(); print('{"lock":"ready"}'); return
    requested = strict_json(policy.read_secure(args.file)) if args.action == 'apply' and args.file else None
    if args.action == 'apply' and requested is None:
        raise ValueError('policy file required')
    print(json.dumps(configure(requested, disable=args.action == 'disable')))


if __name__ == '__main__':
    try:
        main()
    except Exception:
        print('{"error":"activation_refused"}')
        sys.exit(1)
