// Exercise the shipped response schemas against the actual Python planner.
const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm'),cp=require('node:child_process'),ts=require('typescript');
const exportsObject={};
vm.runInNewContext(ts.transpileModule(fs.readFileSync('AppImage/lib/native-lxc.ts','utf8'),{compilerOptions:{module:ts.ModuleKind.CommonJS}}).outputText,{exports:exportsObject,require,Set});
const model=exportsObject;
const raw=cp.execFileSync('python3',['-B','-c','import sys,json;sys.path.insert(0,"tests");from native_lxc_fixture import context,draft;from native_lxc import plan;c=context();c["bridges"][0]["vlan_aware"]=False;d=draft(c);d["network"]["tag"]=100;p,_=plan(d,c);print(json.dumps({"context":c,"draft":d,"plan":p}))'],{encoding:'utf8'});
const sample=JSON.parse(raw),context=model.contextSchema.parse(sample.context),draft=model.draftSchema.parse(sample.draft);
const plan=model.planSchema.parse(sample.plan);
for(const state of ['enabled','disabled','unavailable','legacy_pilot']){
 const current=structuredClone(sample.context);current.write_policy={state,reason:'configured',revision:'c'.repeat(64)};
 if(state==='disabled'){current.defaults.vmid=null;current.can_execute=false;}
 assert.equal(model.contextSchema.parse(current).write_policy.state,state);
}
const exhausted=structuredClone(sample.context);exhausted.defaults.vmid=null;exhausted.can_execute=false;
exhausted.write_policy={state:'enabled',reason:'ids_exhausted',revision:'c'.repeat(64)};
assert.equal(model.contextSchema.parse(exhausted).defaults.vmid,null);
assert.equal(model.planSchema.parse({...sample.plan,policy_revision:'c'.repeat(64)}).policy_revision,'c'.repeat(64));
assert.equal(model.planSchema.safeParse({...sample.plan,policy_revision:'invalid'}).success,false);
assert.equal(model.haReady(context),true);
const oldContext=structuredClone(sample.context);delete oldContext.ha_readiness;
assert.equal(model.haReady(model.contextSchema.parse(oldContext)),false,'legacy context cannot imply HA readiness');
for(const readiness of [{state:'unknown',reason:'unverified'},{state:'unavailable',reason:'services_inactive'},{state:'unavailable',reason:'no_quorum'},{state:'ready',reason:'unverified'}]){
 const unavailable=structuredClone(sample.context);unavailable.ha_readiness=readiness;
 assert.equal(model.haReady(model.contextSchema.parse(unavailable)),false);
}
assert(context.storages.every(s=>s.acl_disable===false),'legacy context cannot imply ACL disable');
const qualified=structuredClone(sample.context);qualified.storages[0].acl_disable=true;
assert.equal(model.contextSchema.parse(qualified).storages[0].acl_disable,true);
qualified.storages[0].acl_disable='true';assert.equal(model.contextSchema.safeParse(qualified).success,false);
const legacyDraft=structuredClone(sample.draft);legacyDraft.rootfs.acl=false;
assert.equal(model.draftSchema.parse(legacyDraft).rootfs.acl,false,'legacy draft remains readable');
const legacyPlan=structuredClone(sample.plan);legacyPlan.create.rootfs+=',acl=0';
assert(model.planSchema.parse(legacyPlan).create.rootfs.includes('acl=0'),'history schema must not validate new admission');
assert.equal(model.nativeSteps.length,8);
assert.deepEqual(Array.from(model.nativeSteps),['general','template','disks','cpu','memory','network','dns','confirm']);
assert.equal(plan.create.start,0);assert.equal(plan.create.onboot,0);
assert.equal(context.bridges[0].vlan_aware,false);assert.equal(draft.network.tag,100);assert(plan.create.net0.includes('tag=100'));
const check={plan,check:{owner:'owner',plan_hash:plan.plan_hash,expires_at:120,signature:'a'.repeat(64)},expires_at:120,server_time:0,can_execute:false};
assert.equal(model.checkMatches(model.checkSchema.parse(check),draft,context),true);
for(const mutation of [c=>c.check.plan_hash='b'.repeat(64),c=>c.expires_at=121,c=>c.plan.node='other',c=>c.plan.vmid=106,c=>c.plan.context_revision='b'.repeat(64),c=>c.server_time=121]){
 const other=structuredClone(check);mutation(other);assert.equal(model.checkMatches(other,draft,context),false);
}
for(const mutation of [c=>c.defaults=null,c=>c.capabilities=null,c=>c.templates=[null],c=>c.storages=[{id:'broken'}],c=>c.observed_at='now',c=>c.arch=null,c=>c.can_execute='true',c=>c.defaults.network.ipv4=null]){
 const broken=structuredClone(context);mutation(broken);assert.equal(model.contextSchema.safeParse(broken).success,false);
}
for(const key of ['password','ssh-public-keys','force','restore']){
 const unsafe=structuredClone(plan);unsafe.create[key]='never';assert.equal(model.planSchema.safeParse(unsafe).success,false);
}
const mount=model.newMount(2);assert.equal(mount.id,2);assert.equal(mount.backup,true);assert.equal(mount.target,'/data2');
const job={id:'a'.repeat(32),intent:'b'.repeat(32),node:'testnode',vmid:4900,status:'unknown',phase:'create',upid:null,error:'task_timeout',plan,created_at:1,updated_at:2,result:null};
assert.equal(model.jobSchema.parse(job).task_warnings.length,0,'older journals remain readable');
assert.equal(model.jobSchema.parse({...job,task_warnings:[{phase:'vzcreate',upid:'UPID:fixture',exitstatus:'WARNINGS: 12'}]}).task_warnings[0].exitstatus,'WARNINGS: 12');
assert.equal(model.jobSchema.safeParse({...job,task_warnings:[{phase:'vzcreate',upid:'UPID:fixture',exitstatus:'WARNINGS: 1\n'}]}).success,false);
assert.equal(model.jobSchema.safeParse(job).success,true);assert.equal(model.jobSchema.safeParse({...job,status:'completed'}).success,false);
function leaves(value,prefix=''){return Object.entries(value).flatMap(([key,v])=>typeof v==='string'?[[prefix+key,v]]:leaves(v,prefix+key+'.'));}
const en=leaves(JSON.parse(fs.readFileSync('AppImage/messages/en/common.json')).create.native),sk=leaves(JSON.parse(fs.readFileSync('AppImage/messages/sk/common.json')).create.native);
assert.deepEqual(en.map(x=>x[0]),sk.map(x=>x[0]));assert(en.every(x=>x[1].trim()));assert(sk.every(x=>x[1].trim()));
const component=fs.readFileSync('AppImage/components/native-lxc-wizard.tsx','utf8');
assert(!/localStorage|sessionStorage|setWidgetState|console\./.test(component),'the wizard does not persist or log secrets');
assert(component.includes('credentials: { password }'));assert(!component.includes('JSON.stringify({ password'));
console.log(JSON.stringify({pass:true,localeKeys:en.length,contracts:'Python planner -> TypeScript schemas'}));
