/* Reviewed identity only: no source collection endpoints are invoked here. */
(() => {
  'use strict';
  const byId = id => document.getElementById(id);
  const el = (tag, value, cls) => {
    const n = document.createElement(tag);
    if (value !== undefined) n.textContent = value;
    if (cls) n.className = cls;
    return n;
  };
  const message = (value, error = false) => {
    const n = byId('food-message'); n.textContent = value; n.className = error ? 'error' : 'note';
  };
  async function request(path, body) {
    const r = await fetch(path, body ? {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)} : {});
    const data = await r.json();
    if (!r.ok) throw new Error(data.error || '요청 처리 실패');
    return data;
  }
  const splitAliases = value => value.split(',').map(s => s.trim()).filter(Boolean);
  const button = (label, callback) => {
    const b = el('button', label); b.type = 'button';
    b.addEventListener('click', () => act(callback, b)); return b;
  };
  async function act(callback, control) {
    if (control) control.disabled = true;
    try { await callback(); }
    catch (e) { message(e.message, true); }
    finally { if (control) control.disabled = false; }
  }
  let entities = [];
  const dialog = byId('food-dialog'), body = byId('food-dialog-body');
  byId('food-dialog-close').onclick = () => dialog.close();
  function open(title) {
    byId('food-dialog-title').textContent = title; body.replaceChildren();
    if (!dialog.open) dialog.showModal();
  }
  function field(form, label, name, value = '', options) {
    const l = el('label', label), input = el(options ? 'select' : 'input'); input.name = name;
    if (options) options.forEach(([v, title]) => { const o = el('option', title); o.value = v; input.append(o); });
    else { input.type = 'text'; input.maxLength = name === 'reason' ? 1000 : 200; }
    input.value = value; l.append(input); form.append(l); return input;
  }
  function approvalForm(label, callback) {
    const form = el('form', undefined, 'food-form');
    const actor = field(form, '검토자', 'actor_ref', 'local-reviewer'); actor.required = true;
    const reason = field(form, '승인·변경 근거', 'reason'); reason.required = true;
    const checkLabel = el('label', undefined, 'food-wide'), check = el('input'); check.type = 'checkbox'; check.required = true;
    checkLabel.append(check, document.createTextNode(' 내용과 음식 유형을 확인하고 이 작업을 승인합니다.')); form.append(checkLabel);
    const submit = el('button', label); submit.type = 'submit'; form.append(submit);
    let retryKey = null, retryPayload = null;
    form.addEventListener('submit', e => {
      e.preventDefault(); act(async () => {
        const payload = {approved: true, actor_ref: actor.value, reason: reason.value};
        const run = async (operation, data) => {
          const combined = {...payload, ...data}, encoded = JSON.stringify(combined);
          if (retryPayload !== encoded) { retryKey = crypto.randomUUID(); retryPayload = encoded; }
          return request('/api/foods/' + operation, {...combined, request_key: retryKey});
        };
        await callback(run); dialog.close(); await reload(); message('검토 결과와 변경 이력을 저장했습니다.');
      }, submit);
    });
    body.append(form); return form;
  }
  async function history(subject, target) {
    const data = await request('/api/food-events?subject_id=' + encodeURIComponent(subject));
    const details = el('details'), list = el('ul'); details.append(el('summary', '승인·변경 이력'));
    data.items.forEach(event => list.append(el('li', `${event.action} · ${event.actor_ref} · ${event.reason} · ${new Date(event.occurred_at).toLocaleString('ko-KR')}`)));
    details.append(list); target.append(details);
  }
  async function showCandidate(id) {
    const c = await request('/api/food-candidates/' + encodeURIComponent(id));
    open(c.surface_form + ' · ' + c.status);
    body.append(el('p', `${c.proposed_kind} · ${c.origin} · ${c.scope}`), el('p', c.description), el('p', '제안 별칭: ' + (c.proposed_aliases.join(', ') || '없음')));
    if (c.resolution) body.append(el('p', '연결된 FOOD_ID: ' + c.resolution.food_id));
    if (c.status === 'NEEDS_REVIEW') {
      let action, canonical, target, kind, aliases, approveAliases;
      const form = approvalForm('검토 결과 저장', async run => {
        const data = {candidate_id: c.candidate_id, expected_revision: c.revision, action: action.value};
        if (action.value !== 'reject') {
          data.entity_kind = kind.value;
          if (action.value === 'link') { data.food_id = target.value; data.approve_aliases = approveAliases.checked; }
          else { data.canonical_name = canonical.value; data.aliases = splitAliases(aliases.value); }
          if (action.value === 'create_variant') data.parent_food_id = target.value;
        }
        await run('review', data);
      });
      action = field(form, '검토 작업', 'action', 'create', [['create', '신규 음식 생성·제안 별칭 승인'], ['link', '기존 음식에 연결'], ['create_variant', '별도 파생 음식 생성'], ['reject', '비음식·부적절 후보 거절']]);
      canonical = field(form, '신규 음식 대표명', 'canonical_name', c.surface_form);
      kind = field(form, '확인한 음식 유형', 'entity_kind', c.proposed_kind === 'UNKNOWN' ? 'PRODUCT' : c.proposed_kind,
        [['PRODUCT', '음식 상품'], ['DISH', '요리'], ['INGREDIENT', '식재료'], ['FOOD_CONCEPT', '음식 범주']]);
      target = field(form, '연결할 음식 / 파생의 원본 음식', 'food_id', '', [['', '음식 선택'], ...entities.filter(f => f.entity_status === 'ACTIVE').map(f => [f.food_id, `${f.canonical_name} (${f.food_id.slice(0, 13)}…)`])]);
      aliases = field(form, '추가 승인 별칭 (쉼표 구분)', 'aliases');
      const l = el('label', '기존 음식 연결 시 제안 별칭도 승인 '); approveAliases = el('input'); approveAliases.type = 'checkbox'; l.append(approveAliases); form.append(l);
      body.append(el('p', '신규 생성은 후보명·대표명·제안 별칭을 함께 승인합니다. 브랜드·매장은 음식 승인 대상이 아닙니다.', 'note'));
      body.append(button('승인된 정확 별칭으로 다시 확인', async () => {
        await request('/api/foods/resolve', {request_key: crypto.randomUUID(), candidate_id: c.candidate_id, expected_revision: c.revision});
        await reload(); await showCandidate(c.candidate_id);
      }));
    }
    await history(c.candidate_id, body);
  }
  async function showFood(id) {
    const f = await request('/api/foods/' + encodeURIComponent(id));
    open(f.canonical_name); body.append(el('p', f.food_id, 'food-id'), el('p', `${f.entity_kind} · ${f.entity_status} · 변경 버전 ${f.revision}`));
    if (f.merged_into) body.append(el('p', '병합 대상: ' + f.merged_into));
    body.append(el('h3', '승인된 정확 별칭'));
    f.approved_aliases.forEach(alias => {
      const line = el('p', `${alias.surface_form} · ${alias.scope} `);
      line.append(button('별칭 검토·복구', () => showAlias(alias))); body.append(line);
    });
    f.revoked_aliases.forEach(alias => {
      const line = el('p', `취소된 별칭: ${alias.surface_form} · ${alias.scope} `);
      line.append(button('이력·복구', () => showAlias(alias))); body.append(line);
    });
    if (f.entity_status === 'ACTIVE') {
      body.append(button('대표명 변경', async () => {
        open('대표명 변경 · ' + f.canonical_name); let name;
        const form = approvalForm('대표명 변경 승인', run => run('rename', {food_id: id, expected_revision: f.revision, canonical_name: name.value}));
        name = field(form, '새 대표명', 'canonical_name', f.canonical_name); name.required = true;
      }));
      body.append(button('정확 별칭 추가', async () => {
        open('정확 별칭 승인'); let name, scope;
        const form = approvalForm('별칭 승인', run => run('alias', {action: 'approve', food_id: id, expected_revision: f.revision, surface_form: name.value, scope: scope.value}));
        name = field(form, '정확 별칭', 'surface_form'); name.required = true;
        scope = field(form, '별칭 범위', 'scope', f.default_scope);
      }));
      body.append(button('음식 간 관계 추가', async () => {
        open('관계 검토'); let target, type;
        const form = approvalForm('관계 승인', run => run('relate', {from_food_id: id, expected_revision: f.revision, to_food_id: target.value, relation_type: type.value}));
        target = field(form, '다른 음식', 'to_food_id', '', [['', '음식 선택'], ...entities.filter(x => x.food_id !== id && x.entity_status === 'ACTIVE').map(x => [x.food_id, x.canonical_name])]);
        type = field(form, '관계', 'relation_type', 'RELATED_TO', [['RELATED_TO', '서로 연관'], ['VARIANT_OF', '현재 음식은 선택한 음식의 파생']]);
      }));
    }
    const pre = el('pre', JSON.stringify(f, null, 2)); const details = el('details'); details.append(el('summary', '추적 시스템 전달용 음식 정보'), pre); body.append(details);
    await history(id, body);
  }
  async function showAlias(alias) {
    open('별칭 검토 · ' + alias.surface_form); let action, target;
    const form = approvalForm('별칭 변경 승인', run => run('alias', {alias_id: alias.alias_id, expected_revision: alias.revision, action: action.value,
      ...(action.value === 'reassign' ? {food_id: target.value} : {})}));
    action = field(form, '작업', 'action', alias.status === 'REVOKED' ? 'restore' : 'revoke',
      [...(alias.status === 'REVOKED' ? [['restore', '취소한 별칭 복구']] : [['revoke', '잘못된 별칭 승인 취소']]), ['reassign', '다른 음식으로 이동']]);
    target = field(form, '이동 대상', 'food_id', '', [['', '음식 선택'], ...entities.filter(f => f.entity_status === 'ACTIVE').map(f => [f.food_id, f.canonical_name])]);
    const events = await request('/api/food-events?subject_id=' + encodeURIComponent(alias.alias_id));
    const last = events.items.at(-1);
    if (last && last.after.revision === alias.revision) {
      body.append(button('마지막 별칭 변경을 되돌리는 승인 화면', async () => {
        open('별칭 변경 복구'); body.append(el('p', `${last.action} · ${last.reason}`));
        approvalForm('복구 승인', run => run('revert-alias', {event_id: last.event_id, expected_revision: alias.revision}));
      }));
    }
    await history(alias.alias_id, body);
  }
  async function reload() {
    const [foods, candidates] = await Promise.all([request('/api/foods'), request('/api/food-candidates')]);
    entities = foods.items;
    const list = byId('food-candidate-list'); list.replaceChildren();
    if (!candidates.items.length) list.append(el('p', '등록된 후보가 없습니다. 위에서 음식을 등록하세요.', 'note'));
    candidates.items.forEach(c => {
      const card = el('div', undefined, 'food-card'); card.append(el('strong', c.surface_form), el('span', `${c.status} · ${c.proposed_kind} · ${c.origin}`, 'note'), button('검토·이력', () => showCandidate(c.candidate_id))); list.append(card);
    });
    const foodList = byId('food-entity-list'); foodList.replaceChildren();
    if (!entities.length) foodList.append(el('p', '승인된 음식이 없습니다.', 'note'));
    entities.forEach(f => {
      const card = el('div', undefined, 'food-card'); card.append(el('strong', f.canonical_name), el('span', `${f.entity_status} · 별칭 ${f.approved_aliases.length}개`, 'note'), el('code', f.food_id, 'food-id'), button('상세·별칭 관리', () => showFood(f.food_id))); foodList.append(card);
    });
  }
  function submitForm(id, operation, payload) {
    const form = byId(id); let retryKey, retryValue;
    form.addEventListener('submit', e => {
      e.preventDefault(); const control = form.querySelector('button[type=submit],button:not([type])');
      act(async () => {
        const data = payload(new FormData(form)), encoded = JSON.stringify(data);
        if (encoded !== retryValue) { retryKey = crypto.randomUUID(); retryValue = encoded; }
        const result = await request('/api/foods/' + operation, {...data, request_key: retryKey});
        const current = await request('/api/food-candidates/' + encodeURIComponent(result.candidate_id));
        await reload(); message(current.status === 'RESOLVED' ? '승인된 음식에 연결했습니다: ' + current.resolved_food_id
          : current.status === 'REJECTED' ? '이미 거절된 후보입니다. 검토 이력을 확인하세요.' : '검토 후보를 등록했습니다. 아래에서 확인하고 승인하세요.');
        await showCandidate(result.candidate_id);
      }, control);
    });
  }
  submitForm('food-register', 'submit', data => {
    const result = {name: data.get('name'), entity_kind: data.get('entity_kind'), aliases: splitAliases(data.get('aliases')), scope: data.get('scope')};
    if (data.get('description')) result.description = data.get('description');
    if (data.get('relation_food_id')) result.relations = [{food_id: data.get('relation_food_id'), relation_type: data.get('relation_type')}];
    return result;
  });
  submitForm('food-import', 'import', data => ({source_code: 'google_trends_rss', source_item_id: Number(data.get('source_item_id'))}));
  byId('food-reload').onclick = () => act(reload, byId('food-reload'));
  act(reload);
})();
