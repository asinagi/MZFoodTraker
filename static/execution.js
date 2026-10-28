/* Execution metadata is read-only until an explicit queue/run command. */
(() => {
  const panel=document.querySelector('#execution-overview');if(!panel)return;
  const el=(tag,text)=>{const n=document.createElement(tag);if(text!==undefined)n.textContent=text;return n;};
  const heading=el('h2','추적 실행 관리'),note=el('p'),toolbar=el('div'),summary=el('p'),list=el('div');
  note.className='note';note.textContent='미리보기는 읽기 전용입니다. 실행은 승인된 대상·정책·예산을 다시 확인하며, 운영 자동 실행은 기본 비활성입니다.';
  toolbar.className='tools';panel.append(heading,note,toolbar,summary,list);
  const message=el('p');message.setAttribute('role','status');panel.append(message);
  const refresh=el('button','실행 상태 새로고침');refresh.id='execution-refresh';refresh.type='button';toolbar.append(refresh);
  const queue=el('button','Mock 예정 작업 등록');queue.id='execution-schedule';queue.type='button';toolbar.append(queue);
  const api=async(path,body)=>{
    const response=await fetch(path,body?{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)}:{});
    const data=await response.json();if(!response.ok)throw Error(data.error||'실행 요청 실패');return data;
  };
  const when=value=>value?new Date(value).toLocaleString('ko-KR',{timeZone:'Asia/Seoul'})+' KST':'없음';
  async function render() {
    const data=await api('/api/execution/status');list.replaceChildren();
    queue.disabled=!data.available_modes.includes('mock');
    summary.textContent=`모드 ${data.available_modes.join(' / ')} · 자동 실행 ${data.automatic_enabled?'켜짐':'꺼짐'} · 마지막 성공 ${when(data.last_success_at)} · 상태 ${JSON.stringify(data.counts)}`;
    if(!data.items.length)list.append(el('p','등록된 실행 작업이 없습니다. 음식을 승인하고 출처별 대상을 활성화한 후 작업을 등록하세요.'));
    data.items.forEach(job=>{
      const card=el('article');card.className='food-card execution-job';card.dataset.jobId=job.job_id;
      card.append(el('strong',`${job.source_id} · ${job.mode} · ${job.status}`));
      card.append(el('p',`시도 ${job.attempt_count}/${job.max_attempts} · 다음 재시도 ${when(job.next_retry_at)} · 오류 ${job.error_kind||'없음'}`));
      card.append(el('p',`활성 결과 대상 ${job.eligible_target_ids.length} · 원격 중복 가능성 ${job.remote_duplicate_possible?'있음':'기록 없음'} · 수집 범위 ${job.run.result_contract?.coverage||'미확정'}`));
      if(job.execution_blockers.length)card.append(el('p','차단: '+job.execution_blockers.join(', ')));
      job.current_budget.lines.forEach(line=>card.append(el('p',`${line.resource}: 예상 상한 ${line.estimated_cost} / 사용 기록 ${line.observed_usage} / 예약 ${line.reserved_cost} / 남은 로컬 예산 ${line.remaining_budget??'별도 상한 없음'}`)));
      const canRun=['READY','RETRY_WAIT'].includes(job.status)&&!job.execution_blockers.length&&(!job.next_retry_at||new Date(job.next_retry_at)<=new Date());
      if(canRun&&data.available_modes.includes(job.mode)){
        const run=el('button',job.mode==='mock'?'Mock 한 번 실행':'승인된 Live 작업 한 번 실행');run.type='button';run.className='execution-run';
        run.onclick=async()=>{run.disabled=true;try{const result=await api('/api/execution/run',{job_id:job.job_id});message.textContent=result.result?'실행 상태: '+result.result.status:'현재 실행 가능한 작업이 없습니다.';await render();}catch(error){message.textContent=error.message;run.disabled=false;}};
        card.append(run);
      }
      const details=el('details');details.append(el('summary','작업·호출 이력 보기'),el('pre',JSON.stringify({job_id:job.job_id,run_id:job.run_id,query_id:job.query_id,calls:job.calls,result:job.run.result_contract},null,2)));card.append(details);list.append(card);
    });
  }
  refresh.onclick=()=>render().catch(error=>{message.textContent=error.message;});
  queue.onclick=async()=>{queue.disabled=true;try{const data=await api('/api/execution/schedule',{mode:'mock'});message.textContent=`작업 ${data.items.length}개 확인 · 아직 API를 실행하지 않았습니다.`;await render();}catch(error){message.textContent=error.message;queue.disabled=false;}};
  render().catch(error=>{message.textContent=error.message;});
})();
