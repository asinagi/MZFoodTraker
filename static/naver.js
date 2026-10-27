/* Results live only in the current view. No localStorage, IndexedDB or response cache. */
const naverLabels={not_configured:'API 키 미설정 · .env에 NAVER 인증정보 입력 후 서버를 재시작하세요.',unverified:'연결 미검증 · 키워드를 선택해 조회하세요.',success:'정상 조회',no_data:'조회 기간에 데이터가 없습니다.',authentication:'인증 실패 · Client ID, Secret 및 API 권한을 확인하세요.',network:'네트워크 오류 · 다시 조회하세요.',upstream:'NAVER 서버 오류',rate_limit:'NAVER 호출량 제한 초과 · 재시도 대기',budget:'자체 일일 호출량 제한 초과',duplicate:'같은 요청이 실행 중이거나 방금 완료되었습니다. 잠시 후 조회하세요.',running:'조회 중',disabled:'자동 수집 비활성화',invalid_response:'응답 형식 오류',internal:'요청 처리 오류',pending:'조회 전'};
let naverOverviewReady=false;
async function refreshNaverOverview(){
  const panel=$('naver-overview');
  if(!panel)return;
  if(!naverOverviewReady){naverOverviewReady=true;await mountNaver(null,panel);}
  else if(panel.updateNaverStatus)await panel.updateNaverStatus();
}

function naverChart(result){
  const box=node('div',undefined,'naver-chart');
  if(!result.data.length){box.append(node('p',naverLabels.no_data));return box;}
  const ns='http://www.w3.org/2000/svg';
  const svg=document.createElementNS(ns,'svg');svg.setAttribute('viewBox','0 0 680 230');
  svg.setAttribute('role','img');svg.setAttribute('aria-label',`${result.title} 날짜별 NAVER 상대 검색 지수 0~100. 아래 표에서 값을 확인하세요.`);
  const element=(tag,attrs,text)=>{const n=document.createElementNS(ns,tag);for(const [k,v]of Object.entries(attrs))n.setAttribute(k,v);if(text!==undefined)n.textContent=text;svg.append(n);return n;};
  for(const value of [0,25,50,75,100]){const y=185-value*1.55;element('line',{x1:45,y1:y,x2:655,y2:y,stroke:'#dce5dc'});element('text',{x:5,y:y+4,fill:'#66746c','font-size':12},value);}
  const first=Date.parse(result.data[0].period),last=Date.parse(result.data.at(-1).period);
  let previous=null;
  for(const point of result.data){
    const stamp=Date.parse(point.period),x=45+(stamp-first)/Math.max(86400000,last-first)*610,y=185-point.ratio*1.55;
    if(previous && stamp-previous.stamp===86400000)element('line',{x1:previous.x,y1:previous.y,x2:x,y2:y,stroke:'#23593e','stroke-width':2});
    const dot=element('circle',{cx:x,cy:y,r:3,fill:'#23593e'});const title=document.createElementNS(ns,'title');title.textContent=`${point.period}: ${point.ratio}`;dot.append(title);previous={x,y,stamp};
  }
  element('text',{x:45,y:215,fill:'#66746c','font-size':12},result.data[0].period);
  element('text',{x:655,y:215,'text-anchor':'end',fill:'#66746c','font-size':12},result.data.at(-1).period);
  box.append(node('p','X축: 날짜 · Y축: NAVER 상대 검색 지수 (0~100). 빈 날짜는 미제공이며 0으로 채우지 않습니다.','note'),svg);
  const details=node('details');details.append(node('summary','날짜별 검색 지수 표'));const table=node('table');
  const head=node('tr');head.append(node('th','날짜'),node('th','상대 검색 지수'));table.append(head);
  for(const p of result.data){const row=node('tr');row.append(node('td',p.period),node('td',String(p.ratio)));table.append(row);}
  details.append(table);box.append(details);return box;
}

async function mountNaver(candidate,container){
  const panel=candidate?node('section',undefined,'instagram-panel'):container;
  if(candidate){panel.id='naver-detail';container.append(panel);}
  panel.replaceChildren(node(candidate?'h3':'h2','NAVER 검색 트렌드'));
  const message=node('p','연결 상태 확인 중…');message.setAttribute('role','status');
  const info=node('p',undefined,'note'),tools=node('div',undefined,'tools'),output=node('div');
  const keyword=node('select');keyword.setAttribute('aria-label','NAVER 키워드');
  const days=node('select');days.setAttribute('aria-label','NAVER 조회 기간');
  for(const d of [7,30,90]){const option=node('option',`${d}일`);option.value=d;days.append(option);}days.value=30;
  const keywordLabel=node('label','음식 키워드');keywordLabel.append(keyword);
  const manual=node('input');manual.type='text';manual.value='두쫀쿠';manual.maxLength=100;manual.setAttribute('aria-label','NAVER 사용자 지정 음식');
  const manualLabel=node('label','사용자 지정 음식');manualLabel.append(manual);manualLabel.hidden=true;
  const periodLabel=node('label','조회 기간');periodLabel.append(days);
  const query=node('button','검색 추이 조회');query.disabled=true;
  tools.append(keywordLabel,manualLabel,periodLabel,query);
  panel.append(message,info,tools,output,node('p','출처: NAVER DataLab · 실제 검색 횟수가 아닌 상대 지수입니다. Google Trends RSS 트래픽과 합산하지 않으며 서로 다른 요청의 지수로 순위를 매기지 않습니다.','note'));
  let current=null,busy=false,sequence=0;
  const summary=node('p',undefined,'note');panel.append(summary);
  function clear(){sequence++;output.replaceChildren();describe();}
  function describe(){
    const target=current?.targets.find(t=>String(t.candidate_id)===keyword.value);
    manualLabel.hidden=keyword.value!=='manual';
    info.textContent=target?`대표어: ${target.topic} · 연결 검색어: ${target.keywords.join(', ')} · 마지막 정상 조회: ${fmt(target.last_success_at)} · 최근 시도: ${fmt(target.last_attempt_at)} · ${naverLabels[target.status]||target.status}`:keyword.value==='manual'?'사용자가 지정한 음식의 실시간 조회입니다. RSS 탐지 후보와 구분하며 자동 수집 대상으로 등록하지 않습니다.':'FOOD로 판별된 음식 후보가 생기면 조회할 수 있습니다.';
  }
  async function update(){
    try{
      current=await api('/api/naver');if(!panel.isConnected)return;
      const selected=keyword.value;
      const targets=candidate?current.targets.filter(t=>t.candidate_id===candidate.id):current.targets;
      const unique=new Map();for(const t of targets)if(!unique.has(t.topic))unique.set(t.topic,t);
      keyword.replaceChildren();for(const t of unique.values()){const opt=node('option',t.topic);opt.value=t.candidate_id;keyword.append(opt);}
      if(!candidate){const opt=node('option','사용자 지정 음식');opt.value='manual';keyword.append(opt);}
      if([...keyword.options].some(o=>o.value===selected))keyword.value=selected;
      if(!busy)message.textContent=naverLabels[current.status]||current.status;
      summary.textContent=`오늘 요청 ${current.daily_requests}/${current.daily_limit}회 · 누적 ${current.total_requests}회 · 자동 수집 ${current.automatic_enabled?'하루 1회':'비활성화'} · 응답 저장·캐시 없음`;
      query.disabled=busy||!current.configured||(!targets.length&&candidate!==null);describe();
    }catch(e){message.textContent=`연결 상태 조회 실패: ${e.message}`;message.className='error';}
  }
  panel.updateNaverStatus=update;
  keyword.onchange=days.onchange=clear;
  query.onclick=async()=>{
    const seq=++sequence;busy=true;query.disabled=true;keyword.disabled=true;days.disabled=true;output.replaceChildren();message.textContent='NAVER 검색 추이 조회 중…';message.className='';
    try{
      const selection=keyword.value==='manual'?{keywords:[manual.value]}:{candidate_ids:[Number(keyword.value)]};
      const response=await fetch('/api/naver/query',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({...selection,days:Number(days.value)})});
      const data=await response.json();if(!panel.isConnected||seq!==sequence)return;
      if(!response.ok){message.textContent=naverLabels[data.error_type]||data.error||'조회 실패';message.className='error';return;}
      message.textContent=naverLabels[data.status];
      output.append(node('p',data.origin==='user_keyword'?'조회 대상: 사용자 지정 음식 (RSS 자동 탐지와 별도)':'조회 대상: RSS FOOD 후보','note'));
      output.append(node('p',`기준 기간: ${data.start_date} ~ ${data.end_date} · 일간 · 기기/성별/연령 전체 · 조회 시각: ${fmt(data.collected_at)}`));
      for(const result of data.results){output.append(node('p',`대표어: ${result.title} · 검색어: ${result.keywords.join(', ')}`),naverChart(result));
        if(result.data.length)output.append(node('p',`최근 제공 지수: ${result.data.at(-1).period} · ${result.data.at(-1).ratio} / 100`));}
    }catch{message.textContent=naverLabels.network;message.className='error';}
    finally{busy=false;query.disabled=!current?.configured||!keyword.options.length;keyword.disabled=false;days.disabled=false;
      if(panel.isConnected){const text=message.textContent,cls=message.className;await update();message.textContent=text;message.className=cls;}}
  };
  await update();
}
