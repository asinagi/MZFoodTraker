const $ = id => document.getElementById(id);
const fmt = value => value ? new Intl.DateTimeFormat('ko-KR', {timeZone:'Asia/Seoul',year:'numeric',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',second:'2-digit',hour12:false}).format(new Date(value)) : '미제공';
function node(tag, text, cls) { const n=document.createElement(tag); if(text!==undefined)n.textContent=text; if(cls)n.className=cls; return n; }
function link(news) { let url; try {url=new URL(news.url);}catch{return node('span',news.title || '제목 미제공');} if(!['https:','http:'].includes(url.protocol))return node('span',news.title || '제목 미제공'); const a=node('a',news.title || '뉴스 링크'); a.href=url.href; a.target='_blank'; a.rel='noopener noreferrer'; return a; }
async function api(path,options) { const r=await fetch(path,options); const data=await r.json(); if(!r.ok)throw new Error(data.error || data.message || `HTTP ${r.status}`); return data; }
function cards(id, values) {
  $(id).replaceChildren();
  for (const [label,value] of values) { const c=node('div',undefined,'card'); c.append(node('span',label),node('strong',String(value))); $(id).append(c); }
}
async function status() {
  const s=await api('/api/status'), r=s.latest_run;
  cards('summary', [['마지막 수집',r?fmt(r.finished_at):'수집 전'],['최근 수집 결과',r?(r.success?'성공':'실패'):'대기'],['누적 저장 항목',s.total_keywords],['누적 음식 후보',s.food_count],['누적 검토 필요',s.review_count]]);
  cards('recent-summary', [['최근 응답 원본 항목',r?.success?r.item_count:'확인 불가'],['최근 신규 저장',r?.success?r.new_count:'확인 불가'],['최근 응답 FOOD (현재 분류)',r?.success?r.current_food_count:'확인 불가'],['최근 응답 REVIEW (현재 분류)',r?.success?r.current_review_count:'확인 불가'],['신규 발견 상태',!r?'수집 전':!r.success?'수집 실패':r.new_count?'신규 항목 발견':'신규 없음']]);
  $('collect').disabled=s.collecting;
  $('collect').textContent=s.collecting?'수집 중…':'지금 수집';
  $('status').className=r&&!r.success?'error':'';
  const result = !r?'수집을 기다리는 중입니다.':r.success?`RSS 수집 성공 · ${r.new_count?'신규 '+r.new_count+'개 저장':'신규 데이터 없음'} · 주기 ${s.interval_seconds/60}분${r.warnings.length?' · '+r.warnings.join(' / '):''}`:`수집 실패: ${r.error} · 기존 저장 데이터 표시 · 마지막 성공: ${fmt(s.last_success_at)}`;
  $('status').textContent=(s.collecting?'RSS 수집 중 · ':'')+result;
  $('freshness').textContent=`서로 다른 검색어 ${s.distinct_keywords}개 · 마지막 신규 발견 ${fmt(s.last_discovery_at)} · 최근 응답의 최신 게시 시각 ${fmt(r?.latest_publication_at)}${r?.feed_age_seconds>86400?' · 주의: 최근 성공 응답의 게시 시각이 수집 시각보다 24시간 이상 오래됐습니다.':''} · 최근 응답 후보 수는 해당 응답 항목에 현재 분류기를 적용한 값입니다.`;
}
let listSequence=0;
async function list() {
  const seq=++listSequence, filter=$('filter').value;
  const data=await api(`/api/trends?classification=${filter}&q=${encodeURIComponent($('search').value)}`);
  if(seq!==listSequence)return;
  $('rows').replaceChildren(); $('count').textContent=`${data.items.length}개 항목`;
  for(const item of data.items) {
    const row=node('tr'); row.dataset.id=item.id; row.append(node('td',fmt(item.first_seen)));
    const title=node('td'), button=node('button',item.keyword,'keyword'); button.onclick=()=>detail(item.id); title.append(button);
    row.append(title,node('td',item.traffic??'미제공')); const cls=node('td'); cls.append(node('span',item.classification,`badge ${item.classification}`));row.append(cls);
    const news=node('td'); if(item.news.length)news.append(link(item.news[0]));else news.textContent='미제공';row.append(news);
    row.onclick=e=>{if(!e.target.closest('a,button'))detail(item.id);};$('rows').append(row);
  }
  if(!data.items.length){const td=node('td',filter==='FOOD'&&!$('search').value?'음식 후보가 발견되지 않았습니다.':'현재 조건에 해당하는 저장 항목이 없습니다.');td.colSpan=5;const row=node('tr');row.append(td);$('rows').append(row);}
}
async function detail(id) {
  try {
    const r=await api(`/api/trends/${id}`);$('detail-title').textContent=r.keyword;$('detail-body').replaceChildren(); const body=$('detail-body');
    for(const [label,value] of [['정규화 후보명',r.normalized_name],['추정 개체 유형',r.entity_type],['최종 분류',r.classification],['규칙 점수',`${r.rule_score??'미기록'} / 100 — ${r.score_basis??'이전 버전 미기록'}`],['분류기 버전',r.classifier_version],['분류 시각',fmt(r.classified_at)],['게시 시각',fmt(r.published_at)],['항목 최초 / 최근 발견',`${fmt(r.first_seen)} / ${fmt(r.last_seen)}`],['동일 원본 검색어 최초 / 최근 발견',`${fmt(r.keyword_first_seen)} / ${fmt(r.keyword_last_seen)} · 게시 시점 ${r.occurrence_count}개`],['트래픽 (RSS 원본)',r.traffic??'미제공'],['관련 검색어',r.related_queries?.join(', ')||'RSS 미제공'],['원본 식별자',r.source_id]])body.append(node('p',`${label}: ${value}`));
    body.append(node('h3','분류 근거'));const reasons=node('ul');r.reasons.forEach(x=>reasons.append(node('li',x)));body.append(reasons,node('h3','분류에 참고한 뉴스 관계'));
    if(!r.evidence_news?.length)body.append(node('p','음식 직접 근거로 사용한 뉴스가 없습니다.'));
    for(const n of r.evidence_news||[]) {const p=node('p');p.append(link(n),node('span',` · 검색어 ${n.target_mentioned?'직접 언급':'미언급'} · ${n.relation} · ${n.signals.join(' / ')||'배경 표현은 음식 근거에서 제외'}`));body.append(p);}
    body.append(node('h3','RSS 관련 뉴스 전체'));
    if(!r.news.length)body.append(node('p','RSS 미제공'));for(const n of r.news){const p=node('p');p.append(link(n),node('span',` · ${n.source??'출처 미제공'}`));body.append(p);}
    const naverPanel=node('div');body.append(naverPanel);mountNaver(r,naverPanel);
    const igPanel=node('div');body.append(igPanel);body.append(node('h3','원본 RSS 항목'),node('pre',r.raw_xml));$('detail').showModal();mountInstagram(r,igPanel);
  }catch(e){$('status').textContent=e.message;}
}
async function refresh(){try{await Promise.all([status(),list(),refreshInstagramOverview(),refreshNaverOverview(),refreshYouTubeOverview()]);}catch(e){$('status').className='error';$('status').textContent=`조회 실패: ${e.message} · 표시 중인 데이터는 기존 저장 데이터입니다.`;}}
$('collect').onclick=async()=>{$('collect').disabled=true;$('status').textContent='수집 요청 중…';try{const r=await api('/api/collect',{method:'POST'});if(!r.success)throw new Error(r.error);}catch(e){$('status').textContent=`수집 실패: ${e.message}`;}finally{await refresh();}};
$('filter').onchange=()=>refresh();let debounce;$('search').oninput=()=>{clearTimeout(debounce);debounce=setTimeout(refresh,200);};$('close').onclick=()=>$('detail').close();refresh();setInterval(refresh,5000);
