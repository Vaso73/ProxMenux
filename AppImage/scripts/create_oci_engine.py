"""Monitor transport to the root-owned existing OCI engine wizard."""
import importlib.util
from pathlib import Path
import stat
import sys

ENTRY = Path('/usr/local/share/proxmenux/oci/engine/remote/oci_web_wizard.py')


def catalog():
    """Read native installation choices without starting its CLI or worker."""
    root = ENTRY.parent.parent
    entry = root / 'src/proxmenux_oci/catalog_selection.py'
    for path in (entry, *entry.parents):
        if not path.is_relative_to(root):
            break
        value = path.lstat()
        if value.st_uid != 0 or stat.S_ISLNK(value.st_mode) or value.st_mode & 0o022:
            raise RuntimeError('engine_unavailable')
    spec = importlib.util.spec_from_file_location('_proxmenux_native_catalog_selection', entry)
    module = importlib.util.module_from_spec(spec)
    exec(compile(entry.read_bytes(), str(entry), 'exec'), module.__dict__)
    return module.read_catalog(root)


def engine():
    for path in (ENTRY, ENTRY.parent, ENTRY.parent.parent):
        value = path.lstat()
        if value.st_uid != 0 or stat.S_ISLNK(value.st_mode) or value.st_mode & 0o022:
            raise RuntimeError('engine_unavailable')
    name = '_proxmenux_existing_engine_web_wizard'
    if name not in sys.modules:
        spec = importlib.util.spec_from_file_location(name, ENTRY)
        module = importlib.util.module_from_spec(spec)
        # A metadata GET must not create an engine bytecode cache.
        exec(compile(ENTRY.read_bytes(), str(ENTRY), 'exec'), module.__dict__)
        sys.modules[name] = module
    return sys.modules[name]


def manager():
    """Load the distinct manager contract without redefining the v1 installer."""
    entry = ENTRY.with_name('oci_web_manager.py')
    for path in (entry, entry.parent, entry.parent.parent):
        value = path.lstat()
        if value.st_uid != 0 or stat.S_ISLNK(value.st_mode) or value.st_mode & 0o022:
            raise RuntimeError('engine_unavailable')
    source = str(entry.parent)
    # Read endpoints must not populate native __pycache__ directories.
    sys.dont_write_bytecode = True
    if source not in sys.path:
        sys.path.insert(0, source)
    name = '_proxmenux_native_web_manager'
    if name not in sys.modules:
        spec = importlib.util.spec_from_file_location(name, entry)
        module = importlib.util.module_from_spec(spec)
        exec(compile(entry.read_bytes(), str(entry), 'exec'), module.__dict__)
        sys.modules[name] = module
    return sys.modules[name]
