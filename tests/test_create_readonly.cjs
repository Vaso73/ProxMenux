// Shipped wizard handlers; shared browser and Radix composition have their
// own executable tests and full browser acceptance.
const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm'),ts=require('typescript');
const compile=source=>ts.transpileModule(source,{compilerOptions:{module:ts.ModuleKind.CommonJS,jsx:ts.JsxEmit.ReactJSX}}).outputText;
const manager={};vm.runInNewContext(compile(fs.readFileSync('AppImage/lib/oci-manager.ts','utf8')),{exports:manager,Set});
const flatten=n=>!n||typeof n==='boolean'?[]:Array.isArray(n)?n.flatMap(flatten):typeof n==='object'?
  n.props?.hidden?[]:n.type==='Dialog'&&!n.props.open?[n,...flatten(n.props.children).filter(x=>x.type==='DialogTrigger'||x.type==='Button'||x.type==='CreateCatalogBrowser')]:[n,...flatten(n.props?.children)]:[n];
const text=n=>flatten(n).filter(n=>typeof n==='string'||typeof n==='number').join('');
function fixture(locale,data,error,catalogMode=false,catalogSource){
 const messages=JSON.parse(fs.readFileSync('AppImage/messages/'+locale+'/common.json'));
 const t=(key,params={})=>{const s=key.split('.').reduce((o,k)=>o?.[k],messages);assert.equal(typeof s,'string',key);return s.replace(/\{(\w+)\}/g,(m,k)=>params[k]??m);};
 let slots=[],cursor=0,tree;const calls=[],reads=[],fetched=new Set();
 const react={useState(v){const i=cursor++;if(!(i in slots))slots[i]=v;return[slots[i],v=>slots[i]=v];},useRef(){const i=cursor++;return slots[i]??(slots[i]={current:null});},useEffect(){}};
 const jsx=(type,props)=>({type,props}),exports={};
 vm.runInNewContext(compile(fs.readFileSync('AppImage/components/create-workloads.tsx','utf8')),{exports,performance:{now:()=>0},document:{activeElement:null},HTMLElement:class{},require:name=>name==='react'?react:name==='react/jsx-runtime'?{jsx,jsxs:jsx}:
  name==='swr'?{default:(key,fetcher)=>{reads.push(key);if(key&&!fetched.has(key)){fetched.add(key);fetcher(key);}return{data,error};}}:
  name==='../lib/oci-manager'?manager:name.includes('api-config')?{fetchApi:(path,options)=>{calls.push({path,options});return Promise.resolve(data);}}:
  name.includes('i18n/provider')?{useT:()=>t}:name.includes('lib/create-oci-check')?{makeCheckRequest:()=>null}:name.includes('lib/create-oci-readiness')?{makeReadinessRequest:()=>null,readinessFresh:()=>false,readinessMatches:()=>false}:name.includes('create-catalog-browser')?{CreateCatalogBrowser:'CreateCatalogBrowser',categoryText:app=>app.category_label,methodText:app=>app.provider+' '+app.variant}:
  new Proxy({},{get:(_,key)=>key})});
 return{t,calls,reads,render(){cursor=0;tree=exports.CreateWorkloads({catalogMode,catalogSource});return tree;},nodes:()=>flatten(tree),text:()=>text(tree),dialog:()=>flatten(tree).find(n=>n.type==='Dialog'),
  browser:()=>flatten(tree).find(n=>n.type==='CreateCatalogBrowser'),set(d,e){data=d;error=e;},
  button(label){const n=flatten(tree).find(n=>n.type==='Button'&&text(n)===label);assert.ok(n,label);return n;},
  choose(method){const n=flatten(tree).find(n=>n.props?.['data-create-method']===method);assert.ok(n,method);n.props.onClick();},
  input(id){const n=flatten(tree).find(n=>n.type==='Input'&&n.props.id===id);assert.ok(n,id);return n;}};
}
const oci={id:'oci:same',source_id:'same',provider:'oci',variant:'',title:'Same',category_label:'Tools',architectures:['amd64'],template_status:'generated-unvalidated',requires_privileged_lxc:true};
const community={...oci,id:'community:same:debian',provider:'community',variant:'debian',architectures:[],template_status:'catalog-only',requires_privileged_lxc:false};
for(const locale of ['en','sk']){
 const payload={success:true,apps:[oci,community],sources:{}},f=fixture(locale,payload);f.render();assert.equal(f.calls.length,0);
 const unified=fixture(locale,payload);unified.render();unified.dialog().props.onOpenChange(true);unified.render();
 assert.equal(unified.calls.length,0,'choosing a method is read-only and does not load OCI metadata');
 unified.choose('native');unified.render();assert.ok(unified.nodes().some(n=>n.type==='NativeLxcWizard'));assert.equal(unified.calls.length,0);
 unified.nodes().find(n=>n.type==='NativeLxcWizard').props.onBack();unified.render();
 unified.choose('custom');unified.render();
 assert.equal(unified.nodes().filter(n=>n.type==='Dialog').length,1,'custom uses the same dialog');
 assert.ok(!unified.nodes().some(n=>n.type==='OciManagerOperation'),'custom choice does not mount a job allocator');
 unified.button(unified.t('create.wizard.next')).props.onClick();unified.render();
 assert.equal(unified.nodes().find(n=>n.type==='OciManagerOperation').props.operation,'custom.install');
 unified.button(unified.t('create.native.backToChoice')).props.onClick();unified.render();assert.ok(!unified.nodes().some(n=>n.type==='OciManagerOperation'));
 unified.choose('custom');unified.render();assert.equal(unified.nodes().filter(n=>n.type==='OciManagerOperation').length,1,'return resumes mounted preparation');
 assert.ok(unified.calls.every(c=>!c.options?.method),'source changes never start work');
 f.dialog().props.onOpenChange(true);f.render();assert.equal(f.calls.length,0);f.choose('apps');f.render();assert.deepEqual(f.calls,[{path:'/api/create/catalog/installation',options:undefined}]);
 assert.equal(f.button(f.t('create.wizard.next')).props.disabled,true);
 f.browser().props.onSelect(oci);f.render();f.button(f.t('create.wizard.next')).props.onClick();f.render();
 assert.ok(f.text().includes(f.t('create.details.requirement.privilegedLxc')));
 f.nodes().find(n=>n.type==='select').props.onChange({target:{value:'advanced'}});f.render();
 f.button(f.t('create.wizard.next')).props.onClick();f.render();const op=f.nodes().find(n=>n.type==='OciManagerOperation').props;assert.equal(op.operation,'catalog.install');assert.equal(op.parameters.app_id,'same');assert.equal(op.parameters.mode,'advanced');assert.equal(f.nodes().filter(n=>n.type==='Input').length,0,'OCI inputs belong to the engine wizard');
 f.dialog().props.onOpenChange(false);f.render();f.dialog().props.onOpenChange(true);f.render();f.choose('apps');f.render();
 f.browser().props.onSelect(community);f.render();f.button(f.t('create.wizard.next')).props.onClick();f.render();
 assert.ok(f.text().includes(f.t('create.unified.communityStatus')));assert.ok(f.text().includes('community debian'));
 f.button(f.t('create.wizard.next')).props.onClick();f.render();assert.equal(f.input('create-instance-label').props.value,'','same title with another provider must reset draft');
 f.set(payload,new Error('network'));f.render();assert.equal(f.button(f.t('create.wizard.next')).props.disabled,true,'stale selection cannot proceed');
 f.button(f.t('actions.close')).props.onClick();f.render();f.set(payload);f.dialog().props.onOpenChange(true);f.render();f.choose('apps');f.render();assert.equal(f.button(f.t('create.wizard.next')).props.disabled,true);
 assert.equal(f.calls.length,1,'all preparation/cancel/back/selection handlers remain local');
 const page=fixture(locale,payload,undefined,true);page.render();assert.equal(page.calls.length,1,'full catalog is explicitly read while visible');
 page.browser().props.onSelect(community);page.render();assert.equal(page.dialog().props.open,true);assert.ok(page.text().includes('community debian'),'page card opens same variant in details');
 const fixed=fixture(locale,payload,undefined,true,'oci');fixed.render();assert.equal(fixed.browser().props.fixedSource,'oci');
 fixed.dialog().props.onOpenChange(true);fixed.render();const header=fixed.browser().props.renderHeader('controls');assert.ok(text(header).includes(fixed.t('create.unified.views.oci.title')));assert.equal(flatten(header).filter(n=>n.type==='DialogTrigger').length,1,'the real catalog header contains one Create trigger');assert.equal(flatten(header).filter(n=>n.type==='CardTitle').length,1);fixed.browser().props.onSelect(community);fixed.render();assert.equal(fixed.button(fixed.t('create.wizard.next')).props.disabled,true,'a foreign-provider selection cannot become a plan');
 fixed.browser().props.onSelect(oci);fixed.render();fixed.button(fixed.t('create.wizard.next')).props.onClick();fixed.render();
 assert.equal(fixed.nodes().find(n=>n.type==='OciManagerOperation').props.parameters.app_id,'same');fixed.set({...payload,sources:{oci:{available:false}}});fixed.render();assert.equal(fixed.nodes().find(n=>n.type==='OciManagerOperation').props.parameters.app_id,'same','catalog failure preserves admitted job tracking');
 assert.equal(fixed.calls.length,1,'fixed-provider preparation has no mutation');
 const shared=fixture(locale,payload);shared.render();shared.dialog().props.onOpenChange(true);shared.render();shared.choose('apps');shared.render();shared.browser().props.onSelect(oci);shared.render();shared.button(shared.t('create.wizard.next')).props.onClick();shared.render();shared.set({...payload,sources:{oci:{available:false}}});shared.render();assert.equal(shared.button(shared.t('create.wizard.next')).props.disabled,true,'shared chooser also rejects a stale selection from a failed provider');assert.equal(shared.calls.length,1);
}
console.log('PASS: real wizard handlers EN/SK, separate provider identities, same-app draft reset, two entry paths, stale guard and zero mutations');
