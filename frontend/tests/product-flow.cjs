/* Optional browser regression: node tests/product-flow.cjs (Playwright required).
   Synthetic data only. Starts and stops its own loopback server. */
const {chromium}=require('playwright'),{spawn}=require('node:child_process'),fs=require('node:fs'),path=require('node:path'),assert=require('node:assert/strict');
const root=path.resolve(__dirname,'..');
const server=spawn('python3',['-B','serve.py','--port','8189'],{cwd:root,stdio:['ignore','pipe','pipe']});
const bundle=JSON.parse(fs.readFileSync(path.join(__dirname,'synthetic-report.json')));
bundle.ingredient_suppliers=[{ingredient:'피스타치오',search_term:'피스타치오',source:'검증용 합성 I1250 형식',fetched_at:'2026-11-17T00:00:00Z',counts:{products:4,ingredient_use:2},products:[{report_no:'TEST_I001',name:'[TEST] 피스타치오 페이스트',maker:'가상 제조사 A',ingredient_use:true,basis:'name',usage:'제과 원료'},{report_no:'TEST_I002',name:'[TEST] 피스타치오 분말',maker:'가상 제조사 B',ingredient_use:true,basis:'usage',usage:'제빵 원료'}]}];
const upload={name:'synthetic-bundle.json',mimeType:'application/json',buffer:Buffer.from(JSON.stringify(bundle))};
let browser,page;const errors=[];
(async()=>{
 await new Promise((resolve,reject)=>{server.stdout.once('data',resolve);server.once('error',reject);server.once('exit',code=>reject(Error('server exited '+code)));});
 browser=await chromium.launch({headless:true,executablePath:process.env.CHROMIUM_PATH || (fs.existsSync("/usr/bin/chromium")?"/usr/bin/chromium":undefined),args:["--no-sandbox"]});page=await browser.newPage({viewport:{width:390,height:844}});
 page.on('pageerror',e=>errors.push(e.message));
 await page.goto('http://127.0.0.1:8189/#/menus/butter');
 await page.locator('[data-app-products]').click();
 await page.locator('#report-file').setInputFiles(upload);
 await page.locator('#report-choice').waitFor();
 await page.locator('#ingredient-query').fill('버터');
 assert.equal(await page.locator('.product-evidence-card').count(),1);
 await page.locator('#report-choice').selectOption('1');
 assert.equal(await page.locator('.product-evidence-card').count(),2);
 const checks=page.locator('[data-compare-no]');await checks.nth(0).check();await checks.nth(1).check();
 await page.locator('#compare-products').click();assert.equal(await page.locator('#comparison thead th[scope=col]:not(:first-child)').count(),2);
 await page.locator('#link-menu').check();await page.locator('[data-product-save]').first().click();
 assert.match(await page.locator('#data-message').innerText(),/이유/);
 await page.locator('#link-reason').fill('용도 원문에 제과 원료가 기재되어 시식 후보로 검토');
 await page.locator('[data-product-save]').first().click();
 assert.match(await page.locator('#data-message').innerText(),/저장/);
 await page.locator('a[href="#/evidence-reviews"]').filter({visible:true}).click();
 assert.match(await page.locator('.review-data-cards').innerText(),/목업|사용자가 관련성/);
 await page.reload();assert.equal(await page.locator('.review-reason').inputValue(),'용도 원문에 제과 원료가 기재되어 시식 후보로 검토');
 await page.goto('http://127.0.0.1:8189/#/observations?q=두바이쫀득');
 await page.locator('[data-food-index]').first().click();await page.locator('.live-column').first().waitFor();
 await page.locator('#go-products').click();await page.locator('#report-file').setInputFiles(upload);await page.locator('#report-choice').waitFor();
 await page.locator('#link-menu').check();await page.locator('#link-reason').fill('합성 제품 예시의 연결·보존 테스트');await page.locator('[data-product-save]').first().click();
 const snapshots=await page.evaluate(()=>Object.values(JSON.parse(localStorage.getItem('mz-maker:data-reviews:v1'))));
 assert.ok(snapshots.some(r=>r.evidence?.series?.points.length>0));
 assert.ok(snapshots.every(r=>r.product_evidence.file.sha256.length===64));
 await page.locator('#report-file').setInputFiles({name:'invalid.json',mimeType:'application/json',buffer:Buffer.from('{')});
 await page.waitForFunction(()=>document.getElementById('data-message').textContent.includes('이전 자료'));
 assert.equal(await page.locator('.product-evidence-card').count(),2);
 for(const width of [320,390]){
  await page.setViewportSize({width,height:844});
  const widths=await page.evaluate(()=>({root:document.documentElement.scrollWidth,view:innerWidth,host:document.getElementById('data-workspace').scrollWidth,hostClient:document.getElementById('data-workspace').clientWidth}));
  assert.ok(widths.root<=width&&widths.host<=widths.hostClient+1,JSON.stringify(widths));
 }
 await page.goto('http://127.0.0.1:8189/#/search?q=버터');await page.reload();assert.equal(await page.locator('#search-query').inputValue(),'버터');
 assert.deepEqual(errors,[]);
 console.log(JSON.stringify({productImport:'food+ingredient bundle',comparison:'two products',relationReason:'required',snapshot:'preserved with 0/null and source',refresh:'review kept',widths:[320,390],pageErrors:errors}));
})().catch(async e=>{console.error(e);if(page)console.error((await page.locator('body').innerText()).slice(-3000));process.exitCode=1;}).finally(async()=>{if(browser)await browser.close();server.kill('SIGTERM');});
