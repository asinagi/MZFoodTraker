/* Shared food target management. This module never invokes provider collection APIs. */
window.mountFoodTracking = async function(food, parent) {
  const element = (tag, text) => { const node = document.createElement(tag); if (text !== undefined) node.textContent = text; return node; };
  const panel = element('section'); panel.className = 'tracking-panel';
  panel.append(element('h3', '출처별 추적 계획 관리'), element('p', '계획 관리만 수행합니다. 실제 API 실행은 연결되지 않았습니다.'));
  parent.append(panel);
  const note = element('p'); note.setAttribute('role', 'status'); panel.append(note);
  const fetchJSON = async (path, body) => {
    const response = await fetch(path, body ? {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)} : {});
    const result = await response.json(); if (!response.ok) throw Error(result.error || '요청 처리 실패'); return result;
  };
  const controls = element('div'); controls.className='food-form'; panel.append(controls);
  const input = (label, value, type='text') => {
    const wrapper=element('label',label), node=element('input'); node.type=type; if (type==='checkbox') node.checked=false; else node.value=value;
    wrapper.append(node); controls.append(wrapper); return node;
  };
  const actor=input('검토자','local-reviewer'), reason=input('변경 근거',''), approved=input('추적 계획 활성화를 승인합니다.','', 'checkbox');
  const list=element('div'); panel.append(list);
  async function render() {
    list.replaceChildren();
    const [accounts, targets] = await Promise.all([fetchJSON('/api/tracking/accounts'),fetchJSON('/api/tracking/targets?food_id='+encodeURIComponent(food.food_id))]);
    for (const account of accounts.items) {
      const target=targets.items.find(t=>t.source_id===account.source_id && t.account_scope===account.account_scope);
      const card=element('div'); card.className='food-card'; list.append(card);
      card.append(element('strong',`${account.source_id} · ${account.mode==='mock'?'Mock 계획':account.account_scope}`));
      card.append(element('p',target ? `${target.effective_status} · 세대 ${target.generation} · 다음 예정 ${target.next_due_at ? new Date(target.next_due_at).toLocaleString('ko-KR',{timeZone:'Asia/Seoul'})+' KST' : '없음'}` : '추적 대상 미등록'));
      const blockers=target ? target.blockers : account.blockers;
      if (blockers.length) card.append(element('p','차단 사유: '+blockers.join(', ')));
      const label=element('label','수집 주기(초) '), interval=element('input'); interval.type='number';interval.min='1';interval.value=target ? target.interval_seconds : account.interval_seconds;label.append(interval);card.append(label);
      const add=(text, operation, extra={})=>{
        const b=element('button',text);b.type='button';card.append(b);
        b.onclick=async()=>{
          b.disabled=true;
          try {
            if (!actor.value.trim() || !reason.value.trim()) throw Error('검토자와 변경 근거를 입력하세요.');
            const data={request_key:crypto.randomUUID(),actor_ref:actor.value,reason:reason.value,...(target?{target_id:target.target_id,expected_revision:target.revision}:{}),...extra};
            if (operation==='register') {delete data.target_id;data.food_id=food.food_id;data.source_id=account.source_id;data.account_scope=account.account_scope;data.interval_seconds=Number(interval.value);}
            if (operation==='interval') data.interval_seconds=Number(interval.value);
            if (operation==='activate'||operation==='resume') {if(!approved.checked)throw Error('추적 계획 활성화 승인을 확인하세요.');data.approved=true;}
            await fetchJSON('/api/tracking/'+operation,data);note.textContent='추적 계획 상태를 저장했습니다. 실제 수집은 실행하지 않았습니다.';await render();
          } catch(error) {note.textContent=error.message;} finally {b.disabled=false;}
        };
      };
      if (!target) {if(food.tracking_eligible)add('추적 대상 등록','register');}
      else {
        if (['PENDING','BLOCKED'].includes(target.status)) add('계획 활성화 승인','activate');
        if (['PAUSED','BLOCKED'].includes(target.status)) add('재개','resume');
        if (target.status==='ACTIVE') add('일시 중단','pause');
        if (target.status!=='STOPPED') {add('추적 종료','stop');add('주기 변경','interval');add('음식·별칭 계획 갱신','refresh');}
        else add('명시적 재등록','register',{reregister:true});
        if(window.mountQueryPreview) window.mountQueryPreview(target,card,fetchJSON);
      }
    }
  }
  try {await render();} catch(error) {note.textContent=error.message;}
};
