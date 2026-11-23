/* Hash routes work on static hosting and survive refresh without server rewrites. */
(() => {
  'use strict';
  if (!document.body.classList.contains('mobile-app')) return;
  const app = window.MZApp, data = window.MZDataApp;
  let applying = false, lastHash = '', lastAppRoute = '/home';
  const titles = {home:'메뉴 찾기',search:'메뉴 검색',detail:'메뉴 살펴보기',saved:'내 검토',account:'내 계정',login:'로그인',signup:'회원가입',verify:'이메일 확인',reset:'비밀번호 재설정','reset-confirm':'재설정 안내','reset-new':'새 비밀번호',terms:'이용약관',privacy:'개인정보 안내',withdraw:'탈퇴 확인'};
  const dataRoutes = {api:'/observations',products:'/product-evidence',reviews:'/evidence-reviews'};
  const host = document.getElementById('data-workspace');
  const header = document.createElement('header');
  header.className = 'app-data-header';
  header.innerHTML = '<button type="button" aria-label="이전 메뉴로" id="app-data-back"><svg class="icon"><use href="#i-back"></use></svg></button><h1 id="app-data-title">자료 보기</h1><a href="#/home">홈</a>';
  host.before(header);
  document.getElementById('app-data-back').onclick = () => navigate(lastAppRoute);
  function updateNavigation(path) {
    const section = /^\/(product-evidence|observations)/.test(path) ? 'products'
      : /^\/(reviews|evidence-reviews)/.test(path) ? 'reviews' : 'discover';
    document.querySelectorAll('[data-editorial-nav]').forEach(link => {
      if (link.dataset.editorialNav === section) link.setAttribute('aria-current','page');
      else link.removeAttribute('aria-current');
    });
  }
  function write(path, replace = false) {
    if (applying) return;
    const hash = '#' + path;
    if (location.hash !== hash) history[replace ? 'replaceState' : 'pushState']({mz:true}, '', hash);
    lastHash = hash; updateNavigation(path);
  }
  function label(name) {
    document.title = 'MZ제조기 · ' + (titles[name] || name);
    requestAnimationFrame(() => {
      const screen = document.querySelector('.screen-wrap.view-active');
      const title = screen?.querySelector('h1') || screen?.querySelector('.back-title');
      if (title) {title.setAttribute('tabindex','-1'); title.focus({preventScroll:true});}
    });
  }
  window.MZRoute = {
    onView(name) {
      if (applying) return;
      data.hide(); document.body.classList.remove('welcome-active');
      const s = app.state();
      const path = name === 'detail' ? '/menus/' + s.menu : name === 'saved' ? (document.getElementById('phone-saved').classList.contains('view-resume') ? '/reviews/'+s.resume : '/reviews') : '/' + name;
      write(path); lastAppRoute = path; label(name);
      if (document.body.dataset.storageWarning) document.querySelector('.view-active .toast')?.replaceChildren(document.createTextNode('이 브라우저에서 기록을 보관할 수 없어요.'));
    },
    onResume(key) {write('/reviews/' + key);label('saved');},
    onProduct(key) {write('/menus/' + app.state().menu + '/products/' + key);},
    onSheet(title) {write('/menus/' + app.state().menu + (title === '두 제품 비교' ? '/compare' : '/source'));},
    onSearch(query) {write('/search' + (query ? '?q=' + encodeURIComponent(query) : ''));},
    onDataPane(pane) {
      if (pane === 'sample') return;
      const title = {api:'음식 관측',products:'제품 자료',reviews:'저장한 데이터 근거'}[pane];
      document.getElementById('app-data-title').textContent = title;
      document.title = 'MZ제조기 · ' + title;
      write(dataRoutes[pane]);
    }
  };
  function navigate(path, replace = false) {write(path,replace);applyRoute();}
  function applyRoute() {
    applying = true;
    try {
      const raw = location.hash.slice(1) || '/welcome';
      const [path, queryString = ''] = raw.split('?');
      updateNavigation(path);
      const route = path.split('/').filter(Boolean);
      const query = new URLSearchParams(queryString);
      app.closeSheets();
      document.body.classList.toggle('welcome-active',path==='/welcome');
      if(path==='/welcome') {
        data.hide();app.setView('home');document.title='MZ제조기 · 시작하기';
        requestAnimationFrame(()=>document.getElementById('welcome-title').focus({preventScroll:true}));
      } else if (['observations','product-evidence','evidence-reviews'].includes(route[0])) {
        const pane = {'observations':'api','product-evidence':'products','evidence-reviews':'reviews'}[route[0]];
        data.open(pane,query.get('q') || '');
      } else {
        data.hide();lastAppRoute = raw;
        if (route[0] === 'menus' && ['butter','cookie'].includes(route[1])) {
          app.openDetail(route[1]);
          if (route[2] === 'products' && ['dough','mix'].includes(route[3]) && route[1] === 'butter') app.openProduct(route[3]);
          if (route[2] === 'compare' && route[1] === 'butter') app.compare();
          if (route[2] === 'source') app.source();
          label('detail');
        } else if (route[0] === 'reviews') {
          if (['butter','cookie'].includes(route[1])) app.resumeMenu(route[1]); else app.showSaved();
          label(app.state().view);
        } else if (route[0] === 'search') {
          document.getElementById('search-query').value = query.get('q') || '';
          app.setView('search');app.renderSearch();label('search');
        } else {
          const name = Object.hasOwn(titles,route[0]) && !['detail','saved'].includes(route[0]) ? route[0] : 'home';
          app.setView(name);label(name);
        }
      }
    } finally {applying=false;lastHash=location.hash;}
  }
  window.addEventListener('hashchange', () => {if(location.hash!==lastHash)applyRoute();});
  document.addEventListener('click', e => {
    // Own in-app navigation: embedded browsers can cancel native fragment links.
    const link = e.target.closest('a[href^="#/"]');
    if (link && e.button === 0 && !e.metaKey && !e.ctrlKey && !e.shiftKey && !e.altKey
        && !link.hasAttribute('download') && (!link.target || link.target === '_self')) {
      e.preventDefault();
      navigate(link.getAttribute('href').slice(1));
      return;
    }
    if (e.target.closest('[data-app-observations]')) {
      lastAppRoute = '/menus/' + app.state().menu;
      navigate('/observations?q=' + encodeURIComponent(app.state().menu === 'butter' ? '버터떡' : '두바이쫀득'));
    }
    if (e.target.closest('[data-app-products]')) {
      const key=app.state().menu; lastAppRoute='/menus/'+key;
      data.setMenuContext({food_id:'prototype:'+key,name:key==='butter'?'버터떡':'두바이 쫀득 쿠키',origin:'목업 메뉴 · B8 음식 ID 미연결',evidence:null});
      navigate('/product-evidence');
    }
    if (e.target.closest('[data-close-sheet]')) write('/menus/' + app.state().menu);
  });
  document.addEventListener('keydown', e => {if(e.key==='Escape' && app.state().view==='detail')write('/menus/'+app.state().menu);});
  // Keep authentication explicitly a prototype, without a dashboard over the app.
  document.querySelectorAll('.auth-content').forEach(content => {
    const note = document.createElement('p');note.className='app-demo-note';
    note.textContent='계정 연결 전입니다. 예시 정보만 입력해 주세요. 검토는 현재 브라우저에 저장됩니다.';
    content.prepend(note);
  });
  const verifyNote = document.querySelector('#phone-verify .app-demo-note');
  verifyNote.textContent += ' 체험 확인 코드: 123456.';
  const resetButton = document.createElement('button');resetButton.className='secondary-button app-auth-continue';resetButton.textContent='체험용 재설정 링크 열기';
  resetButton.onclick=()=>app.setView('reset-new');document.querySelector('#phone-reset-confirm .auth-content').append(resetButton);
  const productLink=document.createElement('button');productLink.dataset.appProducts='';productLink.textContent='제품 자료로 후보 비교하기 ↗';
  document.querySelector('.app-data-link').append(productLink);
  document.querySelector('#phone-saved #saved-member').insertAdjacentHTML('beforeend','<a class="app-evidence-link" href="#/evidence-reviews">저장한 데이터 근거 보기 <span>↗</span></a>');
  document.querySelector('#account-identity .identity-note').textContent = '검토 기록은 현재 브라우저에 저장됩니다. 다른 기기와 동기화되지 않습니다.';
  if (!location.hash) history.replaceState({mz:true},'','#/welcome');
  applyRoute();
})();
