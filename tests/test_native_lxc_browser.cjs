// Actual shipped React component in Chromium. Every request is intercepted;
// no Proxmox, SSH, credentials or guest lifecycle is involved.
const fs=require('node:fs'),path=require('node:path'),os=require('node:os'),assert=require('node:assert/strict');
const cp=require('node:child_process'),{createRequire}=require('node:module');
const root=path.resolve(__dirname,'..'),app=path.join(root,'AppImage');
const req=createRequire(path.join(app,'package.json')),ts=req('typescript');
const {chromium}=require(process.env.PLAYWRIGHT_CORE || 'playwright-core');
// A small CommonJS fixture packer uses the repository's TypeScript compiler
// and installed dependencies, without adding a product dependency/toolchain.
function bundle(customEntry){
 const modules=[],ids=new Map();
 function add(file){
  if(ids.has(file))return ids.get(file);
  const id=modules.length;ids.set(file,id);modules.push('');
  let code=fs.readFileSync(file,'utf8');
  if(file.endsWith('.json'))code='module.exports='+code;
  else if(/\.tsx?$/.test(file))code=ts.transpileModule(code,{compilerOptions:{jsx:ts.JsxEmit.ReactJSX,module:ts.ModuleKind.CommonJS,target:ts.ScriptTarget.ES2020,esModuleInterop:true}}).outputText;
  code=code.replace(/require\(["']([^"']+)["']\)/g,(_,name)=>{
   const target=name.startsWith('@/')?path.join(app,name.slice(2)):name;
   let resolved;
   try{resolved=createRequire(file).resolve(target)}catch(error){
    if(name.startsWith('.')||name.startsWith('@/')){
     const base=name.startsWith('@/')?target:path.resolve(path.dirname(file),name);
     resolved=['.ts','.tsx','/index.ts','/index.tsx'].map(x=>base+x).find(x=>fs.existsSync(x));
    }
    if(!resolved)throw error;
   }
   return '__require('+add(resolved)+')';
  });
  modules[id]='function(module,exports,__require){'+code+'\n}';return id;
 }
 const dir=fs.mkdtempSync(path.join(os.tmpdir(),'native-browser-'));
 const entry=path.join(app,'.native-browser-entry.tsx');
 const parentSource=fs.readFileSync(path.join(app,'components/create-workloads.tsx'),'utf8');
 const bodyClass=parentSource.match(/data-create-wizard-body className=\{method === "native" \? "([^"]+)"/)[1];
 // Entry is temporary and removed even if packing fails.
 fs.writeFileSync(entry,`import React from 'react';import {createRoot} from 'react-dom/client';import {I18nProvider} from './lib/i18n/provider';import {NativeLxcWizard} from './components/native-lxc-wizard';import {Dialog,DialogContent,DialogTitle,DialogDescription} from './components/ui/dialog';createRoot(document.getElementById('root')!).render(<I18nProvider><Dialog open><DialogContent className="flex h-[100dvh] max-h-[100dvh] w-full max-w-none flex-col gap-0 overflow-hidden rounded-none p-0 sm:h-auto sm:max-h-[min(86dvh,700px)] sm:w-[94vw] sm:max-w-4xl sm:rounded-xl"><header className="shrink-0 border-b px-4 py-2.5 pr-12"><DialogTitle>ProxMenux</DialogTitle><DialogDescription className="sr-only">LXC</DialogDescription></header><div className="${bodyClass}"><NativeLxcWizard onBack={()=>{document.getElementById('root')!.innerHTML='CLOSED'}} /></div></DialogContent></Dialog></I18nProvider>);`);
 if(customEntry)fs.writeFileSync(entry,customEntry);
 try{
  const entryId=add(entry);
  return '(()=>{const process={env:{NODE_ENV:"production"}};const m=['+modules.join(',')+'],cache={};function __require(i){if(cache[i])return cache[i].exports;const item=cache[i]={exports:{}};m[i](item,item.exports,__require);return item.exports}__require('+entryId+');})();';
 }finally{fs.unlinkSync(entry);fs.rmSync(dir,{recursive:true});}
}
const sample=JSON.parse(cp.execFileSync('python3',['-B','-c',`import sys,json;sys.path[:0]=['AppImage/scripts','tests'];from native_lxc_fixture import context,draft;from native_lxc import plan;c=context();d=draft(c);p,_=plan(d,c);print(json.dumps(dict(context=c,plan=p)))`],{cwd:root,encoding:'utf8'}));
const ids=['a'.repeat(32),'b'.repeat(32),'c'.repeat(32)];
function job(id,index){return {id,intent:String(index+1).repeat(32),node:sample.context.node,vmid:4900+index,status:['unknown','partial','succeeded'][index],phase:'create',upid:'UPID:fixture:'+index,error:index===0?'task_timeout':null,plan:{...sample.plan,vmid:4900+index},created_at:1,updated_at:2,result:null,task_warnings:index===1?[{phase:'vzcreate',upid:'UPID:warning',exitstatus:'WARNINGS: 1'}]:[]};}
async function main(){
 const javascript=bundle(),browser=await chromium.launch({executablePath:process.env.CHROMIUM || '/usr/bin/chromium',headless:true,args:['--no-sandbox']});
 const calls=[],errors=[],cases=[];let page,mode='normal',release=null;
 const cssPath=path.join(app,'out/_next/static/css');
 const css=fs.existsSync(cssPath)?fs.readdirSync(cssPath).filter(n=>n.endsWith('.css')).map(n=>fs.readFileSync(path.join(cssPath,n),'utf8')).join('\n'):'';
 try{
  const context=await browser.newContext({viewport:{width:1280,height:900},serviceWorkers:'block'});
  await context.route('**/*',async route=>{
   const r=route.request(),u=new URL(r.url());calls.push({method:r.method(),path:u.pathname});
   if(u.origin!=='https://native.fixture.invalid')return route.abort();
   if(u.pathname==='/')return route.fulfill({contentType:'text/html; charset=utf-8',body:'<!doctype html><html class="'+(u.searchParams.get('theme')==='dark'?'dark':'')+'"><head><meta charset="utf-8"><link rel="stylesheet" href="/fixture.css"></head><body><div id="root"></div><script src="/fixture.js"></script></body></html>'});
   if(u.pathname==='/fixture.css')return route.fulfill({contentType:'text/css',body:css});
   if(u.pathname==='/fixture.js')return route.fulfill({contentType:'text/javascript',body:javascript});
   if(r.method()==='GET'&&u.pathname==='/api/create/lxc/v1/context'){
    if(mode==='context-delay')await new Promise(resolve=>{release=resolve;});
    if(mode==='context-error')return route.fulfill({status:503,json:{error:'pve_unavailable'}});
    const c=structuredClone(sample.context);
    if(mode==='lock'){c.can_execute=false;c.defaults.vmid=null;c.write_policy={state:'unavailable',reason:'lock_unavailable',revision:'c'.repeat(64)};}
    if(mode==='exhausted'){c.can_execute=false;c.defaults.vmid=null;c.write_policy={state:'enabled',reason:'ids_exhausted',revision:'c'.repeat(64)};c.ha_readiness={state:'unavailable',reason:'services_inactive'};
     c.templates=['13.1-2','13.6-1','13.6-1-shutdownfix1'].map(build=>({...c.templates[0],id:'local:vztmpl/debian-13-standard_'+build+'_amd64.tar.zst',name:'debian-13-standard_'+build+'_amd64.tar.zst'}));
    }
    return route.fulfill({json:{success:true,context:c}});
   }
   if(r.method()==='GET'&&u.pathname==='/api/create/lxc/v1/jobs')return route.fulfill({json:{success:true,jobs:ids.map(job)}});
   const i=ids.indexOf(u.pathname.split('/').at(-1));
   if(r.method()==='GET'&&i>=0){
    if(mode==='delay')await new Promise(resolve=>{release=resolve;});
    return route.fulfill({json:{success:true,job:{...job(ids[i],i),...(mode==='mismatch'?{intent:'f'.repeat(32)}:{})}}}).catch(()=>{});
   }
   if(r.method()==='POST'&&u.pathname==='/api/create/lxc/v1/checks'){
    const body=r.postDataJSON();assert.deepEqual(Object.keys(body),['draft']);
    assert(!JSON.stringify(body).includes('fixture-password'));
    if(mode==='field-error')return route.fulfill({status:422,json:{error:'invalid_field',field:'cpu.units'}});
    const plan=JSON.parse(cp.execFileSync('python3',['-B','-c',`import sys,json;sys.path[:0]=['AppImage/scripts','tests'];from native_lxc_fixture import context;from native_lxc import plan;p,_=plan(json.load(sys.stdin),context());print(json.dumps(p))`],{cwd:root,input:JSON.stringify(body.draft),encoding:'utf8'}));
    return route.fulfill({json:{success:true,plan,check:{owner:'fixture',plan_hash:plan.plan_hash,expires_at:120,signature:'a'.repeat(64)},server_time:mode==='expire'?119:0,expires_at:120,can_execute:true}});
   }
   errors.push('Unexpected request '+r.method()+' '+u.pathname);return route.abort();
  });
  await context.addInitScript(()=>{localStorage.setItem('proxmenux-ui-language',new URLSearchParams(location.search).get('lang')||'en');});
  page=await context.newPage();page.on('pageerror',e=>errors.push(e.message));
  const en=JSON.parse(fs.readFileSync(path.join(app,'messages/en/common.json'))).create.native;
  await page.goto('https://native.fixture.invalid/');
  for(const state of ['context-error','context-delay']){
   mode=state;await page.reload();
   await page.getByRole('button',{name:en.history,exact:true}).waitFor();
   const box=await page.getByRole('dialog').boundingBox();assert(box.height<620&&box.height>=360,'stable bounded loading/error frame');
   await page.getByRole('button',{name:en.history,exact:true}).click();
   await page.getByRole('heading',{name:en.history,exact:true}).waitFor();
   if(release){mode='normal';release();release=null;}
  }
  mode='normal';await page.reload();
  await page.getByRole('button',{name:en.history,exact:true}).click();
  // Regression: after a new mount/reload, reopen the same persisted job,
  // preserve unknown/partial/warnings, and never create/re-observe it.
  for(let i=0;i<ids.length;i++){
   const row=page.getByRole('button').filter({hasText:sample.context.node+' · LXC '+(4900+i)});
   await row.click({timeout:3000});
   await page.getByRole('heading',{name:en.status[job(ids[i],i).status],exact:true}).waitFor();
   assert(calls.some(c=>c.path.endsWith('/jobs/'+ids[i])&&c.method==='GET'));
   if(i===1)await page.getByText('WARNINGS: 1',{exact:true}).waitFor();
   await page.getByRole('button',{name:en.history,exact:true}).click();
  }
  assert.equal(calls.filter(c=>c.method!=='GET').length,0);
  await page.reload();await page.getByRole('button',{name:en.history,exact:true}).click();
  await page.getByRole('button').filter({hasText:sample.context.node+' · LXC 4900'}).click();
  await page.getByRole('heading',{name:en.status.unknown,exact:true}).waitFor();
  cases.push('history unknown/partial/warnings/success and reload use GET only');
  await page.getByRole('button',{name:en.history,exact:true}).click();mode='mismatch';
  await page.getByRole('button').filter({hasText:sample.context.node+' · LXC 4900'}).click();
  await page.getByRole('alert').waitFor();assert.equal(await page.getByRole('heading',{name:en.history,exact:true}).count(),1);
  cases.push('mismatched original identity rejected');
  mode='delay';await page.getByRole('button').filter({hasText:sample.context.node+' · LXC 4900'}).click();
  await page.waitForFunction(()=>document.querySelector('[aria-busy="true"]'));
  await page.getByRole('button',{name:en.back,exact:true}).click();
  assert(release);mode='normal';release();release=null;
  await page.getByRole('heading',{name:en.guided.system,exact:true}).waitFor();
  cases.push('cancel delayed history request preserves draft view');
  const evidence=process.env.NATIVE_BROWSER_EVIDENCE;
  if(evidence)fs.mkdirSync(evidence,{recursive:true});
  for(const language of ['en','sk'])for(const theme of ['light','dark'])for(const size of [{width:1440,height:1000},{width:768,height:1024},{width:390,height:844},{width:320,height:640}]){
   mode='normal';await page.setViewportSize(size);
   await page.goto('https://native.fixture.invalid/?lang='+language+'&theme='+theme);await page.waitForFunction(l=>document.documentElement.lang===l,language);
   const m=JSON.parse(fs.readFileSync(path.join(app,'messages',language,'common.json'))).create.native;
   const w=page.locator('[data-native-wizard]'),footer=w.locator('[data-native-footer]');
   await page.getByRole('heading',{name:m.guided.system,exact:true}).waitFor();
   if(size.width>=768){
    const modal=await page.getByRole('dialog').boundingBox();assert(modal.width<=900,'compact desktop width');
    const field=await w.locator('[data-native-field="hostname"]').boundingBox();assert(field.height<=34,'compact fields');
   }
   assert.equal(await w.getByRole('navigation').getByRole('button').count(),5);
   assert.equal(await w.locator('[data-native-field="password"]').count(),0);
   assert.equal(await w.getByRole('navigation').getByRole('button',{name:m.guided.resources,exact:true}).isDisabled(),true);
   await w.locator('[data-native-field="vmid"]').fill('');await footer.getByRole('button',{name:m.next,exact:true}).click();
   await page.getByRole('heading',{name:m.guided.system,exact:true}).waitFor();
   await w.locator('[data-native-field="vmid"]').fill('4900');await w.locator('[data-native-field="hostname"]').fill('guided-fixture');
   await w.getByText(m.containerOptions,{exact:true}).click();
   const checkbox=await w.locator('[data-native-field="unprivileged"]').locator('..').boundingBox();assert(checkbox.height<=32,'compact checkbox row');
   await w.locator('[data-native-field="unprivileged"]').uncheck();
   await footer.getByRole('button',{name:m.next,exact:true}).click();
   await page.getByRole('heading',{name:m.guided.access,exact:true}).waitFor();
   await w.locator('[data-native-field="password"]').fill('fixture-password');
   await w.locator('[data-native-field="confirmPassword"]').fill('different-password');
   await footer.getByRole('button',{name:m.next,exact:true}).click();await w.getByRole('alert').waitFor();
   await w.locator('[data-native-field="confirmPassword"]').fill('fixture-password');
   await footer.getByRole('button',{name:m.next,exact:true}).click();
   await page.getByRole('heading',{name:m.guided.resources,exact:true}).waitFor();
   await w.getByRole('button',{name:m.addMount,exact:true}).click();
   await w.locator('[data-native-field="mounts.0.size_gib"]').fill('1');
   await w.locator('[data-native-field="mounts.0.ro"]').check();
   await w.locator('[data-native-section="disks"] details').nth(1).locator('summary').click();
   assert.equal(await w.locator('[data-native-field="mounts.0.acl"] option[value="false"]').evaluate(e=>e.disabled),true,await w.locator('[data-native-field="mounts.0.acl"]').evaluate(e=>e.outerHTML));
   await w.locator('[data-native-field="mounts.0.acl"]').selectOption('true');
   await w.locator('[data-native-field="cpu.cores"]').fill('2');
   await w.locator('[data-native-field="memory.memory"]').fill('768');
   await w.locator('[data-native-field="memory.swap"]').fill('256');
   await footer.getByRole('button',{name:m.next,exact:true}).click();
   await page.getByRole('heading',{name:m.guided.network,exact:true}).waitFor();
   await w.locator('[data-native-field="network.tag"]').fill('200');
   await w.locator('[data-native-field="network.ipv4.mode"]').selectOption('static');
   await w.locator('[data-native-field="network.ipv4.address"]').fill('10.200.200.20/24');
   await w.locator('[data-native-field="network.ipv4.gateway"]').fill('10.200.200.1');
   await w.locator('[data-native-field="dns.nameservers"]').fill('10.200.200.1');
   await footer.getByRole('button',{name:m.next,exact:true}).click();
   await page.getByRole('heading',{name:m.guided.review,exact:true}).waitFor();
   mode='field-error';await w.getByRole('button',{name:m.check,exact:true}).click();
   await w.getByRole('alert').getByRole('button').click();
   await page.getByRole('heading',{name:m.guided.resources,exact:true}).waitFor();
   await w.getByRole('navigation').getByRole('button',{name:m.guided.review,exact:true}).click();
   mode='normal';await w.getByRole('button',{name:m.check,exact:true}).click();await w.locator('[data-native-checked]').waitFor();
   const create=w.getByRole('button',{name:m.create,exact:true});assert.equal(await create.isDisabled(),true);
   for(const box of await w.locator('[data-native-checked] input[type="checkbox"]').all())await box.check();
   assert.equal(await create.isEnabled(),true);
   const scroll=w.locator('[data-native-scroll]');await scroll.evaluate(e=>e.scrollTo(0,e.scrollHeight));
   const rect=await footer.boundingBox();assert(rect.y>=0&&rect.y+rect.height<=size.height+1,'stable footer in viewport');
   assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth));
   assert(await scroll.evaluate(e=>e.scrollWidth<=e.clientWidth+1),'content does not overflow');
   assert(!(await w.innerText()).includes('fixture-password'));
   if(evidence&&size.width!==768)await page.screenshot({path:path.join(evidence,`${language}-${theme}-${size.width}-review.png`)});
   await w.locator('[data-native-review="resources"]').getByRole('button',{name:m.friendly.edit,exact:true}).click();
   await w.locator('[data-native-field="memory.swap"]').fill('128');
   await w.getByRole('navigation').getByRole('button',{name:m.guided.review,exact:true}).click();
   assert.equal(await w.locator('[data-native-checked]').count(),0,'draft edit invalidates check');
   mode='expire';await w.getByRole('button',{name:m.check,exact:true}).click();await w.locator('[data-native-checked]').waitFor();
   await w.getByText(m.errors.check_expired,{exact:true}).waitFor();assert.equal(await w.locator('[data-native-checked]').count(),0);
   cases.push(`${language}/${theme}/${size.width}: five groups, field validation, backend error, warning, stale edit, TTL, responsive`);
  }
  mode='lock';await page.reload();
  const sk=JSON.parse(fs.readFileSync(path.join(app,'messages/sk/common.json'))).create.native;
  await page.getByText(sk.lockUnavailable,{exact:true}).waitFor();
  assert.equal(await page.locator('[data-native-field="vmid"]').inputValue(),'');
  mode='exhausted';await page.reload();
  await page.getByText(sk.idsExhausted+' '+sk.preparationOnly,{exact:true}).waitFor();
  const templateLabels=await page.locator('[data-native-field="template"] option').allTextContents();
  assert.equal(new Set(templateLabels).size,templateLabels.length,'different real Debian builds remain distinguishable');
  assert(templateLabels.includes('Debian 13 · 13.6-1-shutdownfix1 · amd64'));
  const bottom=await page.locator('[data-native-footer]').boundingBox();assert(bottom.y+bottom.height<=640+1,'long exhausted/HA unavailable mobile keeps footer');
  assert.equal(calls.filter(c=>c.method==='POST'&&c.path.endsWith('/jobs')).length,0);
  assert.deepEqual(errors,[]);
  console.log(JSON.stringify({pass:true,historyGetOnly:true,reload:true,cases,calls}));
 }finally{await browser.close();}
}
module.exports={bundle,sample};
if(require.main===module)main().catch(error=>{console.error(error);process.exitCode=1;});
