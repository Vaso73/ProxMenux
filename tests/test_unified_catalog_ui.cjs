const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm'),ts=require('typescript');
const compile=s=>ts.transpileModule(s,{compilerOptions:{module:ts.ModuleKind.CommonJS,jsx:ts.JsxEmit.ReactJSX}}).outputText;
const flatten=n=>!n||typeof n==='boolean'?[]:Array.isArray(n)?n.flatMap(flatten):typeof n==='object'?[n,...flatten(n.props?.children)]:[n];
const text=n=>flatten(n).filter(n=>typeof n==='string'||typeof n==='number').join('');
const apps=Array.from({length:25},(_,i)=>({id:'entry-'+i,title:'Entry '+i,provider:i%2?'community':'oci',variant:i%2?'debian':'',workload_type:'lxc',category:i===24?'future':'tools',category_label:'Tools',description:'Plain text',development:i===1}));
function fixture(locale){
 const messages=JSON.parse(fs.readFileSync('AppImage/messages/'+locale+'/common.json'));
 const t=(key,params={})=>{const s=key.split('.').reduce((o,k)=>o?.[k],messages);assert.equal(typeof s,'string',key);return s.replace(/\{(\w+)\}/g,(m,k)=>params[k]??m);};
 let slots=[],cursor=0,tree,pending=[],props={data:{success:true,apps,sources:{oci:{available:true},community:{available:true}}},onSelect:app=>selected.push(app)};const selected=[];
 const jsx=(type,props)=>({type,props}),exports={};
 vm.runInNewContext(compile(fs.readFileSync('AppImage/components/create-catalog-browser.tsx','utf8')),{exports,Set,require:name=>name==='react'?{useState(v){const i=cursor++;if(!(i in slots))slots[i]=v;return[slots[i],v=>slots[i]=v];},useEffect(fn,deps){const i=cursor++;if(!slots[i]||deps.some((v,j)=>v!==slots[i][j])){slots[i]=deps;pending.push(fn);}},useMemo:fn=>fn()}:name==='react/jsx-runtime'?{jsx,jsxs:jsx}:name.includes('i18n/provider')?{useT:()=>t}:new Proxy({},{get:(_,key)=>key})});
 return{t,selected,props,render(){cursor=0;tree=exports.CreateCatalogBrowser(props);if(pending.length){const effects=pending;pending=[];effects.forEach(fn=>fn());cursor=0;tree=exports.CreateCatalogBrowser(props);}return tree;},nodes:()=>flatten(tree),text:()=>text(tree),cards:()=>flatten(tree).filter(n=>n.props?.['data-catalog-option']),
  button(label){const n=flatten(tree).find(n=>n.type==='Button'&&(text(n)===label||n.props['aria-label']===label));assert.ok(n,label);return n;}};
}
for(const locale of ['en','sk']){
 const f=fixture(locale);f.render();assert.equal(f.cards().length,12);
 const notes=fixture(locale);notes.props.data.sources.community.origin='bundled';notes.props.fixedSource='oci';notes.render();
 assert.ok(notes.text().includes(notes.t('create.engine.catalogNote')));
 assert.ok(!notes.text().includes(notes.t('create.unified.bundledSnapshot')),'OCI hides Community-only notes');
 notes.props.fixedSource='community';notes.render();assert.ok(notes.text().includes(notes.t('create.unified.bundledSnapshot')),'Community retains its own source note');
 f.button(f.t('create.wizard.nextPage')).props.onClick();f.render();assert.equal(f.cards().length,12);
 f.button(f.t('create.wizard.nextPage')).props.onClick();f.render();assert.equal(f.cards().length,1);
 f.button(f.t('create.unified.sources.community')).props.onClick();f.render();assert.equal(f.cards().length,12,'source change resets page');
 assert.ok(f.cards().every(n=>n.props['aria-label'].includes('Community Scripts')));assert.ok(f.text().includes(f.t('create.unified.development')));
 f.cards()[0].props.onClick();assert.equal(f.selected[0].id,'entry-1');
 f.nodes().find(n=>n.type==='Input').props.onChange({target:{value:'Entry 0'}});f.render();assert.equal(f.cards().length,0);
 f.button(f.t('create.unified.sources.oci')).props.onClick();f.render();assert.equal(f.cards().length,1);
 assert.ok(f.cards()[0].props['aria-label'].includes('OCI'));
 f.props.selectedId='entry-1';f.render();assert.ok(f.text().includes('Community Scripts'),'hidden selection still names exact provider');
 f.props.data={success:true,apps:apps.filter(a=>a.provider==='oci'),sources:{oci:{available:true},community:{available:false}}};f.render();
 assert.ok(!f.nodes().some(n=>n.props?.role==='alert'),'a scoped view does not report another provider failure');
 f.button(f.t('create.unified.sources.all')).props.onClick();f.render();assert.ok(f.text().includes(f.t('create.unified.sourceUnavailable',{source:'Community Scripts'})));
 f.props.error=new Error('network');f.render();assert.equal(f.cards().length,0,'stale payload hidden on error');
 f.props.error=undefined;f.props.data=undefined;f.render();assert.ok(f.text().includes(f.t('create.catalog.loading')));
 f.props.data={success:true,apps:[],sources:{}};f.render();assert.ok(f.text().includes(f.t('create.catalog.noResults')));
 const controlled=fixture(locale),changes=[];controlled.props.source='oci';
 controlled.props.onSourceChange=value=>{changes.push(value);controlled.props.source=value;};
 controlled.render();assert.ok(controlled.cards().every(n=>n.props['aria-label'].includes('OCI')));
 controlled.button(controlled.t('create.wizard.nextPage')).props.onClick();controlled.render();assert.equal(controlled.cards().length,1);
 controlled.props.source='community';controlled.render();assert.equal(controlled.cards().length,12,'external menu selection resets catalog page');
 assert.ok(controlled.cards().every(n=>n.props['aria-label'].includes('Community Scripts')));
 controlled.button(controlled.t('create.unified.sources.all')).props.onClick();controlled.render();assert.deepEqual(changes,['all']);
 assert.equal(controlled.cards().length,12);assert.equal(controlled.selected.length,0,'changing source never selects or acts on an app');
}
// Installation has two direct sources, including a legacy controlled All value.
for(const locale of ['en','sk'])for(const source of [undefined,'all']){
 const f=fixture(locale);f.props.providerOnly=true;f.props.source=source;f.render();
 const filters=()=>f.nodes().find(n=>n.props?.role==='group');
 assert.equal(flatten(filters()).filter(n=>n.type==='Button').length,2);
 assert.ok(f.cards().every(n=>n.props['aria-label'].includes('OCI')));
 assert.equal(f.button(f.t('create.unified.sources.oci')).props['aria-pressed'],true);
 f.button(f.t('create.unified.sources.community')).props.onClick();f.render();
 assert.ok(f.cards().every(n=>n.props['aria-label'].includes('Community Scripts')));
 f.cards()[0].props.onClick();f.props.selectedId=f.selected[0].id;
 f.button(f.t('create.unified.sources.oci')).props.onClick();f.render();
 assert.ok(f.text().includes(f.t('create.wizard.selected',{appName:'Entry 1 · Community Scripts · LXC · debian'})),'switching source preserves exact selected application');
 f.button(f.t('create.unified.sources.community')).props.onClick();
 f.props.data={...f.props.data,sources:{oci:{available:true},community:{available:false}}};f.render();
 assert.equal(f.cards().length,0,'failed selected source never falls back to another provider');
 assert.ok(f.nodes().some(n=>n.props?.role==='alert'));
 assert.equal(f.selected.length,1,'source switching and provider failure never select or install');
}
// Provider pages and their chooser cannot silently change source, including
// partial feeds that retain stale entries for a failed provider.
for(const locale of ['en','sk']){
 const f=fixture(locale),changes=[];f.props.fixedSource='oci';f.props.source='community';f.props.onSourceChange=v=>changes.push(v);f.props.selectedId='entry-1';f.render();
 assert.equal(f.nodes().filter(n=>n.props?.role==='group').length,0);
 assert.ok(f.cards().every(n=>n.props['aria-label'].includes('OCI')));
 assert.ok(!f.text().includes(f.t('create.wizard.selected',{appName:'Entry 1 · Community Scripts · LXC · debian'})));
 f.nodes().find(n=>n.type==='select').props.onChange({target:{value:'future'}});f.render();assert.equal(f.cards().length,1);
 f.props.fixedSource='community';f.render();assert.equal(f.cards().length,12,'changing provider resets a category from the previous provider');
 assert.ok(!f.nodes().filter(n=>n.type==='option').some(n=>n.props.value==='future'),'categories belong to this provider only');
 f.props.data={...f.props.data,sources:{oci:{available:true},community:{available:false}}};f.render();assert.equal(f.cards().length,0,'no stale failed-source entries or fallback to OCI');
 assert.ok(f.text().includes(f.t('create.unified.fixedUnavailable',{source:'Community Scripts'})));assert.deepEqual(changes,[]);
}
console.log("PASS: creation catalog filtering, provider identities, paging and source failures");
