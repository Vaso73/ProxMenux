"""Read-only external input contracts, partial failures and transport guards."""
import copy
import json
from pathlib import Path
import subprocess
import os
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from native_lxc_fixture import FakePVE
from native_lxc import NativeError
from native_lxc_pve import read_context, PveAPI


class ReadAPI:
    mutations_enabled=False
    def __init__(self):self.calls=[];self.fail=None;self.overrides={}
    def request(self,method,path,params=None,**kwargs):
        self.calls.append((method,path,params or {}))
        if self.fail==path:raise NativeError('pve_unavailable',503)
        if path in self.overrides:return self.overrides[path]
        if method=='SCHEMA':return {'properties':['ha-managed','rootfs','net0'],'architecture':'x86_64','network_fields':['host-managed'],'mount_fields':['keepattrs']}
        values={
            '/version':{'version':'9.0.0','release':'1'},
            '/nodes':[{'node':'testnode','status':'online','cgroup-mode':2}],
            '/nodes/testnode/status':{'cpuinfo':{'cpus':8}},
            '/nodes/testnode/storage':[{'storage':'local','content':'vztmpl,iso','active':1,'enabled':1,'avail':20*1024**3,'type':'dir'},{'storage':'data','content':'rootdir','active':1,'enabled':1,'avail':100*1024**3,'type':'zfspool'}],
            '/nodes/testnode/storage/local/content':[{'volid':'local:vztmpl/debian-13-standard_13.0-1_amd64.tar.zst','size':1000000,'ctime':1000}],
            '/nodes/testnode/network':[{'iface':'vmbr0','type':'bridge','active':1,'bridge_vlan_aware':1},{'iface':'eno1','type':'eth','active':1}],
            '/cluster/resources':[{'type':'lxc','vmid':106,'node':'testnode'},{'type':'qemu','vmid':200,'node':'other'}],
            '/cluster/nextid':'4900','/pools':[{'poolid':'lab'}],
            '/cluster/status':[{'type':'cluster','quorate':1,'nodes':1}],
            '/nodes/testnode/services':[{'service':'pve-ha-crm','state':'running','active-state':'active'},
                                        {'service':'pve-ha-lrm','state':'running','active-state':'active'}],
        }
        if method!='GET' or path not in values:raise AssertionError((method,path))
        return copy.deepcopy(values[path])


class NativeDiscoveryTests(unittest.TestCase):
    def test_helper_timeout_kills_process_group_without_guest_or_replay(self):
        with tempfile.TemporaryDirectory() as temporary:
            pidfile=Path(temporary)/'pid'
            program="import pathlib,subprocess,sys,time;child=subprocess.Popen([sys.executable,'-c','import time;time.sleep(10)']);pathlib.Path(sys.argv[1]).write_text(str(child.pid));time.sleep(10)"
            with self.assertRaises(subprocess.TimeoutExpired):
                PveAPI._capture(b'{}',1,[sys.executable,'-c',program,str(pidfile)])
            self.assertTrue(pidfile.exists());pid=int(pidfile.read_text())
            for _ in range(30):
                try: state=Path('/proc/'+str(pid)+'/stat').read_text().rpartition(')')[2].split()[0]
                except FileNotFoundError:break
                if state=='Z':break
                time.sleep(.01)
            else:self.fail('helper descendant still active after timeout')

    def test_template_helper_never_runs_without_admission(self):
        api=PveAPI()
        with patch.object(api,'_authorize_write',side_effect=NativeError('admission_required',403)),patch.object(api,'_capture') as run:
            with self.assertRaises(NativeError):api.prepare_template('testnode',4903,'local:vztmpl/test_amd64.tar.zst')
            run.assert_not_called()

    def test_template_helper_strict_reply_and_fresh_authorization(self):
        from lxc_template_compat import CACHE,KNOWN
        api=PveAPI();template='local:vztmpl/debian_amd64.tar.zst';sha=next(iter(KNOWN))
        base={'version':1,'template':str(CACHE/sha/'template.tar.zst'),'action':'prepared',
              'source_sha256':sha,'artifact_sha256':'b'*64}
        with patch.object(api,'_authorize_write') as authorize,patch.object(api,'_capture',return_value=json.dumps(base).encode()) as run:
            self.assertEqual(api.prepare_template('testnode',4903,template),base['template'])
            self.assertEqual(authorize.call_count,2)
            self.assertEqual(json.loads(run.call_args.args[0]),{'template':template})
            self.assertEqual(run.call_args.args[1],300)
            self.assertTrue(run.call_args.args[2][1].endswith('/lxc_template_compat.py'))
        for changed in ({'template':'/etc/private'},{'source_sha256':'c'*64},{'action':'unexpected'},
                        {'version':2},{'extra':'unexpected'}):
            with self.subTest(reply=changed),patch.object(api,'_authorize_write'),patch.object(api,'_capture',return_value=json.dumps({**base,**changed}).encode()):
                with self.assertRaises(NativeError):api.prepare_template('testnode',4903,template)
        for raw in (b'',b'noise\n{}',b'{}',b'[]',b'{"version":1'):
            with patch.object(api,'_authorize_write'),patch.object(api,'_capture',return_value=raw):
                with self.assertRaises(NativeError):api.prepare_template('testnode',4903,template)
        with patch.object(api,'_authorize_write',side_effect=[None,NativeError('admission_required',403)]),patch.object(api,'_capture',return_value=json.dumps(base).encode()):
            with self.assertRaises(NativeError):api.prepare_template('testnode',4903,template)

    def test_unknown_template_passthrough_and_helper_failure(self):
        api=PveAPI();template='local:vztmpl/custom_amd64.tar.xz'
        reply={'version':1,'template':template,'action':'unchanged_unrecognized','source_sha256':'c'*64,'artifact_sha256':'c'*64}
        with patch.object(api,'_authorize_write'),patch.object(api,'_capture',return_value=json.dumps(reply).encode()):
            self.assertEqual(api.prepare_template('testnode',4903,template),template)
        for error in (subprocess.TimeoutExpired('fixture',300),ValueError('bad output'),OSError('unavailable')):
            with patch.object(api,'_authorize_write'),patch.object(api,'_capture',side_effect=error):
                with self.assertRaises(NativeError) as caught:api.prepare_template('testnode',4903,template)
                self.assertEqual(caught.exception.code,'template_preparation_failed')

    def test_write_gate_requires_local_root_approval_and_live_admission(self):
        from native_lxc_jobs import Store, process_start
        from native_lxc_pve import write_approval
        with tempfile.TemporaryDirectory() as temporary:
            folder=Path(temporary);gate=folder/'approval.json';journal=folder/'jobs.sqlite3'
            approval={'schema':'native.lxc.activation.v2','node':'testnode','host_id_sha256':'a'*64,'enabled':True,'allowed_vmids':[{'first':4900,'last':4901}],'protected_vmids':[106,108,111,112],'protected_pools':['main']}
            gate.write_text(json.dumps(approval));gate.chmod(0o600)
            # Secure descriptor ownership/mode are covered by policy fixtures.
            with patch('native_lxc_policy.machine_hash',return_value='a'*64),patch('native_lxc_policy.read_secure',return_value=gate.read_bytes()),patch('native_lxc_policy.lock_ready',return_value=True),patch('native_lxc_policy.socket.gethostname',return_value='testnode'):
                self.assertTrue(write_approval())
                with patch('native_lxc_policy.socket.gethostname',return_value='production'):
                    self.assertIsNone(write_approval())
            value={'node':'testnode','vmid':4900}
            store=Store(journal);job,_=store.admit('owner','a'*32,'hash',value,1)
            job['status']='creating';store.put(job)
            api=PveAPI()
            with patch('native_lxc_pve.write_approval',return_value=approval),patch('native_lxc_pve.JOURNAL',journal),patch.object(api,'_capture',return_value=b'{"ok":true,"data":null}') as capture:
                for method,path,params in [('POST','/nodes/testnode/lxc',{'vmid':106}),('PUT','/nodes/testnode/lxc/111/config',{}),('DELETE','/nodes/testnode/lxc/4900/config',{}),('POST','/nodes/other/lxc',{'vmid':4900}),('POST','/nodes/testnode/lxc',{'vmid':4901})]:
                    with self.assertRaises(NativeError):api.request(method,path,params)
                capture.assert_not_called()
                self.assertIsNone(api.request('POST','/nodes/testnode/lxc',{'vmid':4900}))
                self.assertEqual(capture.call_count,1)
                job['worker_pid']+=100;store.put(job)
                with self.assertRaises(NativeError):api.request('PUT','/nodes/testnode/lxc/4900/config',{})
                self.assertEqual(capture.call_count,1)
                job['worker_pid']=os.getpid();job['worker_start']='stale';store.put(job)
                with self.assertRaises(NativeError):api.request('PUT','/nodes/testnode/lxc/4900/config',{})
                self.assertEqual(capture.call_count,1)
                job['worker_start']=process_start(os.getpid());job['status']='succeeded';store.put(job)
                with self.assertRaises(NativeError):api.request('POST','/nodes/testnode/lxc/4900/status/start',{})
                self.assertEqual(capture.call_count,1)

    def test_bridge_child_process_stdout_cannot_corrupt_json(self):
        bridge=Path(__file__).resolve().parents[1]/'AppImage/scripts/native_lxc_pve_bridge.pl'
        with tempfile.TemporaryDirectory() as temporary:
            folder=Path(temporary);(folder/'PVE').mkdir()
            (folder/'PVE/RPCEnvironment.pm').write_text('package PVE::RPCEnvironment; my $ready=0; sub setup_default_cli_env { die "only one inotify instance allowed" if $ready++; } 1;')
            (folder/'PVE/INotify.pm').write_text('package PVE::INotify; sub nodename { "testnode" } 1;')
            (folder/'PVE/API2.pm').write_text('package PVE::API2; sub find_handler { return ("FakeHandler", {}); } package FakeHandler; sub handle { system("/bin/echo", "plugin child noise"); print "perl noise"; return { umask => umask() }; } 1;')
            for path in ('/version','/cluster/status','/nodes/testnode/services'):
                result=subprocess.run(['perl','-I',str(folder),str(bridge)],input=json.dumps({'method':'GET','path':path,'params':{}}).encode(),capture_output=True,timeout=5)
                self.assertEqual(result.returncode,0,result.stderr)
                self.assertEqual(json.loads(result.stdout),{'ok':True,'data':{'umask':0o022}})
                self.assertEqual(result.stderr,b'')
            for method,path in (('POST','/nodes/testnode/services/pve-ha-crm/start'),
                                ('PUT','/nodes/testnode/services'),
                                ('GET','/nodes/othernode/services')):
                result=subprocess.run(['perl','-I',str(folder),str(bridge)],input=json.dumps({'method':method,'path':path,'params':{}}).encode(),capture_output=True,timeout=5)
                self.assertNotEqual(result.returncode,0)
                self.assertNotIn(b'"data"',result.stdout)

    def test_packaged_native_modules_and_bridge_are_complete_and_importable(self):
        root=Path(__file__).resolve().parents[1]
        script=(root/'AppImage/scripts/build_appimage.sh').read_text()
        recipe='\n'.join(line for line in script.splitlines() if line.startswith('cp "$SCRIPT_DIR/') and ('.py"' in line or 'native_lxc_pve_bridge.pl"' in line))
        with tempfile.TemporaryDirectory() as temporary:
            folder=Path(temporary);(folder/'usr/bin').mkdir(parents=True)
            result=subprocess.run(['bash','-eu','-c',recipe],env={**os.environ,'SCRIPT_DIR':str(root/'AppImage/scripts'),'APP_DIR':str(folder)},capture_output=True,timeout=10)
            self.assertEqual(result.returncode,0,result.stderr)
            for name in ('native_lxc.py','native_lxc_jobs.py','native_lxc_pve.py','native_lxc_pve_bridge.pl',
                         'native_lxc_policy.py','native_lxc_activation.py',
                         'lxc_template_compat.py','lxc_template_archive.py','lxc_template_mtu.py'):
                self.assertTrue((folder/'usr/bin'/name).is_file(),name+' missing from package')
                self.assertEqual((folder/'usr/bin'/name).read_bytes(),(root/'AppImage/scripts'/name).read_bytes())
            code='import sys;sys.path.insert(0,sys.argv[1]);import native_lxc_jobs,native_lxc_pve,lxc_template_compat,lxc_template_archive;assert native_lxc_pve.BRIDGE.is_file();assert native_lxc_pve.TEMPLATE_HELPER.is_file();assert not native_lxc_jobs.Service().can_execute'
            result=subprocess.run([sys.executable,'-I','-B','-c',code,str(folder/'usr/bin')],capture_output=True,timeout=5)
            self.assertEqual(result.returncode,0,result.stderr)

    def test_real_pipe_transport_is_bounded_handles_empty_partial_and_timeout(self):
        api=PveAPI();original=subprocess.Popen
        cases=[("import sys;sys.stdin.read();print('{\"ok\":true,\"data\":[]}')",True),
               ("import sys;sys.stdin.read();print('{\"ok\":true,')",False),
               ("import sys;sys.stdin.read()",False),
               ("import sys;sys.stdin.read();sys.stdout.write('x'*5000000)",False),
               ("import time;time.sleep(2)",False)]
        for program,success in cases:
            with self.subTest(program=program):
                launch=lambda argv,**kwargs:original([sys.executable,'-c',program],**kwargs)
                with patch('native_lxc_pve.subprocess.Popen',side_effect=launch):
                    if success:self.assertEqual(api.request('GET','/version',timeout=.2),[])
                    else:
                        with self.assertRaises(NativeError):api.request('GET','/version',timeout=.2)

    def test_actual_api_shapes_templates_vmid_and_capabilities_are_normalized_without_writes(self):
        api=ReadAPI();ctx=read_context(api,'testnode')
        self.assertEqual(ctx['arch'],'amd64');self.assertEqual(ctx['nextid'],4900)
        self.assertEqual(ctx['occupied_ids'],[106,200]);self.assertEqual(ctx['templates'][0]['arch'],'amd64')
        self.assertEqual([s['id'] for s in ctx['storages'] if 'rootdir' in s['content']],['data'])
        self.assertFalse(ctx['can_execute']);self.assertFalse(ctx['capabilities']['firewall_active'])
        self.assertFalse(ctx['capabilities']['bind_mounts']);self.assertEqual(ctx['bind_paths'],{})
        self.assertTrue(all(c[0] in ('GET','SCHEMA') for c in api.calls))
        self.assertFalse(any('pveam' in str(c) or 'update' in str(c) for c in api.calls))

    def test_empty_template_list_is_valid_but_not_downloaded_implicitly(self):
        api=ReadAPI();api.overrides['/nodes/testnode/storage/local/content']=[]
        ctx=read_context(api,'testnode');self.assertEqual(ctx['templates'],[])
        self.assertFalse(ctx['can_download']);self.assertFalse(any(c[0]=='POST' for c in api.calls))

    def test_discovery_to_plan_preserves_tag_for_linux_aware_legacy_and_ovs(self):
        from native_lxc import plan
        from native_lxc_fixture import draft
        for item in ({'iface':'vmbr0','type':'bridge','active':1,'bridge_vlan_aware':1},
                     {'iface':'vmbr0','type':'bridge','active':1},
                     {'iface':'vmbr0','type':'OVSBridge','active':1}):
            with self.subTest(bridge=item):
                api=ReadAPI();api.overrides['/nodes/testnode/network']=[item]
                ctx=read_context(api,'testnode');value=draft(ctx)
                value['rootfs']['storage']='data';value['network']['tag']=100
                result,_=plan(value,ctx)
                self.assertIn('tag=100',result['create']['net0'])
                self.assertFalse(result['reserved'])
                self.assertTrue(all(method in ('GET','SCHEMA') for method,_,_ in api.calls))

    def test_real_storage_bare_tar_with_unknown_arch_does_not_hide_os_templates(self):
        api=ReadAPI();items=api.request('GET','/nodes/testnode/storage/local/content')
        items.append({'volid':'local:vztmpl/image-app_amd64_0e52bd054126cdc0.tar','size':26074624,'ctime':1000,'format':'tar'})
        api.overrides['/nodes/testnode/storage/local/content']=items
        ctx=read_context(api,'testnode')
        by_id={v['id']:v for v in ctx['templates']}
        self.assertEqual(by_id[items[0]['volid']]['arch'],'amd64')
        self.assertIsNone(by_id[items[1]['volid']]['arch'])

    def test_pve_hash_order_does_not_expire_identical_context_revision(self):
        api=ReadAPI();first=read_context(api,'testnode')
        for path in ('/nodes/testnode/storage','/nodes/testnode/network','/nodes/testnode/storage/local/content','/pools'):
            api.overrides[path]=list(reversed(api.request('GET',path)))
        for storage in api.overrides['/nodes/testnode/storage']:
            storage['content']=','.join(reversed(storage['content'].split(',')))
        second=read_context(api,'testnode')
        self.assertEqual(first['revision'],second['revision'])
        self.assertEqual(first['storages'],second['storages'])

    def test_approved_reserved_ids_are_skipped_by_nextid_and_blocked_in_context(self):
        class ActualRead(ReadAPI,PveAPI):pass
        approval={'node':'testnode','enabled':True,'protected_vmids':[4900,4901,106,108,111,112]}
        with patch('native_lxc_pve.write_approval',return_value=approval):
            context=read_context(ActualRead(),'testnode')
        self.assertEqual(context['nextid'],4902)
        self.assertIn(108,context['occupied_ids'])

    def test_optional_pool_failure_is_unavailable_not_successful_empty(self):
        api=ReadAPI();api.fail='/pools';ctx=read_context(api,'testnode')
        self.assertEqual(ctx['pool_state'],'unavailable');self.assertEqual(ctx['pools'],[])

    def test_required_probe_failure_is_not_reported_as_empty_host(self):
        for path in ('/nodes/testnode/storage','/nodes/testnode/storage/local/content','/nodes/testnode/network','/cluster/resources','/cluster/nextid'):
            with self.subTest(path=path):
                api=ReadAPI();api.fail=path
                with self.assertRaises(NativeError):read_context(api,'testnode')

    def test_malformed_or_partial_values_unknown_capacity_and_architecture(self):
        cases=[('/nodes',None),('/cluster/resources',[{'vmid':None}]),('/cluster/nextid','106'),('/nodes/testnode/storage/local/content',[{'volid':'/etc/private'}]),('/nodes/testnode/network',[{'iface':'bad,inject','type':'bridge'}])]
        for path,value in cases:
            with self.subTest(path=path):
                api=ReadAPI();api.overrides[path]=value
                with self.assertRaises(NativeError):read_context(api,'testnode')
        api=ReadAPI();orig=api.request
        def schema(method,path,params=None,**kwargs):return {'properties':[],'architecture':'unknown'} if method=='SCHEMA' else orig(method,path,params,**kwargs)
        api.request=schema
        with self.assertRaises(NativeError):read_context(api,'testnode')

    def test_capacity_changes_do_not_fake_capability_changes_but_templates_do(self):
        api=ReadAPI();first=read_context(api,'testnode')
        storage=api.request('GET','/nodes/testnode/storage');storage[1]['avail']-=4096;api.overrides['/nodes/testnode/storage']=storage
        second=read_context(api,'testnode');self.assertEqual(first['revision'],second['revision'])
        api.overrides['/nodes/testnode/storage/local/content']=[]
        self.assertNotEqual(first['revision'],read_context(api,'testnode')['revision'])

    def test_perl_bridge_denies_mutation_and_arbitrary_paths_without_loading_pve(self):
        bridge=Path(__file__).resolve().parents[1]/'AppImage/scripts/native_lxc_pve_bridge.pl'
        for request in ({'method':'POST','path':'/nodes/testnode/lxc','params':{'password':'fixture-only'}},{'method':'DELETE','path':'/nodes/testnode/lxc/106','params':{}},{'method':'GET','path':'/nodes/testnode/execute','params':{}},{'method':'GET','path':'/etc/passwd','params':{}}):
            with self.subTest(method=request['method'],path=request['path']):
                result=subprocess.run(['/usr/bin/perl',str(bridge)],input=json.dumps(request).encode(),capture_output=True,timeout=5)
                self.assertEqual(result.returncode,1);self.assertEqual(json.loads(result.stdout),{'ok':False,'error':'pve_unavailable'})
                self.assertNotIn(b'fixture-only',result.stdout+result.stderr);self.assertNotIn(b"Can't locate PVE",result.stderr)


class BatchReadTests(unittest.TestCase):
    def test_batch_rejects_non_reads_before_transport(self):
        api=PveAPI()
        for reads in ([],[{'method':'POST','path':'/nodes/testnode/lxc','params':{}}],
                      [{'method':'GET','path':'/version','params':{},'policy_revision':'wrong'}]):
            with self.subTest(reads=reads),patch.object(api,'_capture') as capture:
                with self.assertRaises(NativeError):api.read_many(reads)
                capture.assert_not_called()

    def test_real_perl_batch_loads_once_and_invalid_mixed_batch_loads_nothing(self):
        bridge=Path(__file__).resolve().parents[1]/'AppImage/scripts/native_lxc_pve_bridge.pl'
        with tempfile.TemporaryDirectory() as tmp:
            folder=Path(tmp);(folder/'PVE').mkdir();marker=folder/'loads'
            (folder/'PVE/RPCEnvironment.pm').write_text('package PVE::RPCEnvironment; sub setup_default_cli_env {} 1;')
            (folder/'PVE/INotify.pm').write_text('package PVE::INotify; sub nodename { "testnode" } 1;')
            (folder/'PVE/API2.pm').write_text('package PVE::API2; BEGIN { open(my $f, ">>", "'+str(marker)+'"); print $f "load\\n"; close($f); } sub find_handler { return ("FakeHandler", {}); } package FakeHandler; sub handle { system("/bin/echo","plugin noise"); return { fresh=>1 }; } 1;')
            good=[{'method':'GET','path':p,'params':{}} for p in ('/version','/nodes/testnode/status')]
            def run(reads):return subprocess.run(['perl','-I',str(folder),str(bridge)],input=json.dumps({'reads':reads}).encode(),capture_output=True,timeout=5)
            for extra in ({'method':'POST','path':'/nodes/testnode/lxc','params':{}},
                          {'method':'GET','path':'/etc/passwd','params':{}},
                          {'method':'GET','path':'/nodes/othernode/status','params':{}},
                          {'method':'SCHEMA','path':'/version','params':{}},
                          {'method':'GET','path':'/version','params':[]}):
                result=run(good+[extra]);self.assertNotEqual(result.returncode,0)
                self.assertFalse(marker.exists(),'no PVE initialization for a rejected batch')
            result=run(good);self.assertEqual(result.returncode,0,result.stderr)
            self.assertEqual(json.loads(result.stdout),{'ok':True,'data':[{'ok':True,'data':{'fresh':1}},{'ok':True,'data':{'fresh':1}}]})
            self.assertEqual(marker.read_text().splitlines(),['load'])

    def test_batch_context_is_fresh_per_call_and_preserves_optional_errors(self):
        api=PveAPI();fixture=ReadAPI();batches=[]
        def read_many(reads,timeout=30):
            batches.append(reads)
            out=[]
            for item in reads:
                try:out.append({'ok':True,'data':fixture.request(item['method'],item['path'],item['params'])})
                except NativeError:out.append({'ok':False,'error':'pve_unavailable'})
            return out
        with patch.object(api,'context_session',side_effect=lambda: __import__('contextlib').nullcontext(api)),patch.object(api,'read_many',side_effect=read_many),patch('native_lxc_pve.write_approval',return_value=None),patch('native_lxc_pve.policy.load_policy',return_value=None):
            first=read_context(api,'testnode')
            fixture.fail='/pools';second=read_context(api,'testnode')
        self.assertEqual(first['pool_state'],'available');self.assertEqual(second['pool_state'],'unavailable')
        self.assertEqual(len(batches),4,'two bounded read batches for each new context, no shared cache')
        self.assertTrue(all(r['method'] in ('GET','SCHEMA') for rows in batches for r in rows))

    def test_partial_batch_output_is_never_a_valid_context(self):
        api=PveAPI()
        for raw in (b'{}',b'{"ok":true,"data":[]}',b'{"ok":true,"data":[{}]}',b'{"ok":true'):
            with self.subTest(raw=raw),patch.object(api,'_capture',return_value=raw):
                with self.assertRaises(NativeError):api.read_many([{'method':'GET','path':'/version','params':{}}])

    def test_batch_matches_single_reads_with_string_flags_and_unsupported_ha(self):
        fixture=ReadAPI(); storage=fixture.request('GET','/nodes/testnode/storage')
        for item in storage:item.update(active='1',enabled='1')
        fixture.overrides['/nodes/testnode/storage']=storage
        fixture.overrides['/nodes/testnode/lxc']={'properties':['rootfs','net0'],'architecture':'x86_64'}
        expected=read_context(fixture,'testnode',clock=lambda:100)
        fixture.calls=[];api=PveAPI()
        def batch(reads,timeout=30):
            return [{'ok':True,'data':fixture.request(r['method'],r['path'],r['params'])} for r in reads]
        with patch.object(api,'context_session',side_effect=lambda: __import__('contextlib').nullcontext(api)),patch.object(api,'read_many',side_effect=batch),patch('native_lxc_pve.write_approval',return_value=None),patch('native_lxc_pve.policy.load_policy',return_value=None):
            actual=read_context(api,'testnode',clock=lambda:100)
        actual.pop('write_policy');actual['revision']=expected['revision']
        expected['defaults']['vmid']=None  # Production has no approved policy in this fixture.
        self.assertEqual(actual,expected)
        self.assertFalse(any(p.endswith('/services') or p=='/cluster/status' for _,p,_ in fixture.calls))

    def test_batch_mandatory_failure_and_timeout_never_write(self):
        api=PveAPI();fixture=ReadAPI()
        def batch(reads,timeout=30):
            return [{'ok':False,'error':'pve_unavailable'} if r['path']=='/version' else
                    {'ok':True,'data':fixture.request(r['method'],r['path'],r['params'])} for r in reads]
        with patch.object(api,'context_session',side_effect=lambda: __import__('contextlib').nullcontext(api)),patch.object(api,'read_many',side_effect=batch),patch.object(api,'request') as single:
            with self.assertRaises(NativeError):read_context(api,'testnode')
            single.assert_not_called()
        with patch.object(api,'_capture',side_effect=subprocess.TimeoutExpired('bridge',30)):
            with self.assertRaises(NativeError) as result:api.read_many([{'method':'GET','path':'/version','params':{}}])
            self.assertEqual(result.exception.code,'pve_timeout')


if __name__=='__main__':unittest.main()
