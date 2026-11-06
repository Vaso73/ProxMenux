"""Guest boot compatibility: let ifupdown2 inherit PVE's initial link MTUs.

This is embedded only in byte-qualified Debian templates. It runs before
networking, publishes lower-priority system defaults, and never changes a
link, address, route, user policy or interfaces file itself.
"""
import json
import os
from pathlib import Path
import re
import stat
import tempfile

POLICY_NAME = '00-proxmenux-lxc-mtu.json'
MARKER = 'ProxMenux: inherit initial LXC link MTU before ifupdown2'
IFNAME = re.compile(r'[A-Za-z0-9_][A-Za-z0-9_.:-]{0,14}')


def link_defaults(sysnet):
    values = {}
    for link in Path(sysnet).iterdir():
        if (not link.is_dir() or not IFNAME.fullmatch(link.name) or link.name == 'lo'
                or (link / 'bridge').exists() or (link / 'device').exists()):
            continue
        if int((link / 'type').read_text()) != 1:
            continue
        # PVE veth has a distinct peer ifindex; skip standalone/tunnel links.
        if (link / 'ifindex').read_text().strip() == (link / 'iflink').read_text().strip():
            continue
        mtu = int((link / 'mtu').read_text())
        if not 576 <= mtu <= 65535:
            raise ValueError('Invalid inherited link MTU')
        values[link.name] = {'mtu': str(mtu)}
    return values


def safe(path, directory=False):
    path = Path(path)
    info = path.lstat()
    if (stat.S_ISLNK(info.st_mode) or info.st_uid != os.geteuid()
            or info.st_mode & 0o022
            or not (stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode))):
        raise ValueError('Unsafe MTU policy path')
    return info


def publish(sysnet, policy_dir):
    """Rebuild only our owned policy on each boot; explicit settings win."""
    values = link_defaults(sysnet)  # Complete capture before the first write.
    policy_dir = Path(policy_dir)
    if any(p.is_symlink() for p in (policy_dir, *policy_dir.parents)):
        raise ValueError('Symlink in MTU policy path')
    # The parent belongs to ifupdown2; never follow a substituted policy dir.
    if not policy_dir.parent.exists() and not policy_dir.parent.is_symlink():
        safe(policy_dir.parent.parent, directory=True)
        policy_dir.parent.mkdir(mode=0o755)
    safe(policy_dir.parent, directory=True)
    if not policy_dir.exists() and not policy_dir.is_symlink():
        policy_dir.mkdir(mode=0o755)
    safe(policy_dir, directory=True)
    target = policy_dir / POLICY_NAME
    if target.exists() or target.is_symlink():
        if safe(target).st_size > 65536:
            raise ValueError('Oversized MTU policy')
        previous = json.loads(target.read_text())
        if (type(previous) is not dict or set(previous) != {'README', 'address'}
                or previous['README'] != MARKER):
            raise ValueError('Existing policy is not ours')
    address = {'iface_defaults': values}
    # Debian ifupdown2 classifies eth* as management links and consults this
    # fallback BEFORE iface_defaults. Native Create has one NIC. Set the
    # fallback only when all captured MTUs agree; never apply one NIC's MTU
    # to a differently sized NIC added later by an administrator.
    common = {value['mtu'] for value in values.values()}
    if len(common) == 1:
        address['module_globals'] = {'mgmt_intf_mtu': next(iter(common))}
    body = json.dumps({'README': MARKER, 'address': address},
                      sort_keys=True, indent=2) + '\n'
    temporary = None
    try:
        fd, temporary = tempfile.mkstemp(prefix='.proxmenux-mtu-', dir=policy_dir)
        with os.fdopen(fd, 'w') as stream:
            stream.write(body); stream.flush(); os.fsync(stream.fileno())
            os.fchmod(stream.fileno(), 0o644)
        os.replace(temporary, target)
        temporary = None
    finally:
        if temporary is not None:
            Path(temporary).unlink()
    return values


def main():
    if os.geteuid() != 0 or len(__import__('sys').argv) != 1:
        return 1
    try:
        publish('/sys/class/net', '/var/lib/ifupdown2/policy.d')
        return 0
    except Exception:
        # No custom paths or policy contents in the journal.
        print('Unable to preserve initial LXC MTU', file=__import__('sys').stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
