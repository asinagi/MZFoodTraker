const photoAssets=JSON.parse(document.getElementById("photo-assets").textContent);
document.querySelectorAll("img[data-photo]").forEach(i=>i.src=photoAssets[i.dataset.photo]);
const root=document.body;
const $=id=>document.getElementById(id);
const detailPhone=$('phone-detail'), savedPhone=$('phone-saved'), detailScroll=$('detail-scroll');
const params=new URLSearchParams(location.search);
const menuData={butter:{name:'버터떡',photo:document.querySelector('.menu-picture.butter img').src,description:'생지와 믹스를 함께 살펴봐요',keywords:'버터떡 버터 생지 믹스 디저트'},cookie:{name:'두바이 쫀득 쿠키',photo:document.querySelector('.menu-picture.cookie img').src,description:'메뉴와 확인할 정보를 살펴봐요',keywords:'두바이 쫀득 쿠키 디저트'}};
const products={dough:{name:'버터떡 휘낭시에 생지',maker:'모구모구 F&B',type:'빵류',ingredients:'전란·백설탕·버터·쌀가루 등',date:'2026.03.13'},mix:{name:'겉바속쫀 상하이 버터떡 믹스',maker:'(주)에버헬스케어 농업회사법인',type:'곡류가공품',ingredients:'카사바전분·찹쌀',date:'2026.03.23'}};
const statusLabels={active:'검토 중',paused:'보류',done:'검토 마침'};
const nextOptions=['구매처 확인하기','조리·보관 조건 확인','우리 매장과 맞는지 확인'];
const originalProducts=$('product-list').innerHTML;
const stores=new Map();
let auth=null,currentView='home',selectedMenu='butter',resumingMenu='butter',viewedProduct='dough';
let activeFilter='active',editKind='memo',pendingIntent=null,authOrigin={view:'home'},policyOrigin='account',resetOrigin='login';
let signupEmail='',resetEmail='',verifyExpired=false,resetExpired=false,searchMode='ready',lastSearch='',detailOrigin='home',lastSaveIntent=null;
let forcedError=params.get('error')||'',authStack=[],editingOwner=null;
const escapeHTML=value=>String(value??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const icon=(name,extra='')=>'<svg class="icon '+extra+'" aria-hidden="true"><use href="#i-'+name+'"></use></svg>';
const records=()=>auth?(stores.get(auth.email)||{}):{};
const record=key=>records()[key];
function setRecord(key,value){if(!auth)return;const current=stores.get(auth.email)||{};current[key]=value;stores.set(auth.email,current);persistMobile()}
function ensureStore(email){if(!stores.has(email))stores.set(email,{})}
function seedMember(empty=false){auth={email:'owner@example.com'};stores.set(auth.email,empty?{}:{butter:{product:'dough',status:'active',next:nextOptions[0],memo:'소량 구매가 가능한지 확인하기'},cookie:{product:null,status:'active',next:nextOptions[1],memo:'냉장 보관이 필요한지 확인하기'}})}
function toast(phone,message){const t=phone.querySelector('.toast');if(!t)return;t.textContent=message;t.style.display='block';clearTimeout(t._timer);t._timer=setTimeout(()=>t.style.display='none',2800)}
function closeSheets(){detailPhone.classList.remove('show-save','show-product','show-source');savedPhone.classList.remove('show-editor','show-confirm')}
function setView(name,shouldScroll=false){
 currentView=name;const wraps=[...document.querySelectorAll('.screen-wrap')];
 root.classList.remove('home','detail','saved');root.classList.toggle('single',name!=='all');root.dataset.view=name;
 if(['home','detail','saved'].includes(name))root.classList.add(name);
 wraps.forEach(w=>{w.classList.toggle('view-active',w.dataset.screen===name);if(root.classList.contains('render-phone'))w.classList.toggle('render-target',w.dataset.screen===name)});
 document.querySelectorAll('[data-view]').forEach(b=>b.classList.toggle('active',b.dataset.view===name));
 $('demo-mail-next').hidden=name!=='reset-confirm';
 if(shouldScroll&&!root.classList.contains('render-phone'))document.querySelector('.review-controls').scrollIntoView({behavior:'smooth',block:'start'});
 if(name==='account')renderAccount();if(name==='detail')requestAnimationFrame(currentSection);
 window.MZRoute?.onView(name);
}
function renderHome(){
 const saved=Object.keys(records());
 document.querySelectorAll('[data-account-entry]').forEach(b=>{b.innerHTML=auth?icon('user'):'로그인';b.setAttribute('aria-label',auth?'내 계정':'로그인')});
 $('home-member-resume').hidden=!auth||!saved.length;$('home-guest-note').hidden=!!auth;
 $('home-first-note').hidden=!auth||saved.length>0;
 if(saved.length){const key=saved[0],d=menuData[key],r=record(key),b=$('home-member-resume').querySelector('.resume-card');b.dataset.resume=key;b.querySelector('img').src=d.photo;b.querySelector('img').alt=d.name;b.querySelector('strong').textContent=d.name;b.querySelector('.next-small').innerHTML='<span class="caption">다음 확인</span>'+escapeHTML(r.next||'확인사항 정하기')}
}
function renderSaved(){
 $('saved-guest').hidden=!!auth;$('saved-member').hidden=!auth;
 if(!auth){$('saved-list').innerHTML='';savedPhone.classList.remove('view-resume');$('resume-memo').textContent='';$('resume-title').textContent='';return}
 document.querySelectorAll('[data-filter]').forEach(b=>{b.classList.toggle('active',b.dataset.filter===activeFilter);b.querySelector('small').textContent=Object.values(records()).filter(r=>r.status===b.dataset.filter).length});
 const list=Object.entries(records()).filter(([,r])=>r.status===activeFilter);
 $('saved-list').innerHTML=list.map(([key,r])=>'<article class="saved-card" data-menu="'+key+'"><button class="saved-main" data-resume="'+key+'"><img src="'+menuData[key].photo+'" alt="'+menuData[key].name+'"><div class="saved-copy"><h2>'+menuData[key].name+'</h2><p>'+(r.product?'후보 · '+escapeHTML(products[r.product].name):'메뉴만 저장했어요.')+'</p><p class="saved-date">'+statusLabels[r.status]+'</p></div>'+icon('next')+'</button><button class="saved-next" data-resume="'+key+'"><span><small>다음 확인</small><span>'+escapeHTML(r.next||'다음 확인사항 정하기')+'</span></span>'+icon('arrow')+'</button></article>').join('');
 const failed=forcedError==='saved';$('saved-load-error').hidden=!failed;$('saved-list').hidden=failed||!list.length;
 $('saved-empty').hidden=failed||list.length>0;
 $('empty-title').textContent=activeFilter==='paused'?'보류한 메뉴가 없어요':activeFilter==='done'?'검토를 마친 메뉴가 없어요':'아직 담아둔 메뉴가 없어요';
 $('empty-copy').innerHTML=activeFilter==='active'?'궁금한 메뉴를 찾고 검토에 담아보세요.<br>메뉴만 먼저 저장해도 괜찮아요.':'해당 상태의 검토가 아직 없어요.<br>검토 중인 메뉴는 다른 탭에서 볼 수 있어요.';
 $('saved-empty').querySelector('button').textContent=activeFilter==='active'?'메뉴 둘러보기':'검토 중인 메뉴 보기';
}
function renderAccount(){
 $('account-email').textContent=auth?auth.email:'로그인이 필요해요';
 $('account-identity').querySelector('.identity-label').textContent=auth?'이메일 계정':'비회원으로 둘러보는 중';
 $('account-identity').querySelector('.identity-note').innerHTML=auth?'저장한 메뉴와 메모를<br>이 계정으로 이어서 확인해요.':'메뉴는 바로 살펴볼 수 있어요.<br>개인 검토 기록은 로그인 후 이용해 주세요.';
 if(root.classList.contains('mobile-app'))$('account-identity').querySelector('.identity-note').textContent='검토 기록은 현재 브라우저에 저장됩니다. 다른 기기와 동기화되지 않습니다.';
 document.querySelector('[data-account-login]').hidden=!!auth;
 document.querySelectorAll('[data-member-tools]').forEach(el=>el.hidden=!auth);
}
function syncSaveButton(){const r=record(selectedMenu),b=detailPhone.querySelector('.detail-footer .primary');b.querySelector('span').textContent=r?'검토 이어보기':'내 검토에 담기';b.toggleAttribute('data-saved',!!r);detailPhone.querySelector('[data-save].icon-button').setAttribute('aria-label',r?'검토 이어보기':'검토에 담기')}
function renderGlobal(){renderHome();renderSaved();renderAccount();syncSaveButton();persistMobile();$('preview-session').textContent=auth?'로그인 상태 · '+auth.email:'비회원 상태'}
function openDetail(key,opts={}){
 if(!menuData[key])key='butter';if(!opts.preserveOrigin)detailOrigin=currentView==='search'?'search':'home';
 selectedMenu=key;const d=menuData[key];$('detail-menu-name').textContent=d.name;$('detail-photo').src=d.photo;$('detail-photo').alt=d.name+' 메뉴 연출 이미지';
 $('product-count').textContent=key==='butter'?'2':'';$('product-list').innerHTML=key==='butter'?originalProducts:'<p class="hint" style="padding:12px 0">연결된 제품 자료가 아직 없어요.<br>메뉴를 저장하고 필요한 재료부터 확인해 보세요.</p>';
 document.querySelector('[data-compare]').hidden=key!=='butter';closeSheets();syncSaveButton();if(!opts.keepScroll)detailScroll.scrollTop=0;setView('detail');
 $('detail-save-error').hidden=true;renderSourceState();
}
function currentSection(){const links=[...document.querySelectorAll('.section-tabs a')];let active=0;['interest','products','checks'].forEach((id,i)=>{if($(id).getBoundingClientRect().top<=detailScroll.getBoundingClientRect().top+72)active=i});links.forEach((a,i)=>{a.classList.toggle('current',i===active);a.setAttribute('aria-current',i===active?'true':'false')});detailPhone.querySelector('.back-title').textContent=detailScroll.scrollTop>210?menuData[selectedMenu].name:'메뉴 살펴보기'}
function openProduct(key){viewedProduct=key;const p=products[key];['name','maker','type','ingredients','date'].forEach(k=>$('product-'+k).textContent=p[k]);closeSheets();detailPhone.classList.add('show-product');window.MZRoute?.onProduct(key)}
function beginAuth(intent={kind:'return'}){
 pendingIntent=intent;authOrigin={view:currentView,menu:selectedMenu,scroll:detailScroll.scrollTop,product:detailPhone.classList.contains('show-product')?viewedProduct:null};authStack=[];
 $('login-context').textContent=intent.kind==='save'?menuData[intent.menu].name+'을 검토에 담으려면 로그인해 주세요. 완료 후 이 메뉴로 돌아와요.':'저장한 메뉴와 메모를 한곳에 모아보세요.';
 if(intent.kind==='save')$('form-login').querySelector('.primary').textContent='로그인하고 담기';else $('form-login').querySelector('.primary').textContent='로그인';
 setView('login');clearFormError('login');
}
function authDismiss(){pendingIntent=null;authStack=[];const origin=authOrigin;closeSheets();if(origin.view==='detail'){openDetail(origin.menu,{preserveOrigin:true});detailScroll.scrollTop=origin.scroll;if(origin.product)openProduct(origin.product)}else{setView(origin.view==='all'?'home':origin.view||'home');if(currentView==='saved')renderSaved()}}
function authPage(name){
 if(name==='account'){closeSheets();setView('account');return}
 if(name==='login'&&!['login','signup','verify','reset','reset-confirm','reset-new'].includes(currentView)){beginAuth({kind:'return'});return}
 if(name==='reset'){resetOrigin=currentView==='account'?'account':'login';$('reset-email').value=auth?.email||$('login-email').value||''}
 authStack.push(currentView);setView(name);
}
function authBack(){
 if(['terms','privacy'].includes(currentView)){setView(policyOrigin);return}
 if(currentView==='account'){setView('home');return}
 if(currentView==='withdraw'){setView('account');return}
 if(currentView==='login'){authDismiss();return}
 if(currentView==='verify'){setView('signup');return}
 if(currentView==='reset'){setView(resetOrigin);return}
 setView(authStack.pop()||'login');
}
function authenticated(email){
 auth={email:root.classList.contains('mobile-app')?'owner@example.com':email.trim().toLowerCase()};ensureStore(auth.email);renderGlobal();const intent=pendingIntent;pendingIntent=null;authStack=[];
 if(intent?.kind==='save'){openDetail(intent.menu,{preserveOrigin:true,keepScroll:true});detailScroll.scrollTop=authOrigin.scroll||0;performSave(intent);return}
 if(intent?.kind==='saved'){showSaved();return}
 if(intent?.kind==='account'){setView('account');return}
 if(authOrigin.view==='detail'){openDetail(authOrigin.menu,{preserveOrigin:true});detailScroll.scrollTop=authOrigin.scroll||0}else setView(authOrigin.view==='saved'?'saved':authOrigin.view==='account'?'account':'home');
}
function performSave(intent){
 lastSaveIntent={...intent};if(!auth){beginAuth(intent);return}
 if(forcedError==='save'){$('detail-save-error').hidden=false;closeSheets();return}
 $('detail-save-error').hidden=true;const existing=record(intent.menu);
 if(existing){if(intent.product!==undefined&&existing.product!==intent.product){existing.product=intent.product;setRecord(intent.menu,existing);renderGlobal();resumeMenu(intent.menu);toast(savedPhone,'제품 후보를 바꿨어요.')}else resumeMenu(intent.menu);return}
 setRecord(intent.menu,{product:intent.product??null,status:'active',next:null,memo:''});renderGlobal();closeSheets();
 detailPhone.querySelector('.save-sheet h2').textContent='검토에 담았어요';detailPhone.classList.add('show-save');
 const first=document.querySelector('input[name="next-step"]');if(first)first.checked=true;
}
function showSaved(){closeSheets();savedPhone.classList.remove('view-resume');renderSaved();setView('saved')}
function resumeMenu(key){
 if(!auth){beginAuth({kind:'saved'});return}const r=record(key);if(!r){showSaved();return}
 resumingMenu=key;const d=menuData[key];$('resume-photo').src=d.photo;$('resume-photo').alt=d.name;$('resume-title').textContent=d.name;$('resume-next-title').textContent=r.next||'다음 확인사항 정하기';
 $('resume-product-name').textContent=r.product?products[r.product].name:'제품 후보를 골라보세요';$('resume-product-maker').textContent=r.product?products[r.product].maker:'메뉴만 저장해 둔 상태예요';$('resume-memo').textContent=r.memo||'아직 메모를 남기지 않았어요.';$('resume-status').textContent=statusLabels[r.status];savedPhone.classList.add('view-resume');setView('saved');window.MZRoute?.onResume(key);
}
function setFilter(filter){activeFilter=filter;renderSaved()}
function openSavedEditor(kind){
 if(!auth)return;editKind=kind;editingOwner=auth.email;const r=record(resumingMenu);if(!r)return;
 const titles={memo:'내 메모 수정',status:'검토 상태 변경',next:'다음 확인사항 변경'};$('saved-editor-title').textContent=titles[kind];const field=$('saved-editor-fields');
 if(kind==='memo'){field.innerHTML='<label for="memo-input" class="editor-label">'+menuData[resumingMenu].name+'에 남길 메모</label><textarea id="memo-input" maxlength="250" aria-label="메모"></textarea>';field.querySelector('textarea').value=r.memo}
 if(kind==='status')field.innerHTML='<div class="radio-list">'+Object.entries(statusLabels).map(([k,label])=>'<label class="radio-row"><input type="radio" name="saved-status" value="'+k+'" '+(r.status===k?'checked':'')+'>'+label+'</label>').join('')+'</div>';
 if(kind==='next')field.innerHTML='<div class="radio-list">'+[...nextOptions,'나중에 정하기'].map(value=>'<label class="radio-row"><input type="radio" name="edit-next" value="'+value+'" '+((r.next||'나중에 정하기')===value?'checked':'')+'>'+value+'</label>').join('')+'</div>';
 $('edit-error').hidden=true;savedPhone.classList.add('show-editor');
}
function applySavedEditor(){
 if(!auth||auth.email!==editingOwner){$('edit-error').textContent='로그인한 계정을 다시 확인해 주세요. 입력한 내용은 아직 반영되지 않았어요.';$('edit-error').hidden=false;return}
 if(forcedError==='edit'){$('edit-error').textContent='변경사항을 저장하지 못했어요. 입력한 내용은 남아 있어요. 다시 시도해 주세요.';$('edit-error').hidden=false;forcedError='';return}
 const r=record(resumingMenu);if(!r)return;
 if(editKind==='memo')r.memo=$('memo-input').value.trim();
 if(editKind==='status')r.status=document.querySelector('input[name="saved-status"]:checked').value;
 if(editKind==='next'){const value=document.querySelector('input[name="edit-next"]:checked').value;r.next=value==='나중에 정하기'?null:value}
 setRecord(resumingMenu,r);savedPhone.classList.remove('show-editor');renderGlobal();resumeMenu(resumingMenu);toast(savedPhone,'변경사항을 남겼어요.');
}
function confirmRecordRemoval(){if(!record(resumingMenu))return;$('remove-menu-name').textContent=menuData[resumingMenu].name;savedPhone.classList.add('show-confirm')}
function removeRecord(){const store=records();delete store[resumingMenu];savedPhone.classList.remove('show-confirm');renderGlobal();showSaved();toast(savedPhone,'내 검토에서 삭제했어요.')}
function showSource(title,html){closeSheets();$('source-title').textContent=title;$('source-content').innerHTML=html;detailPhone.classList.add('show-source');window.MZRoute?.onSheet(title)}
function renderSourceState(){const failed=forcedError==='source';$('source-load-error').hidden=!failed;document.querySelector('.chart').toggleAttribute('hidden',failed);document.querySelector('.metric-sub').hidden=failed}
function openSearch(){lastSearch=$('search-query').value||'';setView('search');renderSearch();if(!root.classList.contains('render-phone'))$('search-query').focus()}
function renderSearch(mode='ready'){
 searchMode=mode;$('search-loading').hidden=mode!=='loading';$('search-error').hidden=mode!=='error';$('search-results').hidden=mode!=='ready';$('search-empty').hidden=true;
 if(mode!=='ready')return;
 const query=$('search-query').value.trim();lastSearch=query;const list=Object.entries(menuData).filter(([,m])=>!query||m.keywords.replace(/\s/g,'').includes(query.replace(/\s/g,'')));
 $('search-count').textContent=query?'“'+query+'” 검색 결과 '+list.length+'개':'메뉴 둘러보기';
 $('search-result-list').innerHTML=list.map(([key,d])=>'<button class="search-result" data-open-detail="'+key+'"><img src="'+d.photo+'" alt="'+d.name+'"><span><strong>'+d.name+'</strong><small>'+d.description+'</small></span>'+icon('next')+'</button>').join('');
 $('search-empty').hidden=list.length!==0;$('search-empty-query').textContent=query;
}
function runSearch(){window.MZRoute?.onSearch($('search-query').value.trim());renderSearch('loading');setTimeout(()=>renderSearch('ready'),250)}
function clearFormError(kind){const box=$(kind+'-error');if(box){box.hidden=true;box.textContent=''}if(kind==='reset-new')$('reset-expired-action').hidden=true;const form=$('form-'+kind);form?.querySelectorAll('[aria-invalid]').forEach(i=>i.removeAttribute('aria-invalid'))}
function formError(kind,message,field){const box=$(kind+'-error');box.textContent=message;box.hidden=false;if(field){const input=$(field);input.setAttribute('aria-invalid','true');input.setAttribute('aria-describedby',[input.getAttribute('aria-describedby'),kind+'-error'].filter(Boolean).join(' '));input.focus()}box.scrollIntoView({block:'nearest'})}
const validEmail=value=>/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(value);
function busy(form,callback){const b=form.querySelector('button[type="submit"]'),label=b.textContent;b.disabled=true;b.textContent='확인 중…';setTimeout(()=>{b.disabled=false;b.textContent=label;callback()},250)}
function consumeFailure(kind,message){if(forcedError===kind){forcedError='';formError(kind,message);return true}return false}
document.addEventListener('submit',e=>{
 e.preventDefault();const form=e.target;if(form.id==='form-search'){runSearch();return}
 const kind=form.id.replace('form-','');clearFormError(kind);
 if(kind==='login'){
  if(!validEmail($('login-email').value))return formError(kind,'이메일 주소를 확인해 주세요.','login-email');
  if(!$('login-password').value)return formError(kind,'비밀번호를 입력해 주세요.','login-password');
  return busy(form,()=>{if(consumeFailure(kind,'이메일 또는 비밀번호를 확인해 주세요. 다시 입력하거나 비밀번호를 재설정할 수 있어요.'))return;authenticated($('login-email').value)});
 }
 if(kind==='signup'){
  if(!validEmail($('signup-email').value))return formError(kind,'이메일 주소를 확인해 주세요.','signup-email');
  if($('signup-password').value.length<15)return formError(kind,'비밀번호를 15자 이상 입력해 주세요.','signup-password');
  if($('signup-password').value!==$('signup-password-confirm').value)return formError(kind,'두 비밀번호가 같지 않아요. 다시 확인해 주세요.','signup-password-confirm');
  if(!$('signup-terms').checked||!$('signup-privacy').checked)return formError(kind,'필수 안내를 읽고 동의 여부를 확인해 주세요.',!$('signup-terms').checked?'signup-terms':'signup-privacy');
  return busy(form,()=>{if(consumeFailure(kind,'가입 요청을 처리하지 못했어요. 입력한 내용은 남아 있어요. 잠시 후 다시 시도해 주세요.'))return;signupEmail=$('signup-email').value.trim().toLowerCase();$('verify-email').textContent=signupEmail;verifyExpired=false;$('verify-code').value='';clearFormError('verify');setView('verify')});
 }
 if(kind==='verify'){
  if(verifyExpired||forcedError==='verify'){verifyExpired=true;return formError(kind,'확인 코드가 만료됐어요. 새 코드를 받아 다시 입력해 주세요.','verify-code')}
  if($('verify-code').value!=='123456')return formError(kind,'확인 코드가 맞지 않아요. 받은 6자리 코드를 다시 입력해 주세요.','verify-code');
  return busy(form,()=>{authenticated(signupEmail||$('verify-email').textContent);$('signup-password').value='';$('signup-password-confirm').value='';$('verify-code').value=''});
 }
 if(kind==='reset'){
  if(!validEmail($('reset-email').value))return formError(kind,'이메일 주소를 확인해 주세요.','reset-email');
  return busy(form,()=>{if(consumeFailure(kind,'요청을 보내지 못했어요. 이메일 주소는 남아 있어요. 다시 시도해 주세요.'))return;resetEmail=$('reset-email').value.trim();$('reset-target-email').textContent=resetEmail;resetExpired=false;setView('reset-confirm')});
 }
 if(kind==='reset-new'){
  if(resetExpired)return formError(kind,'재설정 링크가 만료됐어요. 안내 메일을 다시 요청해 주세요.');
  if($('reset-new-password').value.length<15)return formError(kind,'비밀번호를 15자 이상 입력해 주세요.','reset-new-password');
  if($('reset-new-password').value!==$('reset-new-confirm').value)return formError(kind,'두 비밀번호가 같지 않아요. 다시 확인해 주세요.','reset-new-confirm');
  return busy(form,()=>{auth=null;renderGlobal();$('login-email').value=resetEmail||'owner@example.com';$('login-password').value='';$('reset-new-password').value='';$('reset-new-confirm').value='';setView('login');toast($('phone-login'),'비밀번호를 변경했어요. 다시 로그인해 주세요.');});
 }
 if(kind==='withdraw'){
  if(!auth)return beginAuth({kind:'account'});
  if(!$('withdraw-password').value)return formError(kind,'현재 비밀번호를 입력해 주세요.','withdraw-password');
  if(!$('withdraw-confirm').checked)return formError(kind,'탈퇴로 달라지는 내용을 확인해 주세요.','withdraw-confirm');
  return busy(form,()=>{stores.delete(auth.email);auth=null;pendingIntent=null;$('withdraw-password').value='';$('withdraw-confirm').checked=false;renderGlobal();setView('home');toast($('phone-home'),'탈퇴 처리가 완료됐어요.');});
 }
});
document.addEventListener('click',e=>{
 const b=e.target.closest('button,a,[data-close-sheet],[data-close-editor]');if(!b)return;if(b.getAttribute('href')==='#')e.preventDefault();
 if(b.dataset.view){closeSheets();if(b.dataset.view==='saved')showSaved();else if(b.dataset.view==='login')beginAuth({kind:'return'});else setView(b.dataset.view);return}
 if(b.dataset.go){closeSheets();if(b.dataset.go==='saved')showSaved();else setView(b.dataset.go);return}
 if(b.hasAttribute('data-account-entry')){if(auth)setView('account');else beginAuth({kind:'account'});return}
 if(b.dataset.openDetail){openDetail(b.dataset.openDetail);return}
 if(b.hasAttribute('data-detail-back')){closeSheets();setView(detailOrigin);return}
 if(b.dataset.resume){resumeMenu(b.dataset.resume);return}
 if(b.hasAttribute('data-back-saved')){showSaved();return}
 if(b.dataset.anchor){e.preventDefault();detailScroll.scrollTo({top:$(b.dataset.anchor).offsetTop-detailScroll.offsetTop-54,behavior:'smooth'});return}
 if(b.hasAttribute('data-save')){performSave({kind:'save',menu:selectedMenu});return}
 if(b.hasAttribute('data-close-sheet')){closeSheets();return}
 if(b.dataset.product){openProduct(b.dataset.product);return}
 if(b.hasAttribute('data-save-product')){performSave({kind:'save',menu:selectedMenu,product:viewedProduct});return}
 if(b.hasAttribute('data-save-next')){const r=record(selectedMenu);if(r){r.next=document.querySelector('input[name="next-step"]:checked').value;setRecord(selectedMenu,r)}renderGlobal();showSaved();return}
 if(b.hasAttribute('data-later')){renderGlobal();showSaved();return}
 if(b.dataset.filter){setFilter(b.dataset.filter);return}
 if(b.hasAttribute('data-empty-action')){if(activeFilter==='active')setView('home');else setFilter('active');return}
 if(b.hasAttribute('data-source')){showSource('자료 출처와 조회 조건','<p><strong>검색 관심 자료</strong></p><p>이 그래프는 예시 데이터입니다. 실제 관측값은 ‘이 메뉴의 관측 기록 보기’에서 출처·기간과 함께 확인하세요.</p><p>관심도와 판매량은 서로 다른 지표입니다.</p>');return}
 if(b.hasAttribute('data-search')){openSearch();return}
 if(b.hasAttribute('data-search-back')){setView('home');return}
 if(b.hasAttribute('data-search-clear')){$('search-query').value='';renderSearch();$('search-query').focus();return}
 if(b.hasAttribute('data-search-retry')){runSearch();return}
 if(b.hasAttribute('data-saved-retry')){forcedError='';renderSaved();return}
 if(b.hasAttribute('data-save-retry')){forcedError='';performSave(lastSaveIntent||{kind:'save',menu:selectedMenu});return}
 if(b.hasAttribute('data-source-retry')){forcedError='';renderSourceState();return}
 if(b.hasAttribute('data-other-products')||b.hasAttribute('data-resume-product')){const r=record(resumingMenu);openDetail(resumingMenu,{preserveOrigin:true});if(b.hasAttribute('data-resume-product')&&r?.product)openProduct(r.product);else requestAnimationFrame(()=>document.querySelector('[data-anchor="products"]').click());return}
 if(b.hasAttribute('data-recheck')){openDetail(resumingMenu,{preserveOrigin:true});return}
 if(b.hasAttribute('data-edit-next')){openSavedEditor('next');return}
 if(b.hasAttribute('data-status-info')){openSavedEditor('status');return}
 if(b.hasAttribute('data-edit-memo')){openSavedEditor('memo');return}
 if(b.hasAttribute('data-close-editor')){savedPhone.classList.remove('show-editor');return}
 if(b.hasAttribute('data-apply-editor')){applySavedEditor();return}
 if(b.hasAttribute('data-remove-review')){confirmRecordRemoval();return}
 if(b.hasAttribute('data-cancel-remove')){savedPhone.classList.remove('show-confirm');return}
 if(b.hasAttribute('data-confirm-remove')){removeRecord();return}
 if(b.hasAttribute('data-compare')){
  const items=Object.values(products);
  const fields=[['제조업체','maker'],['식품 유형','type'],['원재료 기재 내용','ingredients'],['보고일','date']];
  showSource('두 제품 비교','<p class="comparison-origin">제안서에 수록된 제품 기록 · 실시간 조회 아님</p><table class="product-matrix"><caption class="sr-only">같은 항목으로 비교하는 두 제품</caption><thead><tr><th scope="col">항목</th>'+items.map(p=>'<th scope="col">'+escapeHTML(p.name)+'</th>').join('')+'</tr></thead><tbody>'+fields.map(([label,key])=>'<tr><th scope="row">'+label+'</th>'+items.map(p=>'<td>'+escapeHTML(p[key])+'</td>').join('')+'</tr>').join('')+'</tbody></table><p>실제 공급 여부·가격·조리 방법은 별도 확인이 필요해요.</p>');return}

 if(b.dataset.authPage){authPage(b.dataset.authPage);return}
 if(b.hasAttribute('data-auth-back')){authBack();return}
 if(b.hasAttribute('data-auth-dismiss')){authDismiss();return}
 if(b.hasAttribute('data-account-login')){beginAuth({kind:'account'});return}
 if(b.hasAttribute('data-saved-login')){beginAuth({kind:'saved'});return}
 if(b.dataset.policy){policyOrigin=currentView;setView(b.dataset.policy);return}
 if(b.dataset.togglePassword){const input=$(b.dataset.togglePassword),show=input.type==='password';input.type=show?'text':'password';b.textContent=show?'숨김':'표시';b.setAttribute('aria-pressed',String(show));b.setAttribute('aria-label',show?'비밀번호 숨기기':'비밀번호 표시');return}
 if(b.hasAttribute('data-verify-resend')){verifyExpired=false;forcedError='';clearFormError('verify');$('verify-code').value='';toast($('phone-verify'),'새 확인 코드를 보냈어요.');return}
 if(b.hasAttribute('data-verify-change-email')){setView('signup');$('signup-email').focus();return}
 if(b.hasAttribute('data-reset-open')){resetExpired=false;clearFormError('reset-new');setView('reset-new');return}
 if(b.hasAttribute('data-logout')){auth=null;pendingIntent=null;closeSheets();renderGlobal();setView('home');toast($('phone-home'),'로그아웃했어요.');return}
 if(b.hasAttribute('data-withdraw')){if(!auth)beginAuth({kind:'account'});else{clearFormError('withdraw');setView('withdraw')}return}
 if(b.hasAttribute('data-preview-guest')){auth=null;pendingIntent=null;forcedError='';closeSheets();renderGlobal();setView('home');return}
 if(b.hasAttribute('data-preview-member')){seedMember();pendingIntent=null;forcedError='';closeSheets();renderGlobal();showSaved();return}
 if(b.hasAttribute('data-preview-new')){seedMember(true);pendingIntent=null;forcedError='';closeSheets();renderGlobal();showSaved();return}
});
document.addEventListener('keydown',e=>{if(e.key==='Escape'){if(detailPhone.classList.contains('show-save')||detailPhone.classList.contains('show-product')||detailPhone.classList.contains('show-source'))closeSheets();else if(savedPhone.classList.contains('show-editor')||savedPhone.classList.contains('show-confirm'))closeSheets();else if(['login','signup','verify','reset','reset-confirm','reset-new','terms','privacy','withdraw','account'].includes(currentView))authBack()}});
detailScroll.addEventListener('scroll',currentSection,{passive:true});
restoreMobile();
if(params.get('session')==='member')seedMember();if(params.get('session')==='new')seedMember(true);
if(Object.hasOwn(menuData,params.get('menu')))selectedMenu=params.get('menu');
if(params.has('screen'))root.classList.add('render-phone');if(params.has('full'))root.classList.add('full-content');
renderGlobal();
let screen=params.get('screen')||'home';
if(screen==='detail')openDetail(selectedMenu);
else if(screen==='search'){$('search-query').value=params.get('q')||'버터떡';setView('search');renderSearch(params.get('state')==='error'?'error':params.get('state')==='loading'?'loading':'ready')}
else if(screen==='login'&&params.get('intent')==='save'){authOrigin={view:'detail',menu:selectedMenu,scroll:0};pendingIntent={kind:'save',menu:selectedMenu};$('login-context').textContent=menuData[selectedMenu].name+'을 검토에 담으려면 로그인해 주세요. 완료 후 이 메뉴로 돌아와요.';$('form-login').querySelector('.primary').textContent='로그인하고 담기';setView('login')}
else setView(screen);
if(params.get('state')==='resume'){if(!auth)seedMember();renderGlobal();resumeMenu(params.get('menu')||'butter')}
if(params.get('state')==='empty'){if(!auth)seedMember(true);else stores.set(auth.email,{});renderGlobal();showSaved()}
if(params.get('state')==='paused'){activeFilter='paused';renderSaved()}
if(params.has('sheet')){if(params.get('sheet')==='save'){if(!auth)seedMember(true);performSave({kind:'save',menu:selectedMenu})}else detailPhone.classList.add('show-'+params.get('sheet'))}
if(params.has('data-view'))requestAnimationFrame(()=>{detailScroll.scrollTop=$('interest').offsetTop-detailScroll.offsetTop-54;currentSection()});
if(forcedError==='login')formError('login','이메일 또는 비밀번호를 확인해 주세요. 다시 입력하거나 비밀번호를 재설정할 수 있어요.');
if(forcedError==='signup'){$('signup-email').value='owner@example.com';$('signup-password').value='MZ-menu-preview-2026!';$('signup-password-confirm').value='MZ-menu-preview-2026!';$('signup-terms').checked=true;$('signup-privacy').checked=true;formError('signup','가입 요청을 처리하지 못했어요. 입력한 내용은 남아 있어요. 잠시 후 다시 시도해 주세요.')}
if(forcedError==='verify'){verifyExpired=true;formError('verify','확인 코드가 만료됐어요. 새 코드를 받아 다시 입력해 주세요.')}
if(forcedError==='reset'){$('reset-email').value='owner@example.com';formError('reset','요청을 보내지 못했어요. 이메일 주소는 남아 있어요. 다시 시도해 주세요.')}
if(forcedError==='reset-expired'){resetExpired=true;formError('reset-new','재설정 링크가 만료됐어요. 안내 메일을 다시 요청해 주세요.');$('reset-expired-action').hidden=false}
if(forcedError==='save'){$('detail-save-error').hidden=false;lastSaveIntent={kind:'save',menu:selectedMenu}}
if(forcedError==='edit'){if(!auth)seedMember();renderGlobal();resumeMenu('butter');openSavedEditor('memo');$('edit-error').hidden=false;$('edit-error').textContent='변경사항을 저장하지 못했어요. 입력한 내용을 확인하고 다시 시도해 주세요.'}
if(params.has('board')){root.classList.add('snapshot-board');setView('all')}
$('scenario-picker').addEventListener('change',e=>{if(e.target.value)location.search=e.target.value});
window.MZPreview={state:()=>({authenticated:!!auth,email:auth?.email||null,view:currentView,records:JSON.parse(JSON.stringify(records())),pending:pendingIntent?{...pendingIntent}:null,selectedMenu,viewedProduct}),failNext:kind=>forcedError=kind};


// The mobile prototype stores only demo records, never credentials.
function persistMobile(){
 if(!root.classList.contains('mobile-app'))return;
 try{localStorage.setItem('mz-maker:mobile-demo:v1',JSON.stringify({signedIn:!!auth,records:stores.get('owner@example.com')||{}}))}
 catch{document.body.dataset.storageWarning='true'}
}
function restoreMobile(){
 if(!root.classList.contains('mobile-app'))return;
 try{
 const data=JSON.parse(localStorage.getItem('mz-maker:mobile-demo:v1')||'{}'), safe={};
 for(const [key,r] of Object.entries(data.records||{})){
  if(!menuData[key]||!r||typeof r!=='object')continue;
  safe[key]={product:products[r.product]?r.product:null,status:statusLabels[r.status]?r.status:'active',next:typeof r.next==='string'?r.next.slice(0,250):null,memo:typeof r.memo==='string'?r.memo.slice(0,250):''};
 }
 stores.set('owner@example.com',safe);if(data.signedIn)auth={email:'owner@example.com'};
 }catch{}
}
window.MZApp={setView,openDetail,showSaved,resumeMenu,openProduct,closeSheets,beginAuth,renderSearch,renderGlobal,
 state:()=>({view:currentView,menu:selectedMenu,resume:resumingMenu,authenticated:!!auth}),
 compare:()=>document.querySelector('[data-compare]').click(),
 source:()=>document.querySelector('[data-source]').click()};
