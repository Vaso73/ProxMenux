// Actual Create wrapper and manager; every request terminates in local fixtures.
const fs=require('node:fs'),path=require('node:path'),assert=require('node:assert/strict');
const {bundle}=require('./test_native_lxc_browser.cjs');
const {chromium}=require(process.env.PLAYWRIGHT_CORE || 'playwright-core');
const app=path.resolve(__dirname,'../AppImage');
(async()=>{
 const js=bundle(`import React from 'react';import {createRoot} from 'react-dom/client';import {I18nProvider} from './lib/i18n/provider';import {CreateWorkloads} from './components/create-workloads';createRoot(document.getElementById('root')!).render(<I18nProvider><CreateWorkloads /></I18nProvider>);`);
 const css=fs.readdirSync(app+'/out/_next/static/css').filter(n=>n.endsWith('.css')).map(n=>fs.readFileSync(app+'/out/_next/static/css/'+n,'utf8')).join('\n');
 const browser=await chromium.launch({executablePath:process.env.CHROMIUM || '/usr/bin/chromium',headless:true,args:['--no-sandbox']});
 const results=[];
 try{
 for(const lang of (process.env.CREATE_CANCEL_LANG?[process.env.CREATE_CANCEL_LANG]:['en','sk']))for(const width of [1280,390])for(const branch of ['apps','custom'])for(const status of ['cancelled','expired','failed','recovery_required','completed']){
  const context=await browser.newContext({viewport:{width,height:900},serviceWorkers:'block'}),calls=[],jobs=[],errors=[];
  const m=JSON.parse(fs.readFileSync(app+'/messages/'+lang+'/common.json'));
  await context.addInitScript(()=>localStorage.setItem('proxmenux-ui-language',new URLSearchParams(location.search).get('lang')));
  await context.route('**/*',async route=>{
   const req=route.request(),u=new URL(req.url());calls.push({method:req.method(),path:u.pathname,body:req.method()==='POST'?req.postDataJSON():null});
   if(u.pathname==='/')return route.fulfill({contentType:'text/html',body:'<html class="dark"><meta name="viewport" content="width=device-width,initial-scale=1"><style>'+css+'</style><div id="root"></div><script src="/fixture.js"></script></html>'});
   if(u.pathname==='/fixture.js')return route.fulfill({contentType:'text/javascript; charset=utf-8',body:js});
   if(u.pathname==='/api/create/catalog/installation')return route.fulfill({json:{success:true,sources:{oci:{available:true},community:{available:true}},apps:['First','Other'].map(title=>({id:'oci:'+title,source_id:title,provider:'oci',title,description:'Fixture',website:'',icon:'',category:'tools',category_label:'Tools',architectures:['amd64'],template_status:'laboratory-validated',workload_type:'lxc'}))}});
   if(u.pathname.endsWith('/context'))return route.fulfill({json:{success:true,node:'fixture',operations:['custom.install','catalog.install']}});
   if(u.pathname.endsWith('/jobs')&&req.method()==='POST'){
    const body=req.postDataJSON();assert(!jobs.some(j=>j.intent===body.request_id),'new intent required');
    const job={id:String(jobs.length+1).repeat(32),intent:body.request_id,operation:body.operation,parameters:body.parameters,node:'fixture',status:'question',question:{id:'a'.repeat(32),kind:'ask',title:null,text:'Fixture question',default:'',options:[],expires_at:Date.now()/1000+900,back:true},error:null,result:null,server_time:Date.now()/1000,created_at:1};jobs.push(job);return route.fulfill({json:{success:true,job}});
   }
   const job=jobs.find(j=>u.pathname.includes('/jobs/'+j.id));assert(job,'only known fixture jobs');
   if(req.method()==='POST'){
    assert(u.pathname.endsWith('/answer'));assert.deepEqual(req.postDataJSON(),{cancel:true});
    job.status=status;job.question=null;if(status==='completed')job.result={operation:job.operation,notices:[]};
   }else assert.equal(req.method(),'GET');
   return route.fulfill({json:{success:true,job}});
  });
  const page=await context.newPage();page.on('pageerror',e=>errors.push(e.message));page.setDefaultTimeout(10000);
  await page.goto('https://fixture.invalid/?lang='+lang);await page.waitForFunction(l=>document.documentElement.lang===l,lang);
  await page.getByRole('button',{name:m.create.native.entry,exact:true}).click().catch(async e=>{console.error(JSON.stringify({lang,width,branch,status,errors,body:await page.locator('body').innerText()}));throw e});
  const choose=()=>page.locator('[data-create-method="'+branch+'"]');await choose().click();
  async function begin(title='First'){
   if(branch==='apps'){
    await page.locator('[data-catalog-option]').filter({hasText:title}).click();
    await page.getByRole('dialog').locator('footer').getByRole('button',{name:m.create.wizard.next,exact:true}).click();
    await page.getByRole('dialog').getByRole('combobox').selectOption('advanced');
    await page.getByRole('dialog').locator('footer').getByRole('button',{name:m.create.wizard.next,exact:true}).click();
   }else await page.getByRole('button',{name:m.create.wizard.next,exact:true}).click();
   await page.getByLabel('Fixture question',{exact:true}).waitFor();
  }
  await begin();assert.equal(jobs.length,1);
  await page.getByLabel('Fixture question',{exact:true}).fill('private draft');
  await page.getByRole('button',{name:m.create.native.backToChoice,exact:true}).click();
  assert.equal(await choose().getByText(m.create.friendly.resumePreparation,{exact:true}).count(),1);
  const posts=()=>calls.filter(c=>c.method==='POST').length;const ongoing=posts();
  await choose().click();await page.getByLabel('Fixture question',{exact:true}).waitFor();assert.equal(posts(),ongoing,'ongoing return never posts');
  await page.getByRole('button',{name:m.ociManager.cancel,exact:true}).click();
  await page.getByRole('heading',{name:m.ociManager.states[status],exact:true}).waitFor();
  const stopped=posts();const restartable=['cancelled','expired'].includes(status);
  const fresh=page.getByRole('button',{name:m.create.friendly.newPreparation||'Start a new preparation',exact:true});
  assert.equal(await fresh.count(),restartable?1:0,'only safely stopped preparation has explicit fresh action');
  if(restartable){
   if(width===1280)await fresh.click();
   else{
    await page.getByRole('button',{name:m.create.native.backToChoice,exact:true}).click();
    assert.equal(await choose().getByText(m.create.friendly.resumePreparation,{exact:true}).count(),0,'cancelled chooser never advertises continuation');
    await choose().click();
   }
   assert.equal(posts(),stopped,'fresh action is local only');
   if(branch==='apps')await page.locator('[data-catalog-option]').filter({hasText:'Other'}).waitFor();
   else await page.locator('[data-custom-source="image"]').waitFor();
   // Restart via the chooser must work too, without stale outcome/selection.
   await page.getByRole('button',{name:m.create.native.backToChoice,exact:true}).click();
   assert.equal(await choose().getByText(m.create.friendly.resumePreparation,{exact:true}).count(),0);
   await choose().click();assert.equal(posts(),stopped);await begin('Other');
   assert.equal(jobs.length,2);assert.notEqual(jobs[0].intent,jobs[1].intent);
   if(branch==='apps')assert.equal(jobs[1].parameters.app_id,'Other');
   assert.equal(jobs[0].status,status,'old server outcome never replayed');
  }else{
   await page.getByRole('button',{name:m.create.native.backToChoice,exact:true}).click();
   assert.equal(await choose().getByText(m.create.friendly.resumePreparation,{exact:true}).count(),0,'terminal result is not ongoing');
   await choose().click();await page.getByRole('heading',{name:m.ociManager.states[status],exact:true}).waitFor();
   assert.equal(posts(),stopped,'uncertain/completed result never replayed');
  }
  assert.deepEqual(errors,[]);results.push({lang,width,branch,status,posts:posts(),realHostCalls:0});await context.close();
 }
 console.log(JSON.stringify({pass:true,cases:results.length,results}));
 }finally{await browser.close()}
})().catch(e=>{console.error(e);process.exitCode=1});
