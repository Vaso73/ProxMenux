"""Preserve tar bytes and append qualified Debian network compatibility files."""
import hashlib
import io
import os
from pathlib import Path, PurePosixPath
import tarfile

DIRECTORY='etc/systemd/system/networking.service.d'
OVERLAY=DIRECTORY+'/10-shutdown-ordering.conf'
BAD='etc/systemd/system/shutdown.target.wants/networking.service'
UNIT='usr/lib/systemd/system/networking.service'
UNIT_SHA='d6f5adb74de5abd18f06e98588aa77bc96cb5c7b00785b1f0f66ab9aa15f07e1'
CONTENT=b'# Keep network startup out of the shutdown transaction.\n[Install]\nWantedBy=\nWantedBy=basic.target network.target\n'
MTU_DIRECTORY='usr/lib/proxmenux'
MTU_HELPER=MTU_DIRECTORY+'/lxc-preserve-mtu.py'
MTU_OVERLAY=DIRECTORY+'/20-inherit-lxc-mtu.conf'
MTU_UNIT=b'# Preserve the MTU already assigned by PVE before ifupdown2 starts.\n[Service]\nExecStartPre=/usr/bin/python3 -I -B /usr/lib/proxmenux/lxc-preserve-mtu.py\n'


def overlays():
    return [(DIRECTORY,tarfile.DIRTYPE,0o755,None),
            (OVERLAY,tarfile.REGTYPE,0o644,CONTENT),
            (MTU_DIRECTORY,tarfile.DIRTYPE,0o755,None),
            (MTU_HELPER,tarfile.REGTYPE,0o644,Path(__file__).with_name('lxc_template_mtu.py').read_bytes()),
            (MTU_OVERLAY,tarfile.REGTYPE,0o644,MTU_UNIT)]


def sha(path, limit=None):
    h=hashlib.sha256()
    with open(path,'rb') as stream:
        remaining=limit
        while remaining is None or remaining>0:
            data=stream.read(1024*1024 if remaining is None else min(remaining,1024*1024))
            if not data:
                if remaining not in (None,0):raise ValueError('Truncated input')
                break
            h.update(data)
            if remaining is not None:remaining-=len(data)
    return h.hexdigest()


MAX_MEMBERS = 100000
MAX_PLAIN = 8 * 1024**3


def scan(path):
    if Path(path).stat().st_size > MAX_PLAIN:
        raise ValueError('Archive too large')
    members=[];end=0
    with tarfile.open(path,'r:') as archive:
        for item in archive:
            if len(members) >= MAX_MEMBERS:
                raise ValueError('Too many archive members')
            name=item.name.removeprefix('./').rstrip('/')
            parts=PurePosixPath(name).parts
            if name.startswith('/') or '..' in parts or not name:
                raise ValueError('Unsafe archive path')
            value={key:getattr(item,key) for key in ('name','mode','uid','gid','size','mtime','linkname','uname','gname','devmajor','devminor','offset','offset_data','pax_headers','sparse')}
            value['type']=item.type.decode('ascii');value['canonical']=name
            if item.isfile():
                h=hashlib.sha256()
                stream=archive.extractfile(item)
                while chunk:=stream.read(1024*1024):h.update(chunk)
                value['sha256']=h.hexdigest()
            members.append(value);end=archive.offset
    # Tarfile.offset is the next header boundary, including any GNU sparse
    # header/data blocks. Never locate the end by trimming payload zero bytes.
    with open(path,'rb') as stream:
        stream.seek(end)
        while chunk:=stream.read(1024*1024):
            if chunk.strip(b'\0'):raise ValueError('Unexpected data after tar end')
    return members,end


def derive_plain(source,destination):
    source=Path(source);destination=Path(destination)
    if destination.exists() or destination.is_symlink():raise ValueError('Destination exists')
    original,end=scan(source)
    names=[m['canonical'] for m in original]
    if any(n==DIRECTORY or n.startswith(DIRECTORY+'/') for n in names):
        raise ValueError('Existing override requires separate review')
    if any(n==MTU_DIRECTORY or n.startswith(MTU_DIRECTORY+'/') for n in names):
        raise ValueError('Existing MTU helper requires separate review')
    if BAD in names:raise ValueError('Unexpected shutdown link in original template')
    units=[m for m in original if m['canonical']==UNIT]
    if len(units)!=1 or units[0].get('sha256')!=UNIT_SHA:
        raise ValueError('Unknown networking unit')
    for rel in ('multi-user.target','network-online.target'):
        entries=[m for m in original if m['canonical']=='etc/systemd/system/'+rel+'.wants/networking.service']
        if len(entries)!=1 or entries[0]['linkname']!='/'+UNIT:
            raise ValueError('Expected original boot link missing')
    buffer=io.BytesIO()
    with tarfile.open(fileobj=buffer,mode='w',format=tarfile.PAX_FORMAT) as overlay:
        for name,kind,mode,data in overlays():
            member=tarfile.TarInfo(name);member.type=kind;member.mode=mode
            member.uid=0;member.gid=0;member.uname='root';member.gname='root';member.mtime=0
            member.size=len(data) if data else 0
            overlay.addfile(member,io.BytesIO(data) if data else None)
    # Exclusive create, so even a race cannot replace a pre-existing path.
    with source.open('rb') as reader,destination.open('xb') as writer:
        os.chmod(destination,0o600);remaining=end
        while remaining:
            data=reader.read(min(remaining,1024*1024))
            if not data:raise ValueError('Truncated original tar')
            writer.write(data);remaining-=len(data)
        writer.write(buffer.getvalue());writer.flush();os.fsync(writer.fileno())
    result,new_end=scan(destination)
    if result[:len(original)]!=original or len(result)!=len(original)+len(overlays()):
        raise ValueError('Original tar members changed')
    if sha(source,end)!=sha(destination,end):raise ValueError('Original tar bytes changed')
    additions=result[len(original):]
    if [(m['canonical'],m['uid'],m['gid'],m['mode']) for m in additions] != [(n,0,0,mode) for n,kind,mode,data in overlays()]:
        raise ValueError('Overlay metadata mismatch')
    for actual,(_,kind,mode,data) in zip(additions,overlays()):
        if data and actual.get('sha256')!=hashlib.sha256(data).hexdigest():raise ValueError('Overlay content mismatch')
    return {'original_members':len(original),'added_members':len(overlays()),'original_tar_prefix_bytes':end,
            'original_tar_prefix_sha256':sha(source,end),'original_tar_bytes_preserved':True,
            'overlay_sha256':hashlib.sha256(CONTENT).hexdigest(),'derived_plain_sha256':sha(destination),
            'original_manifest_sha256':hashlib.sha256(__import__('json').dumps(original,sort_keys=True).encode()).hexdigest(),
            'derived_manifest_sha256':hashlib.sha256(__import__('json').dumps(result,sort_keys=True).encode()).hexdigest()}
