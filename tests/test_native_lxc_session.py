"""Real Perl protocol: fresh discovery, one initialization and no write frames."""
import json, os, subprocess, sys, tempfile, time, unittest
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'AppImage/scripts'))
from native_lxc_pve import PveAPI, _ReadSession
from native_lxc import NativeError

class ReadSessionTests(unittest.TestCase):
    def fixture(self, folder):
        (folder/'PVE').mkdir();marker=folder/'calls'
        (folder/'PVE/RPCEnvironment.pm').write_text('package PVE::RPCEnvironment; sub setup_default_cli_env { open(my $f,">>","'+str(marker)+'"); print $f "init\\n"; } 1;')
        (folder/'PVE/INotify.pm').write_text('package PVE::INotify; sub nodename { "testnode" } 1;')
        (folder/'PVE/API2.pm').write_text('package PVE::API2; sub find_handler { open(my $f,">>","'+str(marker)+'"); print $f "read\\n"; return ("Fake",{}); } package Fake; sub handle { return { fresh=>time }; } 1;')
        return marker

    def test_real_two_frames_share_one_initialization_and_reject_write(self):
        bridge=Path(__file__).resolve().parents[1]/'AppImage/scripts/native_lxc_pve_bridge.pl'
        read={'reads':[{'method':'GET','path':'/version','params':{}}]}
        with tempfile.TemporaryDirectory() as tmp:
            folder=Path(tmp);marker=self.fixture(folder)
            frames=[read,read,{'method':'POST','path':'/nodes/testnode/lxc','params':{'vmid':4900}}]
            result=subprocess.run(['perl','-I',str(folder),str(bridge),'--read-session'],input=''.join(json.dumps(x)+'\n' for x in frames),text=True,capture_output=True,timeout=5)
            self.assertEqual(result.returncode,1)
            rows=[json.loads(x) for x in result.stdout.splitlines()]
            self.assertEqual([r['ok'] for r in rows],[True,True,False])
            self.assertEqual(marker.read_text().splitlines(),['init','read','read'])
            for extra in ({'method':'PUT','path':'/nodes/testnode/lxc/4900/config','params':{}},
                          {'method':'GET','path':'/nodes/other/status','params':{}}):
                marker.unlink(missing_ok=True)
                bad={'reads':read['reads']+[extra]}
                result=subprocess.run(['perl','-I',str(folder),str(bridge),'--read-session'],input=json.dumps(bad)+'\n',text=True,capture_output=True,timeout=5)
                self.assertNotEqual(result.returncode,0);self.assertFalse(marker.exists())

    def test_python_session_cleanup_success_failure_timeout_and_no_reuse(self):
        bridge=Path(__file__).resolve().parents[1]/'AppImage/scripts/native_lxc_pve_bridge.pl'
        with tempfile.TemporaryDirectory() as tmp:
            folder=Path(tmp);marker=self.fixture(folder)
            # Include test modules through a private wrapper, no environment inheritance.
            wrapper=folder/'bridge.pl';wrapper.write_text('use lib "'+str(folder)+'"; do "'+str(bridge)+'";')
            with patch('native_lxc_pve.BRIDGE',wrapper):
                pids=[]
                for _ in range(2):
                    with PveAPI().context_session() as reader:
                        for _ in range(2):self.assertTrue(reader.read_many([{'method':'GET','path':'/version','params':{}}])[0]['ok'])
                        pids.append(reader.process.pid)
                    self.assertIsNone(reader.process)
                self.assertEqual(marker.read_text().splitlines().count('init'),2)
                self.assertNotEqual(*pids)
                for pid in pids:
                    with self.assertRaises(ProcessLookupError):os.kill(pid,0)
                wrapper.write_text('sleep 10;')
                with self.assertRaises(NativeError) as caught:
                    with PveAPI().context_session() as reader:
                        reader.read_many([{'method':'GET','path':'/version','params':{}}],timeout=.05)
                self.assertEqual(caught.exception.code,'pve_timeout');self.assertIsNone(reader.process)
                for output in ('print "bad\\n";', 'print "{}\\n{}\\n";', 'exit 1;'):
                    wrapper.write_text(output)
                    with self.assertRaises(NativeError):
                        with PveAPI().context_session() as reader:
                            reader.read_many([{'method':'GET','path':'/version','params':{}}])
                    self.assertIsNone(reader.process)

    def test_frame_guard_prevents_process_creation(self):
        reader=_ReadSession()
        with patch('native_lxc_pve.subprocess.Popen') as start:
            with self.assertRaises(NativeError):reader.read_many([{'method':'POST','path':'/nodes/testnode/lxc','params':{}}])
            reader.frames=64
            with self.assertRaises(NativeError):reader.read_many([{'method':'GET','path':'/version','params':{}}])
            start.assert_not_called()

if __name__=='__main__':unittest.main()
