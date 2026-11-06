"""Prepare narrowly qualified OS templates; never modify a guest or the source.

Only byte-identical, previously inspected official archives may be published
world-readable for PVE's unprivileged extraction. Unknown/custom archives pass
through unchanged, not as a claim that their shutdown behaviour is verified.
The CLI accepts a PVE volume ID on stdin; cache paths/recipes are server-owned.
"""
import contextlib
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time

from lxc_template_archive import derive_plain, MAX_PLAIN

CACHE = Path('/var/cache/proxmenux-lxc-templates/v2')
RECIPE = 'debian-networking-install-mtu-v2'
# Evidence: official Debian 13.1-2 and 13.6-1 amd64 archives inspected on
# PVE. Filename alone is intentionally insufficient to qualify a template.
KNOWN = frozenset({
    'ca6dffb91c3239fedc30af2c11389198140de1f1fc3d58082f3d9f4239b92e1b',
    '1a5e43d088b430a1fca0531a2283ae2949ff0b40d383d0836d4a309e192b3244',
})
VOLUME = re.compile(r'[A-Za-z][A-Za-z0-9_.-]{0,63}:vztmpl/[A-Za-z0-9_.+-]+\.(?:tar(?:\.(?:gz|xz|zst))?|tgz)')
MAX_SOURCE = 2 * 1024**3
ZSTD = '/usr/bin/zstd'


def safe_path(path, *, mode=None, directory=False):
    path = Path(path)
    if not path.is_absolute() or '..' in path.parts:
        raise ValueError('Invalid path')
    for parent in reversed((path, *path.parents)):
        info = parent.lstat()
        if stat.S_ISLNK(info.st_mode) or info.st_uid not in (0, os.geteuid()):
            raise ValueError('Untrusted path')
        # A root-owned sticky parent (e.g. /tmp in offline fixtures) cannot
        # replace another user's owned child. All actual cache dirs are 0755.
        if info.st_mode & 0o022 and not (stat.S_ISDIR(info.st_mode) and info.st_uid == 0 and info.st_mode & stat.S_ISVTX):
            raise ValueError('Writable path')
        if parent != path and not stat.S_ISDIR(info.st_mode):
            raise ValueError('Invalid parent')
    info = path.lstat()
    if not (stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)):
        raise ValueError('Invalid file type')
    if mode is not None and stat.S_IMODE(info.st_mode) != mode:
        raise ValueError('Invalid permissions')
    return info


def file_hash(path, limit=MAX_SOURCE):
    before = safe_path(path)
    if before.st_size > limit or before.st_size == 0:
        raise ValueError('Invalid size')
    h = hashlib.sha256()
    with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW), 'rb') as stream:
        opened = os.fstat(stream.fileno())
        if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
            raise ValueError('Source changed')
        count = 0
        while chunk := stream.read(1024*1024):
            count += len(chunk)
            if count > limit:
                raise ValueError('Invalid size')
            h.update(chunk)
        after = os.fstat(stream.fileno())
    attrs = ('st_dev', 'st_ino', 'st_size', 'st_mtime_ns', 'st_ctime_ns')
    if any(getattr(before, a) != getattr(after, a) for a in attrs) or count != before.st_size:
        raise ValueError('Source changed')
    return h.hexdigest()


def mkdir(path):
    path = Path(path)
    if not path.exists() and not path.is_symlink():
        safe_path(path.parent, directory=True)
        try:
            path.mkdir(mode=0o755)
        except FileExistsError:
            # A concurrent creator may already have published the directory.
            safe_path(path, mode=0o755, directory=True)
        else:
            path.chmod(0o755)  # Readable by PVE's mapped extraction uid.
    safe_path(path, mode=0o755, directory=True)


@contextlib.contextmanager
def locked(path, timeout=30):
    fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        safe_path(path, mode=0o600)
        end = time.monotonic() + timeout
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= end:
                    raise TimeoutError('Template cache busy')
                time.sleep(.05)
        yield
    finally:
        os.close(fd)


def _decode(source, destination):
    # Qualified hashes are zstd archives. No filename guessing or extraction.
    # Unbuffered: the child inherits the FD offset, not Python's buffer cursor.
    with source.open('rb', buffering=0) as reader, destination.open('xb') as writer:
        if reader.read(4) != b'\x28\xb5\x2f\xfd':
            raise ValueError('Unexpected archive encoding')
        reader.seek(0)
        process = subprocess.Popen([ZSTD, '-q', '-d', '-c'], stdin=reader,
                                   stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        try:
            count = 0
            while chunk := process.stdout.read(1024*1024):
                count += len(chunk)
                if count > MAX_PLAIN:
                    raise ValueError('Expanded archive too large')
                writer.write(chunk)
            if process.wait(timeout=5):
                raise ValueError('Invalid compressed archive')
        finally:
            process.kill() if process.poll() is None else None
            process.wait(timeout=5)
            process.stdout.close()


def _encode(source, destination):
    with source.open('rb') as reader, destination.open('xb') as writer:
        result = subprocess.run([ZSTD, '-q', '-3', '-c'], stdin=reader,
                                stdout=writer, stderr=subprocess.DEVNULL, timeout=120)
        if result.returncode:
            raise ValueError('Compression failed')
        writer.flush(); os.fsync(writer.fileno())


def prepare(source, template, cache=CACHE):
    """Shared noninteractive preparation, called only after create admission.

    The invoking worker bounds the entire subprocess (including decompression
    and lock waiting) to 300 seconds and kills its process group on timeout.
    """
    source, cache = Path(source), Path(cache)
    source_sha = file_hash(source)
    base = {'version': 1, 'template': template, 'action': 'unchanged_unrecognized',
            'source_sha256': source_sha, 'artifact_sha256': source_sha}
    if source_sha not in KNOWN:
        return base
    mkdir(cache.parent); mkdir(cache)
    entry = cache / source_sha
    with locked(cache / (source_sha + '.lock')):
        if entry.exists() or entry.is_symlink():
            safe_path(entry, mode=0o755, directory=True)
            receipt = entry / 'receipt.json'
            if safe_path(receipt, mode=0o600).st_size > 4096:
                raise ValueError('Invalid receipt')
            data = json.loads(receipt.read_bytes())
            artifact = entry / 'template.tar.zst'
            safe_path(artifact, mode=0o644)
            if (data.get('recipe') != RECIPE or data.get('source_sha256') != source_sha
                    or data.get('artifact_sha256') != file_hash(artifact)):
                raise ValueError('Invalid cached artifact')
            return {**base, 'template': str(artifact), 'action': 'cached',
                    'artifact_sha256': data['artifact_sha256']}
        # Temporary private directory is not visible to PVE until publication.
        stage = Path(tempfile.mkdtemp(prefix='.prepare-', dir=cache))
        try:
            snapshot = stage / 'source.zst'
            with os.fdopen(os.open(source, os.O_RDONLY | os.O_NOFOLLOW), 'rb') as reader, snapshot.open('xb') as writer:
                copied = 0
                while chunk := reader.read(1024*1024):
                    copied += len(chunk)
                    if copied > MAX_SOURCE:
                        raise ValueError('Source grew')
                    writer.write(chunk)
            if file_hash(snapshot) != source_sha:
                raise ValueError('Source changed')
            plain, derived = stage / 'original.tar', stage / 'derived.tar'
            _decode(snapshot, plain)
            proof = derive_plain(plain, derived)
            artifact = stage / 'template.tar.zst'
            _encode(derived, artifact)
            artifact_sha = file_hash(artifact)
            receipt = stage / 'receipt.json'
            with receipt.open('x') as stream:
                receipt.chmod(0o600)
                json.dump({'recipe': RECIPE, 'source_sha256': source_sha,
                           'artifact_sha256': artifact_sha, **proof}, stream, sort_keys=True)
                stream.flush(); os.fsync(stream.fileno())
            for temporary in (snapshot, plain, derived):
                temporary.unlink()
            artifact.chmod(0o644); stage.chmod(0o755)
            os.rename(stage, entry)
            parent = os.open(cache, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(parent)
            finally:
                os.close(parent)
            return {**base, 'template': str(entry / 'template.tar.zst'), 'action': 'prepared',
                    'artifact_sha256': artifact_sha}
        finally:
            if stage.exists():
                shutil.rmtree(stage)


def main():
    try:
        body = sys.stdin.buffer.read(4097)
        value = json.loads(body)
        if (os.geteuid() != 0 or len(body) > 4096 or type(value) is not dict
                or set(value) != {'template'} or type(value['template']) is not str
                or not VOLUME.fullmatch(value['template'])):
            raise ValueError('Invalid request')
        result = subprocess.run(['/usr/sbin/pvesm', 'path', value['template']],
                                capture_output=True, timeout=10, check=True)
        path = result.stdout.decode().strip()
        if len(path) > 4096 or '\n' in path or '\r' in path:
            raise ValueError('Invalid volume path')
        print(json.dumps(prepare(path, value['template']), sort_keys=True))
        return 0
    except Exception:
        # No paths, credentials, command stderr or private archive data on API.
        print('{"error":"template_preparation_failed"}')
        return 1


if __name__ == '__main__':
    sys.exit(main())
