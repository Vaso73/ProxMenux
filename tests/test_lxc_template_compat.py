"""Real tar/zstd IO in a temporary tree; no PVE or guest mutation."""
import concurrent.futures
import gzip
import hashlib
import io
import json
import lzma
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import tempfile
import types
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'AppImage/scripts'))
import lxc_template_archive as archive
import lxc_template_compat as compat
import lxc_template_mtu as mtu

# Sanitized vendor file from the two qualified official Debian archives.
UNIT = b'''[Unit]
Description=Network initialization
Documentation=man:interfaces(5) man:ifup(8) man:ifdown(8)
DefaultDependencies=no
Before=shutdown.target
Conflicts=shutdown.target

[Service]
Type=oneshot
RemainAfterExit=yes
SyslogIdentifier=networking
TimeoutStopSec=30s
EnvironmentFile=/etc/default/networking
ExecStart=/usr/share/ifupdown2/sbin/start-networking start
ExecStop=/usr/share/ifupdown2/sbin/start-networking stop
ExecReload=/usr/share/ifupdown2/sbin/start-networking reload

[Install]
WantedBy=basic.target network.target shutdown.target
'''


class TemplateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        # The restricted developer sandbox maps host root-owned ancestors to
        # nobody. Preserve every real field and normalize ONLY those ancestors
        # for the fixture; production never permits an arbitrary owner.
        original_lstat = Path.lstat
        ancestors = set(Path(self.tmp.name).parents)
        def lstat(path, *args, **kwargs):
            info = original_lstat(path, *args, **kwargs)
            if path in ancestors and info.st_uid == 65534:
                values = {key:getattr(info,key) for key in dir(info) if key.startswith('st_')}
                values['st_uid'] = 0
                return types.SimpleNamespace(**values)
            return info
        self.addCleanup(patch.stopall)
        patch.object(Path,'lstat',lstat).start()
        self.root = Path(self.tmp.name); self.cache = self.root / 'cache' / 'v1'
        self.source = self.root / 'original.tar'; self.derived = self.root / 'derived.tar'
        self.volume = 'local:vztmpl/debian-13-standard_13.6-1_amd64.tar.zst'
        # A local unpacked codec may be selected by the TEST only. The production
        # helper never reads this variable, its cache root or recipe from env.
        codec = os.environ.get('PROXMENUX_TEST_ZSTD') or shutil.which('zstd')
        self.assertTrue(codec, 'Install zstd for the archive fixtures')
        patch.object(compat, 'ZSTD', codec).start()
        self.fixture()

    def fixture(self, extra=None, unit=UNIT):
        with tarfile.open(self.source, 'w', format=tarfile.PAX_FORMAT) as tar:
            for name, data in [(archive.UNIT, unit), ('zero-ending', b'a'+b'\0'*16384), ('x'*150, b'PAX pathname')]:
                item = tarfile.TarInfo(name); item.uid=42; item.gid=43
                item.mode=0o640; item.mtime=123456; item.size=len(data)
                tar.addfile(item, io.BytesIO(data))
            for target in ('multi-user.target', 'network-online.target'):
                item=tarfile.TarInfo('etc/systemd/system/'+target+'.wants/networking.service')
                item.type=tarfile.SYMTYPE; item.linkname='/'+archive.UNIT; tar.addfile(item)
            hard=tarfile.TarInfo('hard-link'); hard.type=tarfile.LNKTYPE; hard.linkname='zero-ending'; tar.addfile(hard)
            if extra: tar.addfile(extra)

    def compressed(self):
        path = self.root / 'original.tar.zst'
        compat._encode(self.source, path)
        return path

    def qualify_fixture(self, path):
        # Only the synthetic compressed fixture is added to the production
        # allowlist inside this test process. No CLI/runtime override exists.
        patch.object(compat, 'KNOWN', frozenset({compat.file_hash(path)})).start()

    def test_vendor_unit_and_recipe_match_real_contract(self):
        self.assertEqual(hashlib.sha256(UNIT).hexdigest(), archive.UNIT_SHA)
        self.assertEqual(hashlib.sha256(archive.CONTENT).hexdigest(), '835816c2cabefb634f8446d0908d64bcdb4a247c57ae81b665d8d0b3b691e700')

    def test_tar_prefix_metadata_pax_hardlink_and_source_preserved(self):
        before=archive.sha(self.source); members,end=archive.scan(self.source)
        proof=archive.derive_plain(self.source,self.derived)
        result,_=archive.scan(self.derived)
        self.assertEqual(result[:len(members)],members); self.assertEqual(len(result),len(members)+5)
        self.assertEqual(self.source.read_bytes()[:end],self.derived.read_bytes()[:end])
        self.assertEqual(archive.sha(self.source),before); self.assertTrue(proof['original_tar_bytes_preserved'])
        self.assertEqual([(m['canonical'],m['uid'],m['gid'],m['mode']) for m in result[-5:]],
                         [(n,0,0,mode) for n,kind,mode,data in archive.overlays()])

    def test_known_source_builds_once_and_verified_cache_is_reused(self):
        source=self.compressed(); self.qualify_fixture(source); before=source.read_bytes()
        first=compat.prepare(source,self.volume,self.cache)
        with patch.object(compat,'_decode',side_effect=AssertionError('must reuse')):
            second=compat.prepare(source,self.volume,self.cache)
        self.assertEqual(first['action'],'prepared'); self.assertEqual(second['action'],'cached')
        self.assertEqual(first['template'],second['template']); self.assertEqual(source.read_bytes(),before)
        artifact=Path(first['template']); self.assertEqual(artifact.stat().st_mode & 0o777,0o644)
        self.assertEqual(artifact.parent.stat().st_mode & 0o777,0o755)
        self.assertEqual((artifact.parent/'receipt.json').stat().st_mode & 0o777,0o600)
        restored=self.root/'restored.tar'; compat._decode(artifact,restored)
        original,end=archive.scan(self.source); actual,_=archive.scan(restored)
        self.assertEqual(actual[:len(original)],original)
        self.assertEqual(self.source.read_bytes()[:end],restored.read_bytes()[:end])

    def test_mtu_recipe_preserves_previous_cache_and_shutdown_fix(self):
        source=self.compressed();self.qualify_fixture(source)
        old=self.root/'cache'/'v1';old.mkdir(parents=True)
        (old/'previous-artifact').write_bytes(b'previous qualified artifact')
        before=(old/'previous-artifact').read_bytes()
        value=compat.prepare(source,self.volume,self.root/'cache'/'v2')
        self.assertEqual((old/'previous-artifact').read_bytes(),before)
        plain=self.root/'mtu.tar';compat._decode(Path(value['template']),plain)
        with tarfile.open(plain) as tar:
            self.assertEqual(tar.extractfile(archive.OVERLAY).read(),archive.CONTENT)
            self.assertEqual(tar.extractfile(archive.MTU_HELPER).read(),Path(mtu.__file__).read_bytes())
            self.assertEqual(tar.extractfile(archive.MTU_OVERLAY).read(),archive.MTU_UNIT)
        self.assertEqual(compat.RECIPE,'debian-networking-install-mtu-v2')

    def test_existing_mtu_helper_in_archive_is_refused_before_output(self):
        extra=tarfile.TarInfo(archive.MTU_HELPER);extra.size=0;self.fixture(extra)
        with self.assertRaises(ValueError):archive.derive_plain(self.source,self.derived)
        self.assertFalse(self.derived.exists())

    def test_unknown_and_already_fixed_all_encodings_pass_without_cache_or_decoder(self):
        archive.derive_plain(self.source,self.derived)
        for name,data in [('fixed',self.derived.read_bytes()),('tar',self.source.read_bytes()),
                          ('gzip',gzip.compress(self.source.read_bytes())),('xz',lzma.compress(self.source.read_bytes())),
                          ('invalid',b'custom or invalid data')]:
            path=self.root/name; path.write_bytes(data)
            with patch.object(compat,'_decode',side_effect=AssertionError('forbidden decode')):
                reply=compat.prepare(path,self.volume,self.cache)
            self.assertEqual(reply['action'],'unchanged_unrecognized'); self.assertEqual(reply['template'],self.volume)
            self.assertEqual(path.read_bytes(),data); self.assertFalse(self.cache.parent.exists())

    def test_same_filename_is_not_a_qualification(self):
        path=self.compressed()
        with patch.object(compat,'_decode') as decode:
            reply=compat.prepare(path,self.volume,self.cache)
        decode.assert_not_called(); self.assertEqual(reply['action'],'unchanged_unrecognized')

    def test_two_concurrent_preparations_publish_one_artifact(self):
        source=self.compressed(); self.qualify_fixture(source)
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
            replies=list(executor.map(lambda _:compat.prepare(source,self.volume,self.cache),range(2)))
        self.assertEqual(sorted(r['action'] for r in replies),['cached','prepared'])
        self.assertEqual(len(list(self.cache.glob('*/template.tar.zst'))),1)

    def test_changed_source_between_hash_and_snapshot_never_publishes(self):
        source=self.compressed();self.qualify_fixture(source);original_hash=compat.file_hash
        def changing(path,*args,**kwargs):
            value=original_hash(path,*args,**kwargs)
            if Path(path)==source:source.write_bytes(b'changed source')
            return value
        with patch.object(compat,'file_hash',side_effect=changing),patch.object(compat,'_decode') as decode:
            with self.assertRaises(ValueError):compat.prepare(source,self.volume,self.cache)
            decode.assert_not_called()
        self.assertEqual(list(self.cache.glob('*/template.tar.zst')),[])
        self.assertEqual(list(self.cache.glob('.prepare-*')),[])

    def test_existing_cache_recipe_or_receipt_damage_is_not_silently_rebuilt(self):
        source=self.compressed();self.qualify_fixture(source)
        result=compat.prepare(source,self.volume,self.cache);entry=Path(result['template']).parent
        receipt=entry/'receipt.json';valid=json.loads(receipt.read_text())
        for data in ('{',json.dumps({**valid,'recipe':'other-recipe'})):
            receipt.write_text(data)
            with patch.object(compat,'_encode') as encode:
                with self.assertRaises(ValueError):compat.prepare(source,self.volume,self.cache)
                encode.assert_not_called()
            self.assertTrue(Path(result['template']).is_file())

    def test_real_decoder_rejects_corruption_and_expansion_limit(self):
        path=self.root/'corrupt.zst';path.write_bytes(b'\x28\xb5\x2f\xfdtruncated')
        with self.assertRaises(ValueError):compat._decode(path,self.root/'invalid.tar')
        source=self.compressed()
        with patch.object(compat,'MAX_PLAIN',1):
            with self.assertRaises(ValueError):compat._decode(source,self.root/'too-large.tar')

    def test_source_read_error_and_empty_file_do_not_create_cache(self):
        for path in (self.root/'absent',self.root/'empty'):
            if path.name=='empty':path.touch()
            with self.assertRaises((ValueError,OSError)):compat.prepare(path,self.volume,self.cache)
            self.assertFalse(self.cache.parent.exists())

    def test_cache_tamper_is_rejected_without_replacement(self):
        source=self.compressed(); self.qualify_fixture(source)
        reply=compat.prepare(source,self.volume,self.cache); artifact=Path(reply['template'])
        artifact.write_bytes(b'tampered')
        with patch.object(compat,'_encode') as encode:
            with self.assertRaises(ValueError):compat.prepare(source,self.volume,self.cache)
        encode.assert_not_called(); self.assertEqual(artifact.read_bytes(),b'tampered')

    def test_interrupted_build_removes_only_own_stage_and_preserves_source(self):
        source=self.compressed(); self.qualify_fixture(source); before=source.read_bytes()
        for function,error in [('_decode',ValueError('corrupt')),('_encode',OSError('disk full'))]:
            with patch.object(compat,function,side_effect=error):
                with self.assertRaises((ValueError,OSError)):compat.prepare(source,self.volume,self.cache)
            self.assertEqual(source.read_bytes(),before)
            self.assertEqual(list(self.cache.glob('.prepare-*')),[])
            self.assertEqual(list(self.cache.glob('*/template.tar.zst')),[])
        self.assertEqual(compat.prepare(source,self.volume,self.cache)['action'],'prepared')

    def test_lock_timeout_never_builds_or_deletes_another_workers_state(self):
        source=self.compressed(); self.qualify_fixture(source)
        compat.mkdir(self.cache.parent); compat.mkdir(self.cache)
        lock=self.cache/(compat.file_hash(source)+'.lock')
        with compat.locked(lock):
            with self.assertRaises(TimeoutError):
                with compat.locked(lock,timeout=0):self.fail('lock admitted')
        self.assertTrue(lock.exists()); self.assertEqual(list(self.cache.glob('.prepare-*')),[])

    def test_source_symlink_parent_and_world_writable_are_refused_before_cache(self):
        source=self.compressed(); self.qualify_fixture(source)
        alias=self.root/'alias'; alias.symlink_to(source)
        with self.assertRaises(ValueError):compat.prepare(alias,self.volume,self.cache)
        folder=self.root/'folder'; folder.symlink_to(self.root,target_is_directory=True)
        with self.assertRaises(ValueError):compat.prepare(folder/source.name,self.volume,self.cache)
        source.chmod(0o666)
        with self.assertRaises(ValueError):compat.prepare(source,self.volume,self.cache)
        self.assertFalse(self.cache.parent.exists())

    def test_cache_symlink_receipt_or_bad_modes_are_refused(self):
        source=self.compressed(); self.qualify_fixture(source)
        reply=compat.prepare(source,self.volume,self.cache); entry=Path(reply['template']).parent
        receipt=entry/'receipt.json'; receipt.unlink(); receipt.symlink_to(source)
        with self.assertRaises(ValueError):compat.prepare(source,self.volume,self.cache)
        receipt.unlink(); receipt.write_text('{}'); receipt.chmod(0o600); entry.chmod(0o777)
        with self.assertRaises(ValueError):compat.prepare(source,self.volume,self.cache)

    def test_tar_unknown_unit_duplicate_override_traversal_and_shutdown_link_refused(self):
        for name in (archive.OVERLAY,archive.UNIT,'../escape',archive.BAD):
            with self.subTest(name=name):
                self.fixture(tarfile.TarInfo(name))
                with self.assertRaises(ValueError):archive.derive_plain(self.source,self.derived)
                self.assertFalse(self.derived.exists())
        self.fixture(unit=b'other unit')
        with self.assertRaises(ValueError):archive.derive_plain(self.source,self.derived)

    def test_tar_limits_truncation_and_nonzero_trailer_refused(self):
        with patch.object(archive,'MAX_MEMBERS',1):
            with self.assertRaises(ValueError):archive.derive_plain(self.source,self.derived)
        with self.source.open('ab') as stream:stream.write(b'not a tar trailer')
        with self.assertRaises(ValueError):archive.derive_plain(self.source,self.derived)
        self.source.write_bytes(b'broken tar')
        with self.assertRaises((ValueError,tarfile.TarError)):archive.derive_plain(self.source,self.derived)
        self.assertFalse(self.derived.exists())

    def test_cli_rejects_unprivileged_extra_fields_and_arbitrary_paths_before_resolver(self):
        for uid,body in [(1000,{'template':self.volume}),(0,{'template':'/private/file'}),
                         (0,{'template':self.volume,'cache':'/tmp/override'}),(0,{'template':self.volume+'\n'})]:
            fake=type('Input',(),{'buffer':io.BytesIO(json.dumps(body).encode())})()
            with patch.object(compat.os,'geteuid',return_value=uid),patch.object(compat.sys,'stdin',fake),patch('sys.stdout',new=io.StringIO()),patch.object(compat.subprocess,'run') as run:
                self.assertEqual(compat.main(),1);run.assert_not_called()

    def test_cli_resolves_only_volume_and_returns_sanitized_failure(self):
        fake=type('Input',(),{'buffer':io.BytesIO(json.dumps({'template':self.volume}).encode())})()
        with patch.object(compat.os,'geteuid',return_value=0),patch.object(compat.sys,'stdin',fake),patch('sys.stdout',new=io.StringIO()) as output,patch.object(compat.subprocess,'run',return_value=subprocess.CompletedProcess([],0,b'/secret/source\n',b'')) as run,patch.object(compat,'prepare',side_effect=OSError('private data')):
            self.assertEqual(compat.main(),1)
            self.assertEqual(json.loads(output.getvalue()),{'error':'template_preparation_failed'})
            self.assertEqual(run.call_args.args[0],['/usr/sbin/pvesm','path',self.volume])


class InheritedMTUTests(unittest.TestCase):
    def setUp(self):
        temporary=tempfile.TemporaryDirectory();self.addCleanup(temporary.cleanup)
        self.root=Path(temporary.name);self.sysnet=self.root/'net';self.sysnet.mkdir()
        self.policy=self.root/'ifupdown2'/'policy.d';self.policy.parent.mkdir()

    def link(self,name,mtu_value,kind=1,peer=True,device=False,bridge=False):
        folder=self.sysnet/name;folder.mkdir()
        for file,value in {'mtu':mtu_value,'type':kind,'ifindex':2,'iflink':401 if peer else 2}.items():
            (folder/file).write_text(str(value)+'\n')
        if device:(folder/'device').mkdir()
        if bridge:(folder/'bridge').mkdir()

    def test_pve_mtu_captured_before_ifupdown_without_link_or_address_mutation(self):
        self.link('eth0',1400);self.link('eth1',1280)
        self.link('lo',65536,kind=772);self.link('physical',9000,device=True)
        self.link('bridge0',1500,bridge=True);self.link('standalone',1500,peer=False)
        before={str(p):p.read_bytes() for p in self.sysnet.rglob('*') if p.is_file()}
        with patch('subprocess.run') as run,patch('subprocess.Popen') as popen:
            value=mtu.publish(self.sysnet,self.policy);run.assert_not_called();popen.assert_not_called()
        self.assertEqual(value,{'eth0':{'mtu':'1400'},'eth1':{'mtu':'1280'}})
        self.assertEqual(before,{str(p):p.read_bytes() for p in self.sysnet.rglob('*') if p.is_file()})
        data=json.loads((self.policy/mtu.POLICY_NAME).read_text())
        self.assertEqual(data,{'README':mtu.MARKER,'address':{'iface_defaults':value}})

    def test_reboot_replaces_stale_own_mtu_and_removes_disappeared_links(self):
        self.link('eth0',1400);mtu.publish(self.sysnet,self.policy)
        (self.sysnet/'eth0/mtu').write_text('1450\n');self.link('eth1',1500)
        value=mtu.publish(self.sysnet,self.policy)
        self.assertEqual(value['eth0'],{'mtu':'1450'});self.assertEqual(value['eth1'],{'mtu':'1500'})
        shutil.rmtree(self.sysnet/'eth0');mtu.publish(self.sysnet,self.policy)
        self.assertEqual(json.loads((self.policy/mtu.POLICY_NAME).read_text())['address']['iface_defaults'],{'eth1':{'mtu':'1500'}})

    def test_real_sysfs_bonding_masters_control_file_is_not_a_network_link(self):
        # Actual LXC sysfs contains this regular control file beside eth0/lo.
        control=self.sysnet/'bonding_masters';control.write_text('\n')
        self.link('eth0',1400)
        self.assertEqual(mtu.publish(self.sysnet,self.policy),{'eth0':{'mtu':'1400'}})
        self.assertEqual(control.read_text(),'\n')

    def test_eth_management_fallback_inherits_single_native_nic_mtu(self):
        # Actual process_mtu checks MGMT_INTF's fallback before iface_defaults.
        self.link('eth0',1400);mtu.publish(self.sysnet,self.policy)
        policy=json.loads((self.policy/mtu.POLICY_NAME).read_text())['address']
        self.assertEqual(policy['module_globals']['mgmt_intf_mtu'],'1400')
        self.assertEqual(policy['iface_defaults']['eth0']['mtu'],'1400')

    def test_divergent_later_nics_never_receive_one_global_mtu(self):
        self.link('eth0',1400);self.link('eth1',1280)
        mtu.publish(self.sysnet,self.policy)
        policy=json.loads((self.policy/mtu.POLICY_NAME).read_text())['address']
        self.assertNotIn('module_globals',policy)
        self.assertEqual(policy['iface_defaults'],{'eth0':{'mtu':'1400'},'eth1':{'mtu':'1280'}})

    def test_invalid_kernel_mtu_has_no_policy_write_or_directory_create(self):
        self.link('eth0',1400);self.link('eth1',1)
        with self.assertRaises(ValueError):mtu.publish(self.sysnet,self.policy)
        self.assertFalse(self.policy.exists())

    def test_explicit_user_policies_and_interfaces_are_never_touched(self):
        self.link('eth0',1400);self.policy.mkdir()
        custom=self.policy/'customer.json';custom.write_text('{"address":{"defaults":{"mtu":"9000"}}}')
        interfaces=self.root/'interfaces';interfaces.write_text('iface eth0 inet dhcp\n mtu 1300\n')
        original={p:p.read_bytes() for p in (custom,interfaces)}
        mtu.publish(self.sysnet,self.policy)
        self.assertEqual(original,{p:p.read_bytes() for p in original})

    def test_unknown_or_symlink_policy_refused_without_overwriting_target(self):
        self.link('eth0',1400);self.policy.mkdir();target=self.policy/mtu.POLICY_NAME
        target.write_text('{"address":{}}');before=target.read_bytes()
        with self.assertRaises(ValueError):mtu.publish(self.sysnet,self.policy)
        self.assertEqual(target.read_bytes(),before)
        target.unlink();outside=self.root/'outside';outside.write_text('keep');target.symlink_to(outside)
        with self.assertRaises(ValueError):mtu.publish(self.sysnet,self.policy)
        self.assertEqual(outside.read_text(),'keep');self.assertTrue(target.is_symlink())

    def test_symlink_policy_directory_refused_without_outside_writes(self):
        self.link('eth0',1400);outside=self.root/'outside';outside.mkdir();self.policy.symlink_to(outside,target_is_directory=True)
        with self.assertRaises(ValueError):mtu.publish(self.sysnet,self.policy)
        self.assertEqual(list(outside.iterdir()),[])

    def test_failed_atomic_publish_keeps_previous_policy(self):
        self.link('eth0',1400);mtu.publish(self.sysnet,self.policy)
        target=self.policy/mtu.POLICY_NAME;before=target.read_bytes();(self.sysnet/'eth0/mtu').write_text('1450')
        with patch.object(mtu.os,'replace',side_effect=OSError('fixture refusal')):
            with self.assertRaises(OSError):mtu.publish(self.sysnet,self.policy)
        self.assertEqual(target.read_bytes(),before);self.assertEqual([p.name for p in self.policy.iterdir()],[mtu.POLICY_NAME])

    def test_networking_unit_orders_capture_before_original_start(self):
        self.assertIn(b'ExecStartPre=/usr/bin/python3 -I -B /usr/lib/proxmenux/lxc-preserve-mtu.py',archive.MTU_UNIT)
        self.assertNotIn(b'ExecStart=',archive.MTU_UNIT)
        self.assertIn(b'WantedBy=basic.target network.target\n',archive.CONTENT)


if __name__=='__main__':unittest.main()
