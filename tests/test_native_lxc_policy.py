"""Activation contracts: secure files, revocation, admission and final dispatcher.

Fixtures substitute only fixed filesystem/uid/hostname constants in copies;
the shipped implementation has no request or environment path override.
"""
import copy
from contextlib import contextmanager, ExitStack
import fcntl
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time
import unittest
from unittest.mock import patch

from native_lxc_fixture import context, draft, FakePVE
from native_lxc import NativeError, plan, strict_json
from native_lxc_jobs import Service, Store
import native_lxc_policy as policy
import native_lxc_activation as admin
from native_lxc_pve import PveAPI, protected_members
from test_native_lxc_pve import ReadAPI


def document(**changes):
    return dict(schema=policy.SCHEMA, enabled=True, node='testnode',
                host_id_sha256='a'*64, allowed_vmids=[{'first':4900,'last':4902}],
                protected_vmids=[106], protected_pools=['main'], **changes)


@contextmanager
def filesystem():
    with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
        root=Path(directory); gate=root/'policy.json'; lock=root/'policy.lock'
        gate.write_text(json.dumps(document()));gate.chmod(0o600)
        lock.touch(mode=0o600)
        original=policy.safe_fd; fixture_uid=os.geteuid()
        def safe(path,**kwargs):
            return original(path,uid=fixture_uid,root=root,**kwargs)
        for name,value in [('POLICY',gate),('LOCK',lock),('safe_fd',safe)]:
            stack.enter_context(patch.object(policy,name,value))
        stack.enter_context(patch.object(policy,'machine_hash',return_value='a'*64))
        stack.enter_context(patch.object(policy.socket,'gethostname',return_value='testnode'))
        yield root,gate,lock


class PolicyTests(unittest.TestCase):
    def test_check_cannot_override_a_closed_context_when_transport_is_ready(self):
        with tempfile.TemporaryDirectory() as temporary:
            ctx = context(1000); ctx['can_execute'] = False
            api = FakePVE(); queue = []; store = Store(Path(temporary) / 'jobs.sqlite3')
            service = Service(api, store, lambda: copy.deepcopy(ctx), clock=lambda: 1000, dispatch=queue.append)
            checked = service.check('owner', draft(ctx))
            self.assertFalse(checked['can_execute'])
            self.assertEqual((queue, api.calls, api.preparations), ([], [], []))
            self.assertFalse((Path(temporary) / 'jobs.sqlite3').exists())

    def test_missing_or_unsafe_lock_is_reported_before_check_or_admission(self):
        from native_lxc_pve import read_context
        class Adapter(ReadAPI, PveAPI):
            pass
        for legacy in (False,):
            with self.subTest(legacy=legacy), filesystem() as (root, gate, lock):
                node = 'testnode'
                if legacy:
                    gate.write_text(json.dumps({'node': node, 'enabled': True, 'protected_vmids': [106]}))
                original = gate.read_bytes()
                with patch.object(policy.socket, 'gethostname', return_value=node):
                    value = policy.load_policy()
                    self.assertIsNotNone(policy.approval())
                    lock.unlink()
                    self.assertIsNone(policy.approval())
                    self.assertEqual(policy.public_policy(value)['reason'], 'lock_unavailable')
                    self.assertEqual(policy.public_policy(value)['state'], 'unavailable')
                    # ReadAPI supplies real PVE response shapes; only the local node
                    # spelling is translated for the historical legacy contract.
                    api = Adapter()
                    original_request = api.request
                    def request(method, path, params=None, **kwargs):
                        result = original_request(method, path.replace(node, 'testnode'), params, **kwargs)
                        if path == '/nodes':
                            result[0]['node'] = node
                        return result
                    api.request = request
                    ctx = read_context(api, node, clock=lambda: 1000)
                    self.assertFalse(ctx['can_execute'])
                    self.assertIsNone(ctx['defaults']['vmid'])
                    self.assertEqual(ctx['write_policy']['reason'], 'lock_unavailable')
                    d = draft(ctx); d['vmid'] = 4900; d['rootfs']['storage'] = 'data'
                    queue = []; store = Store(root / 'jobs.sqlite3')
                    fake = FakePVE(); fake.mutations_enabled = api.mutations_enabled
                    service = Service(fake, store, lambda: copy.deepcopy(ctx), clock=lambda: 1000, dispatch=queue.append)
                    checked = service.check('owner', d)
                    self.assertFalse(checked['can_execute'])
                    with self.assertRaises(NativeError):
                        service.submit('owner', {'draft': d, 'check': checked['check'], 'intent': 'c'*32,
                            'confirmations': [], 'credentials': {'password': 'fixture-only-password'}})
                    self.assertFalse(lock.exists())
                    self.assertFalse((root / 'jobs.sqlite3').exists())
                    self.assertEqual((queue, fake.calls, fake.preparations), ([], [], []))
                    self.assertEqual(gate.read_bytes(), original)
                    lock.touch(mode=0o600)
                    lock.chmod(0o640)
                    self.assertIsNone(policy.approval())
                    lock.chmod(0o600)
                    self.assertIsNotNone(policy.approval())
                    other = root / 'unsafe-link-target'; lock.rename(other); lock.symlink_to(other)
                    self.assertIsNone(policy.approval())
                    lock.unlink(); other.rename(lock)
                    lock.unlink(); os.mkfifo(lock, 0o600)
                    self.assertIsNone(policy.approval())
                    lock.unlink(); lock.touch(mode=0o600)
                    with api.admission_guard(value['revision']):
                        pass
                    # A disabled policy remains disabled even without the lock.
                    gate.write_text(json.dumps({**json.loads(original), 'enabled': False}))
                    lock.unlink()
                    self.assertEqual(policy.public_policy(policy.load_policy())['state'], 'disabled')

    def test_last_allowed_reservation_changes_hint_not_admitted_plan_revision(self):
        from native_lxc_pve import read_context
        class Adapter(ReadAPI,PveAPI):
            mutations_enabled=True
        with filesystem():
            value=policy.validate({**document(),'allowed_vmids':[{'first':4900,'last':4900}],'protected_pools':[]},'testnode','a'*64)
            api=Adapter()
            with patch.object(policy,'load_policy',return_value=value),patch.object(Store,'reserved_ids',return_value=[]):
                before=read_context(api,'testnode');d=draft(before);d['rootfs']['storage']='data'
                checked=plan(d,before)[0]
            with patch.object(policy,'load_policy',return_value=value),patch.object(Store,'reserved_ids',return_value=[4900]):
                after=read_context(api,'testnode')
                self.assertIsNone(after['defaults']['vmid']);self.assertFalse(after['can_execute'])
                self.assertEqual(after['write_policy']['reason'],'ids_exhausted')
                self.assertEqual(before['revision'],after['revision'])
                self.assertEqual(checked['plan_hash'],plan(d,after)[0]['plan_hash'])

    def test_strict_types_identity_unknown_fields_and_legacy_node(self):
        good=document();self.assertFalse(policy.validate(good,'testnode','a'*64)['legacy'])
        for change in ({'enabled':1},{'host_id_sha256':'b'*64},{'node':'other'}, {'extra':True},
                       {'protected_vmids':[True]}, {'protected_vmids':[106,106]},
                       {'protected_pools':['main','main']},{'protected_pools':['../main']},
                       {'allowed_vmids':[]},{'allowed_vmids':[{'first':99,'last':100}]},
                       {'allowed_vmids':[{'first':4900,'last':4901},{'first':4901,'last':4902}]},
                       {'allowed_vmids':[{'first':True,'last':4900}]}):
            with self.subTest(change=change),self.assertRaises(ValueError):
                policy.validate({**good,**change},'testnode','a'*64)
        old={'node':'testnode','enabled':True,'protected_vmids':[106]}
        with self.assertRaises(ValueError):policy.validate(old,'testnode',None)
        with self.assertRaises(ValueError):policy.validate({**old,'node':'testnode'},'testnode',None)
        with self.assertRaises(NativeError):strict_json(b'{"enabled":true,"enabled":false}')

    def test_secure_descriptor_rejects_modes_symlinks_parents_fifo_and_oversize(self):
        with filesystem() as (root,gate,lock):
            self.assertIsNotNone(policy.approval())
            gate.chmod(0o640);self.assertIsNone(policy.approval());gate.chmod(0o600)
            other=root/'other';gate.rename(other);gate.symlink_to(other)
            self.assertIsNone(policy.approval());gate.unlink();other.rename(gate)
            root.chmod(0o770);self.assertIsNone(policy.approval());root.chmod(0o700)
            gate.unlink();os.mkfifo(gate,0o600);self.assertIsNone(policy.approval());gate.unlink()
            gate.write_bytes(b' '*131073);gate.chmod(0o600);self.assertIsNone(policy.approval())
            gate.write_bytes(b'{');self.assertIsNone(policy.approval())
            gate.unlink();self.assertIsNone(policy.approval())

    def test_identity_read_failure_disabled_and_public_data_do_not_leak_fingerprint(self):
        with filesystem() as (_,gate,_):
            with patch.object(policy,'machine_hash',side_effect=OSError()):self.assertIsNone(policy.approval())
            gate.write_text(json.dumps({**document(),'enabled':False}))
            self.assertIsNone(policy.approval())
            public=policy.public_policy(policy.load_policy())
            self.assertEqual(public['state'],'disabled')
            self.assertNotIn('host_id_sha256',public)
            self.assertEqual(set(public),{'state','reason','revision'})

    def test_interval_edges_gaps_exhaustion_and_large_sparse_range(self):
        value=policy.validate(document(),'testnode','a'*64)
        for vmid in (4900,4902):policy.allow(value,vmid)
        for vmid in (4899,4903,106):
            with self.assertRaises(policy.PolicyDenied):policy.allow(value,vmid)
        with self.assertRaises(policy.PolicyDenied):policy.allow(value,4900,'main')
        with self.assertRaises(policy.PolicyDenied):policy.allow(value,4900,revision='stale')
        ranges=[{'first':4900,'last':4902},{'first':5000,'last':999999999}]
        self.assertEqual(policy.first_free(ranges,[4900,4901,4902,5000]),5001)
        self.assertIsNone(policy.first_free(ranges[:1],[4900,4901,4902]))

    def test_revision_canonical_protection_order_and_real_policy_change(self):
        first=policy.validate({**document(),'protected_vmids':[107,106]},'testnode','a'*64)
        second=policy.validate({**document(),'protected_vmids':[106,107]},'testnode','a'*64)
        self.assertEqual(first['revision'],second['revision'])
        changed=policy.validate({**document(),'enabled':False},'testnode','a'*64)
        self.assertNotEqual(first['revision'],changed['revision'])

    def test_exclusive_policy_change_and_admission_have_bounded_lock(self):
        with filesystem() as (_,_,lock):
            with policy.policy_lock(exclusive=True):
                started=time.monotonic()
                with self.assertRaises(policy.PolicyDenied):
                    with policy.policy_lock(timeout=.03):self.fail('admission entered')
                self.assertLess(time.monotonic()-started,.5)
            with policy.policy_lock():
                with self.assertRaises(policy.PolicyDenied):
                    with policy.policy_lock(exclusive=True,timeout=.03):self.fail('policy changed')

    def test_stale_and_revoked_admission_do_not_admit_or_dispatch(self):
        with filesystem() as (root,gate,_):
            api=PveAPI();revision=policy.approval()['revision']
            with api.admission_guard(revision):pass
            gate.write_text(json.dumps({**document(),'enabled':False}))
            with self.assertRaises(policy.PolicyDenied):
                with api.admission_guard(revision):self.fail('admitted disabled')
            gate.write_text(json.dumps({**document(),'protected_vmids':[106,107]}))
            with self.assertRaises(policy.PolicyDenied):
                with api.admission_guard(revision):self.fail('admitted stale')
            ctx=context(1000);ctx['write_policy']=policy.public_policy(policy.load_policy())
            d=draft(ctx);queue=[];fake=FakePVE();store=Store(root/'jobs.sqlite3')
            fake.validate_plan=api.validate_plan;fake.admission_guard=api.admission_guard
            service=Service(fake,store,lambda:copy.deepcopy(ctx),clock=lambda:1000,dispatch=queue.append)
            checked=service.check('owner',d)
            before=(root/'jobs.sqlite3').read_bytes() if (root/'jobs.sqlite3').exists() else None
            gate.write_text(json.dumps({**document(),'protected_vmids':[106,108]}))
            with self.assertRaises(policy.PolicyDenied):
                service.submit('owner',{'draft':d,'check':checked['check'],'intent':'a'*32,
                                       'confirmations':[],'credentials':{'password':'fixture-only-password'}})
            self.assertEqual(queue,[]);self.assertEqual(fake.calls,[]);self.assertEqual(fake.preparations,[])
            self.assertEqual((root/'jobs.sqlite3').read_bytes() if (root/'jobs.sqlite3').exists() else None,before)

    def test_queued_policy_revision_change_fails_before_preparation(self):
        with tempfile.TemporaryDirectory() as temporary:
            ctx=context(1000);ctx['write_policy']={'state':'enabled','revision':'a'*64}
            fake=FakePVE();queue=[];store=Store(Path(temporary)/'jobs.sqlite3')
            service=Service(fake,store,lambda:copy.deepcopy(ctx),clock=lambda:1000,dispatch=queue.append)
            d=draft(ctx);checked=service.check('owner',d)
            job=service.submit('owner',{'draft':d,'check':checked['check'],'intent':'b'*32,
                                     'confirmations':[],'credentials':{'password':'fixture-only-password'}})
            ctx['write_policy']['revision']='b'*64;queue.pop()()
            self.assertEqual(service.get('owner',job['id'])['status'],'failed_before_effect')
            self.assertEqual(fake.preparations,[]);self.assertEqual(fake.calls,[])
            self.assertIn(4900,store.reserved_ids('testnode'))

    def test_forbidden_force_restore_and_wrong_node_never_launch_bridge(self):
        api=PveAPI()
        with patch.object(api,'_capture') as capture:
            for params in ({'vmid':4900,'force':0},{'vmid':4900,'restore':0}):
                with self.assertRaises(policy.PolicyDenied):api.request('POST','/nodes/testnode/lxc',params)
            capture.assert_not_called()

    def test_protected_pool_empty_is_distinct_from_unreadable_or_partial(self):
        class API:
            def __init__(self,rows,pool=None):self.rows=rows;self.pool=pool;self.calls=[]
            def request(self,method,path,**kwargs):
                self.calls.append((method,path));return self.rows if path=='/pools' else self.pool
        value=policy.validate(document(),'testnode','a'*64)
        api=API([]);self.assertEqual(protected_members(api,value,time.monotonic()+1),set())
        self.assertEqual(api.calls,[('GET','/pools')])
        api=API([{'poolid':'main'}],{'members':[{'type':'lxc','vmid':4900},{'type':'storage'}]})
        self.assertEqual(protected_members(api,value,time.monotonic()+1),{4900})
        for rows,pool in ((None,None),({},None),([{}],None),([{'poolid':'main'}],{}),
                          ([{'poolid':'main'}],{'members':[{'type':'lxc'}]})):
            with self.assertRaises(NativeError):protected_members(API(rows,pool),value,time.monotonic()+1)
        api=API([])
        with self.assertRaises(NativeError):protected_members(api,value,time.monotonic()-1)
        self.assertEqual(api.calls,[])

    def test_atomic_backup_success_and_rename_failure_preserve_original(self):
        with filesystem() as (root,gate,_):
            before=gate.read_bytes();backup=admin.atomic_policy({**document(),'enabled':False})
            self.assertEqual(Path(backup).read_bytes(),before)
            self.assertFalse(json.loads(gate.read_bytes())['enabled'])
            before=gate.read_bytes()
            with patch.object(admin.os,'replace',side_effect=OSError('rename fixture')):
                with self.assertRaises(OSError):admin.atomic_policy(document())
            self.assertEqual(gate.read_bytes(),before)
            self.assertFalse(list(root.glob('*.pending-*')))

    def test_failed_readback_restores_original_and_keeps_verified_backup(self):
        with filesystem() as (root,gate,_):
            before=gate.read_bytes();read=policy.read_secure;calls=[0]
            def fail_once(path,**kwargs):
                if path==gate:
                    calls[0]+=1
                    if calls[0]==2:return b'corrupt fixture'
                return read(path,**kwargs)
            with patch.object(policy,'read_secure',side_effect=fail_once):
                with self.assertRaises(ValueError):admin.atomic_policy({**document(),'enabled':False})
            self.assertEqual(gate.read_bytes(),before)
            self.assertEqual([p.read_bytes() for p in root.glob('*.backup-*')],[before])

    def test_write_fsync_failure_never_replaces_target(self):
        with filesystem() as (_,gate,_):
            before=gate.read_bytes()
            with patch.object(admin.os,'fsync',side_effect=OSError('fsync fixture')),patch.object(admin.os,'replace') as replace:
                with self.assertRaises(OSError):admin.atomic_policy({**document(),'enabled':False})
                replace.assert_not_called()
            self.assertEqual(gate.read_bytes(),before)

    def test_rollback_failure_closes_activation_and_keeps_backup(self):
        with filesystem() as (root,gate,_):
            before=gate.read_bytes();read=policy.read_secure;count=[0]
            def broken(path,**kwargs):
                if path==gate:
                    count[0]+=1
                    if count[0]>1:raise OSError('readback unavailable')
                return read(path,**kwargs)
            with patch.object(policy,'read_secure',side_effect=broken):
                with self.assertRaises(OSError):admin.atomic_policy({**document(),'enabled':False})
            self.assertFalse(gate.exists());self.assertIsNone(policy.approval())
            self.assertEqual([p.read_bytes() for p in root.glob('*.backup-*')],[before])

    def test_first_install_and_unsupported_pilot_document_preserve_protections(self):
        with filesystem() as (root,gate,_),patch.object(admin,'initialize_lock'),patch.object(admin.os,'geteuid',return_value=0),patch.object(admin,'ensure_directory'),patch.object(admin,'quiescent'):
            gate.unlink()
            result=admin.configure(document())
            self.assertEqual(result['state']['state'],'enabled')
            self.assertFalse(json.loads(Path(result['backup']).read_bytes())['enabled'])
            old={'node':'testnode','enabled':True,'protected_vmids':[106,108]}
            gate.write_text(json.dumps(old))
            before=gate.read_bytes()
            with self.assertRaises(ValueError):admin.configure(document())
            self.assertEqual(gate.read_bytes(),before)
            self.assertIsNone(policy.approval())

    def test_disable_allowed_during_worker_but_protection_reduction_and_nonroot_denied(self):
        with filesystem() as (_,gate,_),patch.object(admin,'initialize_lock'),patch.object(admin.os,'geteuid',return_value=0):
            before=gate.read_bytes()
            with patch.object(admin,'quiescent') as quiet:
                with self.assertRaises(ValueError):admin.configure({**document(),'protected_vmids':[]})
                quiet.assert_not_called();self.assertEqual(gate.read_bytes(),before)
                self.assertEqual(admin.configure(None,disable=True)['state']['state'],'disabled')
                quiet.assert_not_called()
            with patch.object(admin.os,'geteuid',return_value=1000),patch.object(admin,'atomic_policy') as write:
                with self.assertRaises(PermissionError):admin.configure(document())
                write.assert_not_called()

    def test_quiescence_rejects_active_task_and_read_error_without_policy_write(self):
        with filesystem() as (root,gate,_),patch.object(admin,'initialize_lock'),patch.object(admin.os,'geteuid',return_value=0):
            api=FakePVE();store=Store(root/'jobs.sqlite3');before=gate.read_bytes()
            for response in (None,{},[{'upid':'active'}]):
                with patch.object(api,'request',return_value=response):
                    with self.assertRaises(ValueError):admin.configure(document(),api=api,store=store)
                self.assertEqual(gate.read_bytes(),before)
            with patch.object(api,'request',side_effect=NativeError('pve_unavailable',503)):
                with self.assertRaises(NativeError):admin.configure(document(),api=api,store=store)
            self.assertEqual(gate.read_bytes(),before)

    def test_quiescence_shared_deadline_before_at_and_after_realistic_boundary(self):
        class EmptyStore:
            def _connect(self):return None
        api=FakePVE()
        for finish,ok in ((29.999,True),(30,False),(30.001,False)):
            with patch.object(admin.time,'monotonic',side_effect=[0,0,finish]),patch.object(api,'request',return_value=[]) as read:
                if ok:admin.quiescent(api,EmptyStore())
                else:
                    with self.assertRaises(ValueError):admin.quiescent(api,EmptyStore())
                self.assertEqual(read.call_args.kwargs['timeout'],30)

    def test_real_python_perl_other_local_node_and_negative_zero_handler_calls(self):
        source=Path(__file__).resolve().parents[1]/'AppImage/scripts'
        with filesystem() as (root,gate,lock):
            for name in ('native_lxc.py','native_lxc_policy.py'):
                text=(source/name).read_text()
                if name=='native_lxc_policy.py':
                    text=text.replace("Path('/etc/proxmenux/native-lxc-web.json')",repr(gate))
                    text=text.replace("Path('/etc/proxmenux/native-lxc-policy.lock')",repr(lock))
                    text=text.replace("uid=0, root=Path('/')",'uid='+str(os.geteuid())+', root='+repr(root))
                    text=text.replace('socket.gethostname().split(\'.\')[0]',repr('testnode'))
                    text=text.replace('machine_hash())',"'"+'a'*64+"')")
                (root/name).write_text(text)
            # repr(Path) is PosixPath(...); fixture copies use Path consistently.
            for p in root.glob('*.py'):p.write_text(p.read_text().replace('PosixPath(', 'Path('))
            text=(source/'native_lxc_pve_bridge.pl').read_text().replace('/etc/proxmenux/native-lxc-policy.lock',str(lock)).replace('$info[4] == 0','$info[4] == '+str(os.geteuid()))
            bridge=root/'bridge.pl';bridge.write_text(text);(root/'PVE').mkdir();calls=root/'calls'
            (root/'PVE/RPCEnvironment.pm').write_text('package PVE::RPCEnvironment; sub setup_default_cli_env {} 1;')
            (root/'PVE/INotify.pm').write_text('package PVE::INotify; sub nodename { "testnode" } 1;')
            (root/'PVE/API2.pm').write_text('package PVE::API2; sub find_handler { return ("FakeHandler", {method=>$_[1],path=>$_[2]}); } package FakeHandler; sub handle { my ($s,$i,$p)=@_; if ($i->{path} eq "/pools") { return []; } open(my $f,">>","'+str(calls)+'") or die; print $f $i->{method}."\\n"; close $f; return "UPID:fixture"; } 1;')
            revision=policy.load_policy()['revision']
            def run(method='POST',node='testnode',vmid=4900,rev=revision,extra=None):
                body={'method':method,'path':'/nodes/'+node+'/lxc','params':{'vmid':vmid,**(extra or {})},'policy_revision':rev}
                return subprocess.run(['perl','-I',str(root),str(bridge)],input=json.dumps(body).encode(),capture_output=True,timeout=6)
            reply=run();self.assertEqual(reply.returncode,0,reply.stderr+reply.stdout)
            self.assertTrue(json.loads(reply.stdout)['ok']);self.assertEqual(calls.read_text(),'POST\n')
            before=calls.read_bytes()
            for kwargs in ({'method':'DELETE'},{'node':'other'},{'vmid':4903},{'vmid':106}, {'rev':'stale'}, {'extra':{'force':1}}, {'extra':{'restore':1}}, {'extra':{'pool':'main'}}):
                with self.subTest(kwargs=kwargs):self.assertNotEqual(run(**kwargs).returncode,0)
                self.assertEqual(calls.read_bytes(),before)
            api_source=(root/'PVE/API2.pm').read_text()
            for members in ('[{type=>"lxc",vmid=>4900}]','undef','[{type=>"lxc"}]'):
                guarded=api_source.replace('return [];','return [{poolid=>"main"}];')
                guarded=guarded.replace('open(my $f,', 'if ($i->{path} eq "/pools/main") { return {members=>'+members+'}; } open(my $f,')
                (root/'PVE/API2.pm').write_text(guarded)
                self.assertNotEqual(run().returncode,0)
                self.assertEqual(calls.read_bytes(),before,'final pool guard must precede mutating handler')
            gate.write_text(json.dumps({**document(),'enabled':False}))
            self.assertNotEqual(run().returncode,0);self.assertEqual(calls.read_bytes(),before)


if __name__=='__main__':unittest.main()
