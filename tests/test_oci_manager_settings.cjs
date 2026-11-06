const assert=require('node:assert/strict');
const {fixture,text}=require('./oci_operation_fixture.cjs');
const answers=f=>f.calls.filter(c=>c.path.endsWith('/answer'));
const change=async(f,index,value)=>{f.nodes('input')[index].props.onChange({target:{value}});await f.render()};
(async()=>{
 for(const locale of ['en','sk']){
  const tagged=fixture(locale);tagged.setServer({question:tagged.question('choose',{show_tags:true,options:[['vmbr0','192.0.2.1/24'],['vmbr1','192.0.2.1/24']],default:'vmbr0'})});await tagged.render();
  assert.deepEqual(tagged.nodes('option').slice(1).map(o=>o.props.children),['vmbr0 · 192.0.2.1/24','vmbr1 · 192.0.2.1/24']);
  const internal=fixture(locale);await internal.render();assert.equal(internal.nodes('option')[1].props.children,'Compose','internal source IDs remain hidden');
  const invalid=fixture(locale);invalid.setServer({question:invalid.question('password',{required:'false'})});await invalid.render();
  assert.equal(invalid.nodes('input').length,0,'malformed required flag exposes no input');assert.equal(answers(invalid).length,0);
  for(const value of [true,false]){
   const f=fixture(locale);f.setServer({question:f.question('confirm',{default:value,execution:false})});await f.render();
   assert.ok(text(f.tree).includes(f.t('ociManager.defaultChoice',{value:f.t(value?'ociManager.yes':'ociManager.no')})));
   assert.equal(f.button('ociManager.yes').props.variant,value?'default':'outline');
   assert.equal(f.button('ociManager.no').props.variant,value?'outline':'default');
   assert.equal(answers(f).length,0,'default never auto-answers');
   await f.button('ociManager.no').props.onClick();assert.equal(JSON.parse(answers(f)[0].options.body).value,false);
  }
  const final=fixture(locale);final.setServer({question:final.question('review',{default:false,execution:true})});await final.render();
  assert.ok(text(final.tree).includes(final.t('ociManager.finalConfirmation')));
  assert.ok(!text(final.tree).includes(final.t('ociManager.defaultChoice',{value:final.t('ociManager.no')})));
  assert.equal(answers(final).length,0);await final.button('ociManager.no').props.onClick();
  assert.equal(JSON.parse(answers(final)[0].options.body).value,false);
  const f=fixture(locale);f.setServer({question:f.question('password',{required:true,back:true})});await f.render();
  assert.equal(f.button('ociManager.next').props.disabled,true,'required blank blocked');
  await f.button('ociManager.next').props.onClick();assert.equal(answers(f).length,0,'handler also guards required blank');
  await change(f,0,'synthetic-only');assert.equal(f.nodes('input').length,2,'own secret needs repetition');
  await change(f,1,'mismatch');assert.equal(f.button('ociManager.next').props.disabled,true);
  assert.ok(text(f.tree).includes(f.t('ociManager.passwordMismatch')));
  await f.button('ociManager.next').props.onClick();assert.equal(answers(f).length,0,'mismatch issues zero requests');
  await change(f,1,'synthetic-only');assert.equal(f.button('ociManager.next').props.disabled,false);
  await change(f,0,'changed-secret');assert.equal(f.nodes('input')[1].props.value,'','changing secret clears previous repetition');
  await change(f,1,'changed-secret');await f.button('ociManager.next').props.onClick();await f.render();
  assert.deepEqual(JSON.parse(answers(f)[0].options.body),{question_id:'b'.repeat(32),value:'changed-secret'},'single original value only');
  assert.equal(f.nodes('input')[0].props.value,'','successful answer clears secret');assert.equal(f.nodes('input').length,1);
  await change(f,0,'hide-secret');await change(f,1,'hide-secret');f.setActive(false);await f.render();f.setActive(true);await f.render();
  assert.equal(f.nodes('input')[0].props.value,'');await change(f,0,'new-secret');assert.equal(f.nodes('input')[1].props.value,'','hide clears repetition');
  await change(f,1,'new-secret');f.setServer({question:f.question('password',{id:'c'.repeat(32),required:false,back:true})});await f.poll();
  assert.equal(f.nodes('input')[0].props.value,'');assert.equal(f.nodes('input').length,1);
  assert.equal(f.button('ociManager.next').props.disabled,false,'optional blank delegates generation to engine');
  await f.button('ociManager.next').props.onClick();assert.equal(JSON.parse(answers(f).at(-1).options.body).value,'');await f.render();
  f.setServer({question:f.question('password',{id:'d'.repeat(32),required:true,back:true})});await f.poll();
  await change(f,0,'incomplete');await f.button('ociManager.back').props.onClick();
  assert.deepEqual(JSON.parse(answers(f).at(-1).options.body).value,{back:true});await f.render();
  f.setServer({question:f.question('password',{id:'e'.repeat(32),required:true,back:true})});await f.poll();
  await change(f,0,'incomplete');await f.button('ociManager.cancel').props.onClick();
  assert.deepEqual(JSON.parse(answers(f).at(-1).options.body),{cancel:true});
 }
 console.log('PASS OCI settings EN/SK: native defaults, explicit final decline, repeat/required/optional secrets, no-effect guards, clearing, back/cancel');
})().catch(e=>{console.error(e);process.exit(1)});
