"""Native PVE configuration semantics and prohibited-input boundaries."""
import base64
import copy
import json
import unittest

from native_lxc_fixture import context, draft
from native_lxc import NativeError, defaults, plan, strict_json


class NativeConfigurationTests(unittest.TestCase):
    def setUp(self):
        self.context = context(); self.draft = draft(self.context)

    def reject(self, field, value, code=None):
        item = copy.deepcopy(self.draft); target = item
        parts = field.split('.')
        for key in parts[:-1]:
            target = target[key]
        target[parts[-1]] = value
        with self.assertRaises(NativeError) as caught:
            plan(item, self.context)
        if code:
            self.assertEqual(caught.exception.code, code)

    def test_single_vlan_tag_accepts_legacy_and_aware_bridges_without_effects(self):
        from unittest.mock import patch
        for aware in (False, True):
            for tag in (None, 1, 100, 4094):
                with self.subTest(aware=aware, tag=tag):
                    ctx=copy.deepcopy(self.context);ctx['bridges'][0]['vlan_aware']=aware
                    value=copy.deepcopy(self.draft);value['network']['tag']=tag
                    before=copy.deepcopy((ctx,value))
                    with patch('subprocess.Popen') as process,patch('native_lxc_pve.PveAPI.request') as pve:
                        result,_=plan(value,ctx)
                        process.assert_not_called();pve.assert_not_called()
                    self.assertEqual((ctx,value),before)
                    options=dict(x.split('=',1) for x in result['create']['net0'].split(','))
                    self.assertEqual(options.get('tag'),None if tag is None else str(tag))

    def test_vlan_invalid_types_bounds_and_unknown_bridge_remain_rejected(self):
        for value in (0,-1,4095,True,False,1.5,'100','',float('inf')):
            with self.subTest(value=value):self.reject('network.tag',value,'invalid_field')
        self.reject('network.bridge','missing','bridge_unavailable')

    def test_defaults_follow_native_create_wizard_not_old_os_mockup(self):
        value = defaults(self.context)
        self.assertEqual(value['cpu']['cores'], 1)
        self.assertEqual(value['memory'], {'memory': 512, 'swap': 512})
        self.assertTrue(value['nesting']); self.assertTrue(value['unprivileged'])
        self.assertFalse(value['start']); self.assertFalse(value['onboot'])
        self.assertEqual(value['network']['name'], 'eth0')
        self.assertTrue(value['network']['firewall'])
        self.assertIsNone(value['cpu']['units'])

    def test_default_plan_has_no_secrets_or_immediate_start_and_is_pure(self):
        before = copy.deepcopy((self.context, self.draft))
        value, _ = plan(self.draft, self.context)
        self.assertEqual(value['create']['start'], 0); self.assertEqual(value['create']['onboot'], 0)
        self.assertNotIn('password', value['create']); self.assertNotIn('ssh-public-keys', value['create'])
        self.assertFalse(value['reserved']); self.assertEqual((self.context, self.draft), before)
        self.assertEqual(value['plan_hash'], plan(self.draft, self.context)[0]['plan_hash'])

    def test_native_optional_hostname_unlimited_cpu_and_host_dns(self):
        self.draft['hostname'] = ''; self.draft['cpu']['cores'] = None
        value, _ = plan(self.draft, self.context)
        for key in ('hostname', 'cores', 'cpulimit', 'cpuunits', 'nameserver', 'searchdomain'):
            self.assertNotIn(key, value['create'])

    def test_all_native_advanced_fields_are_serialized_once(self):
        d = self.draft; d.update(pool='lab', ha_managed=True, tags=['web', 'lab', 'web'], onboot=True, start=True)
        d['cpu'].update(cores=4, limit=2.5, units=200)
        d['startup'].update(order=2, up=20, down=90)
        d['rootfs'].update(acl=True, replicate=False, mount_options=['noatime', 'discard'])
        d['dns'].update(nameservers=['192.0.2.53', '2001:db8::53'], searchdomains=['lab.example'])
        n = d['network']; n.update(hwaddr='02:00:00:00:00:01', tag=30, mtu=1500, rate=12.5, link_down=True)
        n['ipv4'].update(mode='static', address='192.0.2.10/24', gateway='192.0.2.1')
        n['ipv6'].update(mode='static', address='2001:db8::10/64', gateway='2001:db8::1')
        value, _ = plan(d, self.context); p = value['create']
        for key, expected in {'pool':'lab', 'ha-managed':1, 'tags':'lab;web', 'cores':4, 'cpulimit':2.5, 'cpuunits':200, 'nameserver':'192.0.2.53 2001:db8::53', 'searchdomain':'lab.example'}.items():
            self.assertEqual(p[key], expected)
        self.assertIn('mountoptions=discard;noatime', p['rootfs'])
        for item in ('tag=30', 'gw=192.0.2.1', 'gw6=2001:db8::1', 'mtu=1500', 'rate=12.5', 'link_down=1'):
            self.assertIn(item, p['net0'])
        self.assertEqual(value['final_config'], {'onboot':1, 'startup':'order=2,up=20,down=90'})
        self.assertTrue(value['start']); self.assertEqual(p['start'], 0)

    def test_static_to_dhcp_or_slaac_discards_old_addresses_and_gateways(self):
        for family, mode in [('ipv4', 'dhcp'), ('ipv6', 'auto'), ('ipv6', 'dhcp'), ('ipv4', 'none')]:
            with self.subTest(family=family, mode=mode):
                d = copy.deepcopy(self.draft)
                d['network'][family].update(mode=mode, address='192.0.2.10/24', gateway='192.0.2.1')
                p, normal = plan(d, self.context)
                self.assertNotIn('gw=', p['create']['net0']); self.assertNotIn('gw6=', p['create']['net0'])
                self.assertEqual(normal['network'][family]['address'], '')

    def test_no_network_does_not_create_hidden_net0(self):
        self.draft['network'] = None
        self.assertNotIn('net0', plan(self.draft, self.context)[0]['create'])

    def test_ssh_public_key_accepted_without_password_but_never_in_plan(self):
        name = b'ssh-ed25519'; blob = len(name).to_bytes(4, 'big')+name+(32).to_bytes(4, 'big')+b'x'*32
        key = 'ssh-ed25519 '+base64.b64encode(blob).decode()+' example'
        self.draft.update(password_set=False, ssh_public_keys=key)
        value, normal = plan(self.draft, self.context)
        self.assertEqual(value['access']['ssh_key_count'], 1)
        self.assertNotIn(key, json.dumps(value)); self.assertEqual(normal['ssh_public_keys'], key)
        for bad in ('-----BEGIN OPENSSH PRIVATE KEY-----', 'ssh-ed25519 invalid', 'command="bad" '+key, 'ssh-rsa '+base64.b64encode(blob).decode()):
            with self.subTest(key=bad[:20]):
                self.reject('ssh_public_keys', bad, 'invalid_ssh_key')

    def test_invalid_scalars_reject_before_serializing(self):
        cases = [('vmid',True),('vmid',99),('vmid',106),('hostname','bad,$(id)'),('node','other'),('pool','missing'),('tags',['bad;tag']),('unprivileged',0),('memory.memory',15),('memory.swap',-1),('cpu.cores',0),('cpu.units',10001),('cpu.limit',float('nan')),('rootfs.size_gib',0),('rootfs.storage','local'),('template','https://example.invalid/os.tar.zst')]
        for field, value in cases:
            with self.subTest(field=field, value=value):
                self.reject(field, value)

    def test_network_validation_and_unsupported_host_managed_slaac(self):
        cases = [('network.hwaddr','xx'),('network.name','eth0,bridge=other'),('network.tag',4095),('network.mtu',575),('network.rate',-1),('network.ipv4.mode','auto')]
        for field, value in cases:
            with self.subTest(field=field): self.reject(field,value)
        self.draft['network']['ipv4'].update(mode='static',address='192.0.2.5',gateway='')
        with self.assertRaises(NativeError): plan(self.draft,self.context)
        self.draft['network']['ipv4'].update(mode='dhcp')
        self.draft['network']['host_managed']=True; self.draft['network']['ipv6']['mode']='auto'
        with self.assertRaises(NativeError): plan(self.draft,self.context)

    def test_dns_validation_covers_ipv4_ipv6_lists_and_no_urls(self):
        for values in (['https://dns.example'],['999.1.1.1'],['2001:db8::1%eth0'],['fe80::1%bad,iface'],['1.1.1.1']*65):
            with self.subTest(values=values): self.reject('dns.nameservers', values)
        self.reject('dns.searchdomains',['bad domain'])

    def test_native_linklocal_dns_interface_suffix_and_combined_dns_default_contract(self):
        self.draft['dns']['nameservers']=['fe80::1%eth0','192.0.2.53','2001:db8::53','192.0.2.54']
        value,_=plan(self.draft,self.context)
        self.assertEqual(value['create']['nameserver'],'fe80::1%eth0 192.0.2.53 2001:db8::53 192.0.2.54')
        self.draft['dns']['nameservers']=[];self.draft['dns']['searchdomains']=['lab.example']
        value,_=plan(self.draft,self.context)
        self.assertEqual(value['create']['searchdomain'],'lab.example');self.assertNotIn('nameserver',value['create'])

    def test_capability_changes_and_unknown_storage_are_not_valid_empty_results(self):
        for change in ('state', 'ha', 'capacity'):
            with self.subTest(change=change):
                c=copy.deepcopy(self.context); d=copy.deepcopy(self.draft)
                if change=='state': c['state']='unavailable'
                if change=='ha': c['capabilities']['ha']=False; d['ha_managed']=True
                if change=='capacity': c['storages'][1]['available']=None
                with self.assertRaises(NativeError): plan(d,c)

    def mount(self, **changes):
        value={'id':0,'kind':'volume','storage':'local-zfs','size_gib':8,'path_id':'','target':'/data','ro':False,'backup':True,'acl':None,'quota':False,'replicate':True,'keepattrs':False,'mount_options':[]}
        value.update(changes); return value

    def test_multiple_mounts_and_backup_semantics(self):
        self.draft['mounts']=[self.mount(),self.mount(id=2,target='/media',ro=True,backup=False)]
        value,_=plan(self.draft,self.context)
        self.assertIn('backup=1',value['create']['mp0']); self.assertIn('backup=0',value['create']['mp2'])
        self.assertIn('ro=1',value['create']['mp2']); self.assertNotIn('mp1',value['create'])

    def test_application_data_under_var_and_readonly_options_follow_pve_schema(self):
        self.draft['mounts']=[self.mount(target='/var/lib/example',ro=True,mount_options=['nodev','noexec'])]
        self.assertIn('mp=/var/lib/example',plan(self.draft,self.context)[0]['create']['mp0'])
        self.draft['mounts'][0]['mount_options']=['discard']
        with self.assertRaises(NativeError):plan(self.draft,self.context)

    def test_mount_traversal_system_paths_duplicates_and_overlaps(self):
        for path in ('/','/etc','/etc/key','/proc/a','/data/../etc','relative','/data//a','/data,backup=1'):
            with self.subTest(path=path):
                self.draft['mounts']=[self.mount(target=path)]
                with self.assertRaises(NativeError): plan(self.draft,self.context)
        for other in (self.mount(id=0,target='/other'),self.mount(id=1,target='/data'),self.mount(id=1,target='/data/sub'),self.mount(id=1,target='/data/child/other')):
            self.draft['mounts']=[self.mount(),other]
            with self.assertRaises(NativeError): plan(self.draft,self.context)

    def test_total_capacity_counts_all_disks_not_each_independently(self):
        self.context['storages'][1]['available']=15*1024**3
        self.draft['mounts']=[self.mount()]
        with self.assertRaises(NativeError) as caught: plan(self.draft,self.context)
        self.assertEqual(caught.exception.code,'capacity_insufficient')

    def test_bind_requires_verified_policy_and_never_promises_backup(self):
        self.context['bind_paths']={'shared':{'path':'/mnt/bindmounts/shared','state':'available','permissions_verified':True}}
        self.draft['mounts']=[self.mount(kind='bind',storage='',size_gib=None,path_id='shared',ro=True,backup=False)]
        value,_=plan(self.draft,self.context)
        self.assertEqual(value['warnings'],['bind_backup']); self.assertIn('backup=0',value['create']['mp0'])
        self.context['bind_paths']['shared']['permissions_verified']=False
        with self.assertRaises(NativeError): plan(self.draft,self.context)

    def test_firewall_has_explicit_dual_stack_sources_and_no_global_effects(self):
        self.draft['firewall']={'mode':'ssh_only','ssh_sources':['192.0.2.0/24','2001:db8::/64'],'log':True}
        value,_=plan(self.draft,self.context)
        self.assertEqual(len(value['firewall']['rules']),2); self.assertEqual(value['firewall']['options']['policy_in'],'DROP')
        self.assertEqual(value['warnings'],['firewall_restricts'])
        self.context['capabilities']['firewall_active']=False
        with self.assertRaises(NativeError): plan(self.draft,self.context)

    def test_unknown_fields_raw_devices_and_json_ambiguity_are_rejected(self):
        for key in ('force','restore','command','password','hostpci0','node_url'):
            value={**self.draft,key:'never'}
            with self.subTest(key=key),self.assertRaises(NativeError): plan(value,self.context)
        self.reject('devices',['/dev/dri/renderD128'],'devices_unsupported')
        for raw in (b'{"x":1,"x":2}',b'{"x":NaN}',b'\xff',b' '*131073):
            with self.subTest(raw=raw[:20]),self.assertRaises(NativeError): strict_json(raw)


if __name__=='__main__': unittest.main()
