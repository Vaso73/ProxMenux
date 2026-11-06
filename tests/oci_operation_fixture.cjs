// Execute shipped handlers/effects, not a second model of the wizard.
const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm'),ts=require('typescript');
const compile=s=>ts.transpileModule(s,{compilerOptions:{module:ts.ModuleKind.CommonJS,jsx:ts.JsxEmit.ReactJSX}}).outputText;
function load(file,imports={}){const exports={};vm.runInNewContext(compile(fs.readFileSync(file,'utf8')),{exports,require:k=>{if(k in imports)return imports[k];throw Error(k)},Set,Number,Object,Array,JSON,TextEncoder});return exports;}
const model=load('AppImage/lib/oci-manager.ts');
const flatten=n=>!n||typeof n==='boolean'?[]:Array.isArray(n)?n.flatMap(flatten):typeof n==='object'?[n,...flatten(n.props?.children)]:[n];
const text=n=>flatten(n).filter(v=>typeof v==='string'||typeof v==='number').join('');
function fixture(locale,{resume=false,invalid=false,initialSource,active=true,answerFailure=false,delayContext=false}={}){
 let slots=[],cursor=0,effects=[],tree,timers=new Map(),nextTimer=0,language=locale;
 const messages=JSON.parse(fs.readFileSync('AppImage/messages/'+locale+'/common.json'));
 const t=(k,params={})=>{const v=k.split('.').reduce((o,k)=>o?.[k],messages);assert.equal(typeof v,'string',k);return v.replace(/\{(\w+)\}/g,(match,key)=>params[key]??match)};
 const calls=[],now=1700000000000;
 const question=(kind,more={})=>({id:'b'.repeat(32),kind,text:'Native question',title:null,default:null,options:[],back:false,expires_at:now/1000+900,...more});
 let server={id:'a'.repeat(32),operation:'custom.install',parameters:{},node:'test-node',status:'question',question:question('choose',{options:[['paste','Compose'],['run','docker run'],['image','Image'],['file','Host file'],['url','URL'],['oci-compose-paste','OCI text'],['oci-compose','OCI file'],['oci-compose-url','OCI URL']]}),created_at:now/1000,server_time:now/1000,result:null,error:null};
 const react={useState(v){const i=cursor++;if(!(i in slots))slots[i]=v;return[slots[i],v=>slots[i]=typeof v==='function'?v(slots[i]):v]},useRef(v){const i=cursor++;return slots[i]??(slots[i]={current:v})},useEffect(fn,deps){const i=cursor++,old=slots[i];if(!old||deps.some((v,j)=>v!==old.deps[j])){old?.cleanup?.();slots[i]={deps};effects.push(()=>slots[i].cleanup=fn())}}};
 const jsx=(type,props)=>({type,props});
 let releaseContext;
 async function fetchApi(path,options){calls.push({path,options});if(path.endsWith('/context')){if(delayContext)await new Promise(r=>releaseContext=r);return{success:true,node:'test-node',operations:['custom.install']}};if(answerFailure&&path.endsWith('/answer'))throw Error('fixture answer failure');if(invalid)return{success:true,job:{status:'completed'}};return{success:true,job:structuredClone(server)}}
 const exports={};vm.runInNewContext(compile(fs.readFileSync('AppImage/components/oci-manager-operation.tsx','utf8')),{exports,require:k=>({'react':react,'react/jsx-runtime':{jsx,jsxs:jsx},'../lib/api-config':{fetchApi},'../lib/i18n/provider':{useI18n:()=>({t,language})},'../lib/oci-manager':model,'./ui/button':{Button:'button'},'./ui/input':{Input:'input'}}[k]),crypto:{randomUUID:()=> '1'.repeat(32)},Date:class extends Date{static now(){return now+3600000}},JSON,Error,TextEncoder,setInterval:fn=>{const id=++nextTimer;timers.set(id,fn);return id},clearInterval:id=>timers.delete(id)});
 const props=resume?{resume:server.id}:{operation:'custom.install',parameters:{},initialSource,active};
 async function render(){for(let i=0;i<5;i++){cursor=0;tree=exports.OciManagerOperation(props);if(!slots[0]){assert.ok(!text(tree).includes(t('ociManager.working')),'no job is not a running operation');assert.ok(!text(tree).includes(t('ociManager.background')),'no job has no background operation');}const work=effects;effects=[];work.forEach(f=>f());await new Promise(r=>setImmediate(r))}return tree}
 return{render,calls,t,get tree(){return tree},setServer(v){server={...server,...v}},setActive(v){props.active=v},releaseContext:()=>releaseContext(),setLanguage(v){language=v},question,async poll(){for(const fn of [...timers.values()])await fn();await render()},nodes(type){return flatten(tree).filter(n=>n.type===type)},button(key){const n=flatten(tree).find(n=>n.type==='button'&&text(n)===t(key));assert.ok(n,key);return n}};
}
module.exports={fixture,text};
