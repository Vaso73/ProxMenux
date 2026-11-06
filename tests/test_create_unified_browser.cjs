// Actual shared Create component; all API, jobs and images are inert fixtures.
const fs=require('node:fs'),path=require('node:path'),assert=require('node:assert/strict');
const {bundle,sample}=require('./test_native_lxc_browser.cjs');
const {chromium}=require(process.env.PLAYWRIGHT_CORE || 'playwright-core');
const app=path.resolve(__dirname,'../AppImage');
async function main(){
 const js=bundle(`import React from 'react';import {createRoot} from 'react-dom/client';import {I18nProvider} from './lib/i18n/provider';import {CreateWorkloads} from './components/create-workloads';createRoot(document.getElementById('root')!).render(<I18nProvider><CreateWorkloads /></I18nProvider>);`);
 const css=fs.readdirSync(app+'/out/_next/static/css').filter(x=>x.endsWith('.css')).map(x=>fs.readFileSync(app+'/out/_next/static/css/'+x,'utf8')).join('\n');
 const browser=await chromium.launch({executablePath:process.env.CHROMIUM || '/usr/bin/chromium',headless:true,args:['--no-sandbox']});
 const calls=[],errors=[],cases=[];let release,mode='delay',posted=false,page,nativeDelay=false,exhausted=false;
 try{
  const context=await browser.newContext({serviceWorkers:'block'});
  await context.addInitScript(()=>localStorage.setItem('proxmenux-ui-language',new URLSearchParams(location.search).get('lang')));
  const item={source_id:'same',workload_type:'lxc',variant:'debian',development:false,title:'Same',description:'Fixture application',website:'',icon:'/missing.svg',category:'tools',category_label:'Tools',architectures:['amd64'],template_status:'catalog-only'};
  const sourceQuestion={id:'b'.repeat(32),kind:'choose',title:'Input method',text:'Choose source',default:'image',options:[['paste','Compose'],['run','docker run'],['image','Image'],['file','Host file'],['url','URL'],['oci-compose-paste','OCI text'],['oci-compose','OCI file'],['oci-compose-url','OCI URL']],expires_at:1000,back:false};
  let job={id:'a'.repeat(32),operation:'custom.install',parameters:{},node:'testnode',status:'question',created_at:1,server_time:10,error:null,result:null,question:sourceQuestion};
  await context.route('**/*',async route=>{
   const r=route.request(),u=new URL(r.url());calls.push({method:r.method(),path:u.pathname});
   if(u.pathname==='/')return route.fulfill({contentType:'text/html',body:'<!doctype html><html class="dark"><head><meta name="viewport" content="width=device-width,initial-scale=1"><style>'+css+'</style></head><body><div id="root"></div><script src="/fixture.js"></script></body></html>'});
   if(u.pathname==='/fixture.js')return route.fulfill({contentType:'text/javascript; charset=utf-8',body:js});
   if(u.pathname==='/missing.svg')return route.fulfill({status:404,body:''});
   if(u.pathname==='/api/create/catalog/installation')return route.fulfill({json:{success:true,sources:{oci:{available:true},community:{available:true}},apps:['oci','community'].map(provider=>({...item,provider,id:provider+':same'}))}});
   if(u.pathname==='/api/create/lxc/v1/context'){
    if(nativeDelay)await new Promise(resolve=>release=resolve);
    const c=structuredClone(sample.context);if(exhausted){c.can_execute=false;c.defaults.vmid=null;c.write_policy={state:'enabled',reason:'ids_exhausted',revision:'c'.repeat(64)}}
    return route.fulfill({json:{success:true,context:c}});
   }
   if(u.pathname.endsWith('/context')){
    if(mode==='delay')await new Promise(resolve=>release=resolve);
    if(mode==='error')return route.fulfill({status:503,json:{error:'unavailable'}});
    return route.fulfill({json:{success:true,node:'testnode',operations:['custom.install']}});
   }
   if(u.pathname==='/api/create/oci/manager/v1/jobs'&&r.method()==='POST'){
    assert(!posted,'exactly one fixture preparation');posted=true;
    assert.equal(r.postDataJSON().operation,'custom.install');return route.fulfill({json:{success:true,job}});
   }
   if(u.pathname.endsWith('/jobs/'+job.id+'/answer')&&r.method()==='POST'){
    assert.deepEqual(r.postDataJSON(),{question_id:sourceQuestion.id,value:'image'});
    job={...job,question:{...sourceQuestion,id:'c'.repeat(32),kind:'ask',title:'Image',text:'Container image',options:[],default:null}};
    return route.fulfill({json:{success:true,job}});
   }
   if(u.pathname.endsWith('/jobs/'+job.id)&&r.method()==='GET')return route.fulfill({json:{success:true,job}});
   throw Error('Unexpected fixture request '+r.method()+' '+u.pathname);
  });
  page=await context.newPage();page.on('pageerror',e=>errors.push(e.message));page.setDefaultTimeout(5000);
  for(const lang of ['en','sk'])for(const width of [1280,390,320]){
   const m=JSON.parse(fs.readFileSync(app+'/messages/'+lang+'/common.json'));mode='normal';posted=false;job={...job,question:sourceQuestion};
   await page.setViewportSize({width,height:800});await page.goto('https://create.fixture.invalid/?lang='+lang);await page.waitForFunction(l=>document.documentElement.lang===l,lang);
   await page.getByRole('button',{name:m.create.native.entry,exact:true}).click();
   const modal=page.getByRole('dialog');await modal.waitFor();
   await page.locator('[data-create-method="custom"]').click();
   assert.equal(await page.locator('[data-oci-manager-operation]').count(),0);
   const initial=calls.filter(c=>c.method==='POST').length;
   mode='delay';await page.getByRole('button',{name:m.create.wizard.next,exact:true}).click();
   await page.getByText(m.ociManager.loading,{exact:true}).waitFor();
   assert.equal(await page.getByText(m.ociManager.working,{exact:true}).count(),0);
   assert.equal(await page.getByText(m.ociManager.background,{exact:true}).count(),0);
   assert.equal(calls.filter(c=>c.method==='POST').length,initial);
   await page.getByRole('button',{name:m.create.native.backToChoice,exact:true}).click();
   while(!release)await page.waitForTimeout(10);mode='normal';release();release=null;await page.waitForTimeout(75);
   assert.equal(calls.filter(c=>c.method==='POST').length,initial,'pending preparation cannot allocate from hidden branch');
   await page.locator('[data-create-method="custom"]').click();
   await page.getByText('Container image',{exact:true}).waitFor();
   assert.equal(calls.filter(c=>c.method==='POST').length,initial+2,'one preparation and one explicit source selection');
   const image=page.getByRole('textbox');await image.fill('fixture-image');
   await page.getByRole('button',{name:m.create.native.backToChoice,exact:true}).click();
   await page.locator('[data-create-method="custom"]').click();await page.getByText('Container image',{exact:true}).waitFor();
   assert.equal(calls.filter(c=>c.method==='POST').length,initial+2,'return never repeats existing intent or source');
   await page.keyboard.press('Tab');assert(await page.evaluate(()=>!!document.activeElement.closest('[role="dialog"]')));
   assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth));
   await page.getByRole('button',{name:m.create.native.backToChoice,exact:true}).click();
   await page.locator('[data-create-method="apps"]').click();
   await page.getByRole('button',{name:/Same/}).first().waitFor();
   await page.waitForFunction(()=>document.querySelectorAll('img[src="/missing.svg"]').length===0);
   const cards=page.getByRole('button',{name:/Same/});assert.equal(await cards.count(),1);
   assert((await cards.first().getAttribute('aria-label')).includes('OCI'),'installation opens directly on OCI');
   const filters=page.getByRole('group',{name:m.create.unified.sourceFilter,exact:true});
   const buttons=filters.getByRole('button');assert.equal(await buttons.count(),2);
   assert.equal(await filters.getByRole('button',{name:m.create.unified.sources.all,exact:true}).count(),0,'installation has no All choice');
   assert.equal(await filters.getByRole('button',{name:m.create.unified.sources.oci,exact:true}).getAttribute('aria-pressed'),'true');
   const {groupBox,boxes}=await filters.evaluate(group=>({groupBox:group.getBoundingClientRect().toJSON(),boxes:Array.from(group.querySelectorAll('button'),button=>button.getBoundingClientRect().toJSON())}));
   assert(boxes.every(b=>b&&Math.abs(b.width-boxes[0].width)<1&&Math.abs(b.y-boxes[0].y)<1),'two equal cards on one row');
   assert(Math.abs(boxes[0].x-groupBox.x)<1&&Math.abs(boxes[1].x+boxes[1].width-groupBox.x-groupBox.width)<1,'cards span full content width: '+JSON.stringify({lang,width,groupBox,boxes}));
   const filterPosts=calls.filter(c=>c.method==='POST').length;
   for(const [source,provider] of [['oci','OCI'],['community','Community Scripts']]){
    const button=filters.getByRole('button',{name:m.create.unified.sources[source],exact:true});
    await button.focus();await page.keyboard.press('Enter');
    assert.equal(await button.getAttribute('aria-pressed'),'true');
    assert.equal(await cards.count(),1);assert((await cards.first().getAttribute('aria-label')).includes(provider));
   }
   assert.equal(calls.filter(c=>c.method==='POST').length,filterPosts,'source filtering never prepares or installs');
   if(process.env.CREATE_BROWSER_EVIDENCE)await page.screenshot({path:process.env.CREATE_BROWSER_EVIDENCE+'/'+lang+'-'+width+'-source-cards.png'});
   await page.getByRole('textbox',{name:m.create.catalog.searchLabel,exact:true}).fill('Community Scripts');
   assert.equal(await page.getByRole('button',{name:/Same/}).count(),1);
   const footer=await modal.locator('footer').boundingBox();assert(footer&&footer.y+footer.height<=801);
   await page.getByRole('button',{name:/Same/}).first().click();
   await page.getByText(m.create.friendly.selected,{exact:true}).waitFor();
   if(process.env.CREATE_BROWSER_EVIDENCE)await page.screenshot({path:process.env.CREATE_BROWSER_EVIDENCE+'/'+lang+'-'+width+'-catalog.png'});
   cases.push(lang+' '+width+' hidden context, one intent/source, resume, keyboard, provider identity, visible selection, broken icon');
  }
  const m=JSON.parse(fs.readFileSync(app+'/messages/en/common.json')),n=m.create.native;
  await page.setViewportSize({width:1280,height:900});await page.goto('https://create.fixture.invalid/?lang=en');
  await page.getByRole('button',{name:n.entry,exact:true}).click();
  nativeDelay=true;const readCount=calls.filter(c=>c.path==='/api/create/lxc/v1/context').length;
  await page.locator('[data-create-method="native"]').click();await page.getByText(n.loading,{exact:true}).waitFor();
  await page.getByRole('button',{name:n.backToChoice,exact:true}).click();while(!release)await page.waitForTimeout(10);nativeDelay=false;release();release=null;
  await page.locator('[data-create-method="native"]').click();await page.locator('[data-native-field="hostname"]').fill('retained-friendly-draft');
  const w=page.locator('[data-native-wizard]'),footer=w.locator('[data-native-footer]');
  await footer.getByRole('button',{name:n.next,exact:true}).click();await w.locator('[data-native-field="password"]').fill('fixture-password');await w.locator('[data-native-field="confirmPassword"]').fill('fixture-password');
  await page.getByRole('button',{name:n.backToChoice,exact:true}).click();await page.locator('[data-create-method="native"]').click();
  assert.equal(await w.locator('[data-native-field="password"]').inputValue(),'');assert.equal(await w.locator('[data-native-field="confirmPassword"]').inputValue(),'');
  await w.getByRole('navigation').getByRole('button',{name:n.guided.system,exact:true}).click();assert.equal(await w.locator('[data-native-field="hostname"]').inputValue(),'retained-friendly-draft');
  assert.equal(calls.filter(c=>c.path==='/api/create/lxc/v1/context').length,readCount+1,'return retains one completed context read');
  await footer.getByRole('button',{name:n.next,exact:true}).click();await w.locator('[data-native-field="password"]').fill('fixture-password');await w.locator('[data-native-field="confirmPassword"]').fill('fixture-password');
  for(let i=0;i<3;i++)await footer.getByRole('button',{name:n.next,exact:true}).click();
  assert.equal(await w.locator('[data-native-review]').count(),4);
  await w.locator('[data-native-review="resources"]').getByRole('button',{name:n.friendly.edit,exact:true}).click();await w.locator('[data-native-field="memory.memory"]').fill('768');await w.getByRole('navigation').getByRole('button',{name:n.guided.review,exact:true}).click();
  assert((await w.locator('[data-native-review="resources"]').innerText()).includes('768'));
  if(process.env.CREATE_BROWSER_EVIDENCE)await page.screenshot({path:process.env.CREATE_BROWSER_EVIDENCE+'/en-native-review.png'});
  cases.push('native pending context return, draft retained, passwords cleared, grouped editable review');
  exhausted=true;await page.reload();await page.getByRole('button',{name:n.entry,exact:true}).click();await page.locator('[data-create-method="native"]').click();
  const before=calls.filter(c=>c.method==='POST').length;await w.locator('[data-native-field="hostname"]').fill('preview-only');assert.equal(await w.locator('[data-native-field="vmid"]').inputValue(),'');
  await footer.getByRole('button',{name:n.next,exact:true}).click();await w.locator('[data-native-field="password"]').fill('fixture-password');await w.locator('[data-native-field="confirmPassword"]').fill('fixture-password');
  for(let i=0;i<3;i++)await footer.getByRole('button',{name:n.next,exact:true}).click();
  assert.equal(await w.getByRole('button',{name:n.check,exact:true}).isDisabled(),true);assert.equal(await w.getByRole('button',{name:n.save,exact:true}).count(),0);assert.equal(await w.getByRole('button',{name:n.create,exact:true}).count(),0);assert.equal(calls.filter(c=>c.method==='POST').length,before,'exhausted preparation has zero checks or mutations');
  cases.push('exhausted blank ID browses all steps, zero checks, exports or creates');
  assert.deepEqual(errors,[]);assert(calls.every(c=>c.method==='GET'||c.path==='/api/create/oci/manager/v1/jobs'||c.path.endsWith('/answer')));
  console.log(JSON.stringify({pass:true,cases,fixtureOnly:true,posts:calls.filter(c=>c.method==='POST').length}));
 }catch(e){console.error(JSON.stringify({calls,errors,cases,text:page?await page.locator('body').innerText():''}));throw e}finally{await browser.close()}
}
main().catch(e=>{console.error(e);process.exitCode=1});
