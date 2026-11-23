/* DOM behavior regression, not a visual/real-device test. Requires jsdom. */
const {JSDOM,VirtualConsole}=require('jsdom');
const fs=require('node:fs'),path=require('node:path'),assert=require('node:assert/strict'),{webcrypto}=require('node:crypto');
const root=path.resolve(__dirname,'../public'),errors=[];
const bundle=JSON.parse(fs.readFileSync(path.join(__dirname,'synthetic-report.json')));
bundle.ingredient_suppliers=[{ingredient:'피스타치오',search_term:'피스타치오',source:'검증용 합성 I1250 형식',fetched_at:'2026-11-17T00:00:00Z',counts:{products:4,ingredient_use:2},products:[{report_no:'TEST_I001',name:'[TEST] 피스타치오 페이스트',maker:'가상 제조사 A',ingredient_use:true,basis:'name',usage:'제과 원료'},{report_no:'TEST_I002',name:'[TEST] 피스타치오 분말',maker:'가상 제조사 B',ingredient_use:true,basis:'usage',usage:'제빵 원료'}]}];
const tick=()=>new Promise(r=>setTimeout(r,40));
async function boot(hash,stored){
 const vc=new VirtualConsole();vc.on('jsdomError',e=>errors.push(e.message));
 const dom=new JSDOM(fs.readFileSync(path.join(root,'index.html'),'utf8'),{
  url:'http://localhost/'+hash,runScripts:'outside-only',pretendToBeVisual:true,virtualConsole:vc,
  beforeParse(w){
   Object.defineProperty(w.crypto,'subtle',{value:webcrypto.subtle});w.TextEncoder=TextEncoder;
   w.HTMLElement.prototype.scrollIntoView=function(){};w.HTMLElement.prototype.scrollTo=function(){};
   w.fetch=async url=>({ok:true,json:async()=>JSON.parse(fs.readFileSync(path.join(root,String(url)),'utf8'))});
   if(stored)w.localStorage.setItem('mz-maker:data-reviews:v1',stored);
  }
 });
 for(const script of dom.window.document.querySelectorAll('script')) {if(script.type&&!['text/javascript','application/javascript','module'].includes(script.type))continue;dom.window.eval(script.src?fs.readFileSync(path.join(root,new URL(script.src).pathname),'utf8'):script.textContent);}
 await tick();return dom;
}
async function upload(w,contents=JSON.stringify(bundle)){
 const input=w.document.getElementById('report-file');Object.defineProperty(input,'files',{configurable:true,value:[{name:'synthetic-bundle.json',size:Buffer.byteLength(contents),text:async()=>contents}]});input.dispatchEvent(new w.Event('change'));await tick();
}
async function route(w,hash){w.location.hash=hash;await tick();}
(async()=>{
 let welcome=await boot('');
 assert.equal(welcome.window.location.hash,'#/welcome');
 assert.ok(welcome.window.document.body.classList.contains('welcome-active'));
 // Embedded browsers may cancel native link navigation; the app must route explicitly.
 welcome.window.document.addEventListener('click', e=>{if(e.target.closest('.welcome-start'))e.preventDefault();},true);
 welcome.window.document.querySelector('.welcome-start span').click();await tick();
 assert.equal(welcome.window.location.hash,'#/home');
 assert.equal(welcome.window.document.body.classList.contains('welcome-active'),false);
 await route(welcome.window,'#/welcome');assert.ok(welcome.window.document.body.classList.contains('welcome-active'));
 welcome.window.document.querySelector('.welcome-start').dispatchEvent(new welcome.window.MouseEvent('click',{bubbles:true,cancelable:true,ctrlKey:true}));
 assert.equal(welcome.window.location.hash,'#/welcome');
 welcome.window.document.querySelector('.welcome-resume').click();await tick();
 assert.equal(welcome.window.location.hash,'#/reviews');
 assert.equal(welcome.window.document.body.classList.contains('welcome-active'),false);
 welcome.window.history.back();await tick();
 assert.equal(welcome.window.location.hash,'#/welcome');
 assert.ok(welcome.window.document.body.classList.contains('welcome-active'));
 welcome.window.close();
 let dom=await boot('#/menus/butter'),w=dom.window,d=w.document;
 assert.equal(d.body.classList.contains('welcome-active'),false);
 d.querySelector('[data-app-products]').click();await tick();assert.equal(w.location.hash,'#/product-evidence');
 assert.equal(d.querySelector('[data-editorial-nav=products]').getAttribute('aria-current'),'page');
 await upload(w);assert.equal(d.querySelectorAll('#report-choice option').length,2);
 d.getElementById('ingredient-query').value='버터';d.getElementById('ingredient-query').dispatchEvent(new w.Event('input'));assert.equal(d.querySelectorAll('.product-evidence-card').length,1);
 d.getElementById('report-choice').value='1';d.getElementById('report-choice').dispatchEvent(new w.Event('change'));assert.equal(d.querySelectorAll('.product-evidence-card').length,2);
 for(const c of d.querySelectorAll('[data-compare-no]')){c.checked=true;c.dispatchEvent(new w.Event('change'));}
 assert.equal(d.getElementById('compare-products').disabled,false);d.getElementById('compare-products').click();assert.equal(d.querySelectorAll('#comparison thead th[scope=col]:not(:first-child)').length,2);
 d.getElementById('link-menu').checked=true;d.querySelector('[data-product-save]').click();assert.match(d.getElementById('data-message').textContent,/이유/);
 d.getElementById('link-reason').value='제과 원료 용도 기재 확인';d.querySelector('[data-product-save]').click();assert.match(d.getElementById('data-message').textContent,/저장/);
 await route(w,'#/evidence-reviews');assert.equal(d.querySelector('.review-reason').value,'제과 원료 용도 기재 확인');assert.equal(d.querySelector('[data-editorial-nav=reviews]').getAttribute('aria-current'),'page');
 await route(w,'#/product-evidence');assert.equal(d.getElementById('report-choice').value,'1');assert.match(d.querySelector('#report-body h3').textContent,/피스타치오/);await route(w,'#/evidence-reviews');
 const stored=w.localStorage.getItem('mz-maker:data-reviews:v1');dom.window.close();
 dom=await boot('#/evidence-reviews',stored);w=dom.window;d=w.document;assert.equal(d.querySelector('.review-reason').value,'제과 원료 용도 기재 확인');
 await route(w,'#/observations?q=두바이쫀득');assert.ok(d.querySelector('[data-food-index]'));d.querySelector('[data-food-index]').click();await tick();assert.ok(d.querySelector('.live-column'));
 d.getElementById('go-products').click();await upload(w);d.getElementById('link-menu').checked=true;d.getElementById('link-reason').value='합성 자료의 관측과 제품 연결 시험';d.querySelector('[data-product-save]').click();
 const records=Object.values(JSON.parse(w.localStorage.getItem('mz-maker:data-reviews:v1')));assert.ok(records.some(r=>r.evidence?.series?.points.length));assert.ok(records.every(r=>r.product_evidence.file.sha256.length===64));
 await upload(w,'{');assert.match(d.getElementById('data-message').textContent,/이전 자료/);assert.equal(d.querySelectorAll('.product-evidence-card').length,2);
 await route(w,'#/evidence-reviews');assert.ok(d.querySelector('.snapshot-points'));
 await route(w,'#/search?q=버터');assert.equal(d.getElementById('search-query').value,'버터');
 assert.deepEqual(errors,[]);dom.window.close();
 console.log(JSON.stringify({checks:12,brandEntry:'root+embedded-start+resume+back+deepLink pass',reportFormats:'food+ingredient',comparison:'pass',relationReason:'required',savedEvidence:'snapshot+file hash',reload:'pass',invalidFile:'previous report preserved',pageErrors:errors,visualQA:false}));
})().catch(e=>{console.error(e);process.exitCode=1;});
