/* Read-only query preview; persisting a plan still creates no reservation or HTTP job. */
window.mountQueryPreview = function(target, card, fetchJSON) {
  const el=(tag,text)=>{const node=document.createElement(tag);if(text!==undefined)node.textContent=text;return node;};
  const details=el('details'),summary=el('summary','검색 요청·예산 미리보기'),result=el('div');
  details.className='query-preview';details.append(summary,result);card.append(details);
  let loaded=false;
  details.addEventListener('toggle',async()=>{
    if(!details.open||loaded)return;
    result.textContent='승인된 별칭과 정책·로컬 예산을 확인하고 있습니다…';
    try {
      const preview=await fetchJSON('/api/planner/preview?target_id='+encodeURIComponent(target.target_id));
      loaded=true;result.replaceChildren();
      result.append(el('p','미리보기 · 예약 0 · HTTP 요청 0. 원격 사용량은 미확인이며 실행 전 B3 재검사가 필요합니다.'));
      preview.rejections.forEach(r=>result.append(el('p','계획 제외: '+r.reasons.join(', '))));
      preview.plans.forEach(plan=>{
        result.append(el('h4',`${plan.source_id} · ${plan.query_kind} · ${plan.decision}`));
        const list=el('ul');plan.budget.lines.forEach(line=>list.append(el('li',`${line.quota_type}: 예상 ${line.estimated_cost} / 기존 ${line.observed_usage} / 예약 ${line.reserved_cost} / 로컬 잔여 ${line.remaining_budget??'별도 상한 없음'}`)));result.append(list);
        const pre=el('pre',JSON.stringify({query_id:plan.query_id,due_slot:plan.due_slot,parameters:plan.canonical_query,effective_window:plan.effective_window,targets:plan.targets},null,2));result.append(pre);
      });
      const save=el('button','이 계획 명시적으로 기록');save.type='button';result.append(save);
      let key=null;
      save.onclick=async()=>{
        save.disabled=true;
        try {
          key=key||crypto.randomUUID();
          const stored=await fetchJSON('/api/planner/commit',{request_key:key,target_ids:[target.target_id]});
          result.append(el('p',`계획 ${stored.stored_plan_ids.length}개 기록 · 예산 예약 0 · 실제 수집 없음`));
        } catch(error) {result.append(el('p',error.message));save.disabled=false;}
      };
      const history=el('button','이 대상의 이전 계획 보기');history.type='button';result.append(history);
      history.onclick=async()=>{
        try {const data=await fetchJSON('/api/planner/history?target_id='+encodeURIComponent(target.target_id));result.append(el('pre',JSON.stringify(data.items,null,2)));}
        catch(error){result.append(el('p',error.message));}
      };
    } catch(error) {result.textContent=error.message;}
  });
};
