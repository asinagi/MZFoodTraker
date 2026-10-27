/* No browser persistence of YouTube API data. All API values are rendered as text. */
const youtubeLabels={unverified:'연결 미검증',not_configured:'API 키 미설정',success:'수집 완료',no_request:'요청 없음 · 연결 검증 결과 아님',quota:'YouTube 할당량 소진 · PT 자정까지 대기',budget:'자체 일일 예산 소진',rate_limit:'요청 속도 제한',cooldown:'요청 재개 대기',authentication:'인증 또는 API 권한 오류',permission:'요청 권한 오류',network:'네트워크 오류',upstream:'YouTube 서버 오류',invalid_response:'응답 형식 오류',internal:'처리 오류',busy:'수집 중',disabled:'자동 수집 비활성화'};
const youtubeOrigins={live_api:'실제 Google API 응답',mock:'Mock 테스트',legacy_unknown:'기존 저장 데이터 · 출처 기록 없음'};
let youtubeBusy=false;
let youtubeExpiryTimer;
async function refreshYouTubeOverview(){
  const panel=$('youtube-overview');if(!panel)return;
  try{
    const data=await api('/api/youtube');
    const connection={live_verified:'실제 API HTTP 200 확인',legacy_http_success:'기존 HTTP 200 기록 · 출처 미기록',mock_only:'Mock 테스트 결과',unverified:'실제 연결 미검증'};
    const title=node('h2','YouTube 공개 영상'),message=node('p',`${connection[data.connection_status]||'서버 재시작 후 검증 정보 표시'} · 최근 작업: ${youtubeLabels[data.status]||data.status} · 마지막 작업 ${fmt(data.last_run_at)} · 자동 수집 ${data.automatic_enabled&&data.collection_scheduler_running?'6시간 간격':'비활성화'}`);
    message.setAttribute('role','status');
    const usage=node('p',`오늘 로컬 요청 시도: 검색 ${data.search_requests}/${data.search_budget}회 (잔여 추정 ${data.search_remaining_estimate}) · 상세 ${data.video_requests}/${data.video_budget}회 · 기준일 ${data.quota_day_pt} (PT)`);
    const evidence=node('div');
    for(const endpoint of ['search','videos']){
      const calls=(data.daily_call_breakdown||[]).filter(c=>c.endpoint===endpoint);
      const total=predicate=>calls.filter(predicate).reduce((sum,c)=>sum+c.attempts,0);
      evidence.append(node('p',`${endpoint==='search'?'검색':'상세'}: 실제 API HTTP 200 ${total(c=>c.origin==='live_api'&&c.status==='success'&&c.http_status===200)}회 · 기존 출처 미기록 HTTP 200 ${total(c=>c.origin==='legacy_unknown'&&c.status==='success'&&c.http_status===200)}회 · HTTP 응답 없음 ${total(c=>c.http_status===null)}회 · Mock ${total(c=>c.origin==='mock')}회`,'note'));
    }
    if(data.latest_run){const r=data.latest_run;evidence.append(node('p',`최근 작업 출처: ${youtubeOrigins[r.origin]||r.origin} · 검색 결과 ${r.search_result_rows}행 / 고유 영상 ${r.unique_search_videos}개 / 중복 제거 ${r.duplicate_search_rows}행 · 저장 ${r.saved_videos}개 (신규 ${r.new_videos}, 기존 갱신 ${r.updated_existing_videos}) · FOOD 검색 연결 ${r.food_connections}개`));}
    const tools=node('div',undefined,'tools');
    for(const [action,label] of [['collect','YouTube 지금 수집'],['refresh','저장 영상 갱신'],['delete','로컬 영상 데이터 삭제']]){
      const button=node('button',label);button.dataset.youtubeAction=action;
      button.disabled=youtubeBusy||data.collecting||(action!=='delete'&&!data.configured);
      button.onclick=async()=>{
        youtubeBusy=true;for(const b of tools.querySelectorAll('button'))b.disabled=true;
        message.textContent='YouTube 작업 중…';
        try{
          const response=await fetch(`/api/youtube/${action}`,{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'});
          const result=await response.json();
          if(!response.ok)throw new Error(youtubeLabels[result.error_type]||result.error||'요청 실패');
          message.textContent=result.notice||`작업 완료 · 요청 ${result.requests}회 · 수신 영상 ${result.videos_received}개`;
        }catch(e){message.textContent=e.message;}
        finally{youtubeBusy=false;await refreshYouTubeOverview();}
      };tools.append(button);
    }
    const policy=node('p','음식명 자동 추출·콘텐츠 분류·유행 점수: 승인 확인 전 비활성화. FOOD 연결은 앱 검색어 기준의 미검증 관계입니다. 통계 미제공은 0이 아닌 미확인으로 표시합니다.','note');
    const retention=node('p',`저장 영상 ${data.stored_videos}개 · 최근 갱신순 최대 25개 표시. 로컬 API 데이터는 29일 이내 갱신하거나 삭제합니다. 로컬 삭제는 YouTube 원본에 영향을 주지 않습니다. 영상 제목·채널·통계는 YouTube 제공 데이터입니다.`,'note');
    const rows=node('div');
    for(const video of data.videos){
      if(Date.parse(video.expires_at)<=Date.now())continue;
      const entry=node('article',undefined,'youtube-video');
      entry.dataset.expires=String(Date.parse(video.expires_at));
      entry.append(link({url:video.video_url,title:video.title}),node('p',`YouTube · ${video.channel_title} · 게시 ${fmt(video.published_at)} · 수집 ${fmt(video.collected_at)} · 갱신 ${fmt(video.refreshed_at)}`),node('p',youtubeOrigins[video.provenance?.origin]||'기존 저장 데이터 · 출처 기록 없음','note'));
      entry.append(node('p',`조회수 ${video.view_count??'미확인'} · 좋아요 ${video.like_count??'미확인'} · 댓글 ${video.comment_count??'미확인'} · 길이 ${video.duration??'미확인'} · ${video.privacy_status}`));
      const relations=video.matches.map(m=>`${m.query}${m.food_id?` → FOOD #${m.food_id} ${m.food_name}`:' (발굴 검색)'} · 내용 미검증`);
      entry.append(node('p',`앱 검색어 매칭: ${relations.join(' / ')||'이전 매칭 만료'}`,'note'));rows.append(entry);
    }
    if(!data.videos.length)rows.append(node('p','저장된 영상이 없습니다.'));
    if(!youtubeBusy)panel.replaceChildren(title,message,usage,evidence,tools,node('p',data.quota_notice,'note'),node('p',data.notice,'note'),policy,retention,rows);
    // Also clear an unattended/offline page; DOM retention must not outlive the API cache.
    clearTimeout(youtubeExpiryTimer);
    const expiries=[...panel.querySelectorAll('[data-expires]')].map(n=>Number(n.dataset.expires));
    if(expiries.length)youtubeExpiryTimer=setTimeout(()=>{
      for(const n of panel.querySelectorAll('[data-expires]'))if(Number(n.dataset.expires)<=Date.now())n.remove();
      refreshYouTubeOverview();
    },Math.min(60000,Math.max(100,Math.min(...expiries)-Date.now())));
  }catch(e){if(!youtubeBusy)panel.replaceChildren(node('h2','YouTube 공개 영상'),node('p',`상태 조회 실패: ${e.message}`));}
}
