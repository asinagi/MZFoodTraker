/* Data integration is separate from the original design-example app. */
(() => {
  "use strict";
  const core = window.MZDataCore,
    host = document.getElementById("data-workspace");
  const esc = (v) =>
    String(v ?? "").replace(
      /[&<>"']/g,
      (c) =>
        ({
          "&": "&amp;",
          "<": "&lt;",
          ">": "&gt;",
          '"': "&quot;",
          "'": "&#39;",
        })[c],
    );
  const el = (id) => document.getElementById(id);
  let pane = "sample",
    source = "fixture",
    generation = 0,
    searchSequence = 0,
    detailSequence = 0,
    foods = [],
    cursor = null,
    query = "",
    selected = null,
    chosenMenu = null,
    activeEvidence = null,
    report = null,
    reportOptions = [],
    importedFile = null,
    importSequence = 0,
    compare = new Set();
  const reviewKey = "mz-maker:data-reviews:v1";
  let reviews = {};
  try {
    const value = JSON.parse(localStorage.getItem(reviewKey) || "{}");
    if (value && typeof value === "object" && !Array.isArray(value))
      reviews = value;
  } catch {}
  const tabs = document.createElement("nav");
  tabs.className = "workspace-tabs";
  tabs.setAttribute("aria-label", "작업 공간");
  tabs.innerHTML =
    '<button data-pane="sample" aria-pressed="true">메뉴 탐색 시안</button><button data-pane="api" aria-pressed="false">음식 데이터</button><button data-pane="products" aria-pressed="false">제품 근거</button><button data-pane="reviews" aria-pressed="false">데이터 검토함</button>';
  document.querySelector(".studio-header").after(tabs);
  host.className = "data-workspace";
  host.hidden = true;
  window.MZDataApp = {
    open(next, initialQuery = "") {
      switchPane(next);
      if (next === "api") { el("data-query").value = initialQuery; connect(); }
    },
    hide() { switchPane("sample"); },
    setMenuContext(menu) { chosenMenu = menu; }
  };
  function notice(text, error = false) {
    el("data-message").textContent = text;
    el("data-message").className = error ? "data-error" : "data-note";
  }
  function switchPane(next) {
    pane = next;
    generation++;
    detailSequence++;
    searchSequence++;
    tabs
      .querySelectorAll("button")
      .forEach((b) =>
        b.setAttribute("aria-pressed", String(b.dataset.pane === pane)),
      );
    document.body.classList.toggle("data-active", pane !== "sample");
    host.hidden = pane === "sample";
    window.MZRoute?.onDataPane(pane);
    if (pane === "sample") return;
    host.innerHTML =
      '<p id="data-message" role="status" aria-live="polite"></p><div id="data-content"></div>';
    if (pane === "api") renderAPI();
    if (pane === "products") renderImport();
    if (pane === "reviews") renderReviews();
  }
  tabs.addEventListener("click", (e) => {
    const b = e.target.closest("[data-pane]");
    if (b) switchPane(b.dataset.pane);
  });
  async function fixture(file) {
    const r = await fetch("fixtures/" + file + ".json");
    if (!r.ok)
      throw Error("예시 파일을 읽지 못했습니다. HTTP 서버로 실행해 주세요.");
    return (await r.json()).response;
  }
  async function api(path) {
    if (!core.validEndpoint(path)) throw Error("허용되지 않은 조회입니다.");
    const r = await fetch(path, {
      credentials: "omit",
      cache: "no-store",
      signal: AbortSignal.timeout(10000),
    });
    let body;
    try {
      body = await r.json();
    } catch {
      throw Error(
        "로컬 연결 서버가 필요합니다. README의 serve.py 실행 방법을 확인하세요.",
      );
    }
    if (!r.ok || body.error)
      throw Error(body.error?.message || "조회 실패 (" + r.status + ")");
    if (!body.data || !body.meta) throw Error("B8 응답 형식을 확인해 주세요.");
    return body;
  }
  function metadata(body) {
    if (el("api-badge"))
      el("api-badge").textContent = core.modeLabel(body.meta);
    notice(
      core.modeLabel(body.meta) +
        " · " +
        (body.meta?.notice || "출처별 관측 범위와 기준일을 확인하세요."),
    );
  }
  function renderAPI() {
    el("data-content").innerHTML =
      '<div class="dataset-head"><div><span class="eyebrow">01 / FOOD OBSERVATIONS</span><h2>음식을 찾고, 관측을 읽어요</h2></div><span class="source-mode" id="api-badge">연결 전</span></div><div class="data-controls"><label>데이터 출처<select id="api-source"><option value="fixture">저장소 계약 예시 · Mock</option><option value="local">내 PC의 B8 API</option></select></label><button id="connect-api">연결 확인</button></div><p class="data-note">이미지·설명·제품과 음식 ID의 자동 연결은 B8에서 제공하지 않습니다. 검색 결과는 승인된 음식명·별칭 기준입니다.</p><form id="data-search" class="data-controls"><label for="data-query" class="sr-only">음식명 또는 별칭</label><input id="data-query" type="search" placeholder="음식명 또는 별칭 검색"><button type="submit">찾기</button></form><div id="food-results"></div><button id="load-more" hidden>더 보기</button><section id="food-observation" aria-live="polite"></section>';
    el("api-source").value = source;
    el("api-source").onchange = () => {
      source = el("api-source").value;
      generation++;
      searchSequence++;
      detailSequence++;
      foods = [];
      selected = null;
      el("food-results").replaceChildren();
      el("food-observation").replaceChildren();
      el("load-more").hidden = true;
      el("api-badge").textContent = "연결 전";
      el("connect-api").disabled = false;
      notice("선택한 출처의 연결 확인을 눌러 주세요.");
    };
    el("connect-api").onclick = connect;
    el("data-search").onsubmit = (e) => {
      e.preventDefault();
      query = el("data-query").value.trim();
      loadFoods(false);
    };
    el("load-more").onclick = () => loadFoods(true);
    if(document.body.classList.contains('mobile-app')) {
      const controls=el('api-source').closest('.data-controls'), info=controls.nextElementSibling;
      const disclosure=document.createElement('details');disclosure.className='app-connection';
      disclosure.innerHTML='<summary>자료 출처 설정</summary>';controls.before(disclosure);disclosure.append(controls,info);
      el('data-content').querySelector('h2').textContent='메뉴의 관심 기록';
    }
    notice("계약 예시는 가상 데이터이며 실제 유행이나 판매량이 아닙니다.");
  }
  async function connect() {
    const g = generation,
      b = el("connect-api");
    b.disabled = true;
    notice("연결 확인 중…");
    try {
      const body =
        source === "fixture"
          ? await fixture("health")
          : await api("/api/v1/health");
      if (g !== generation) return;
      if (body.data.status !== "ok" || !body.data.schema_ready)
        throw Error("저장소가 준비되지 않았습니다.");
      metadata(body);
      el("api-badge").textContent = core.modeLabel(body.meta);
      query = el("data-query").value.trim();
      await loadFoods(false);
    } catch (e) {
      if (g === generation) {
        el("api-badge").textContent = "연결 실패";
        notice(e.message, true);
      }
    } finally {
      if (g === generation) b.disabled = false;
    }
  }
  async function loadFoods(more) {
    const g = generation,
      s = ++searchSequence;
    el("food-results").setAttribute("aria-busy", "true");
    try {
      let body;
      if (source === "fixture") {
        body = await fixture("foods-list");
        const norm = (v) => v.replace(/\s/g, "").toLowerCase();
        body.data.items = body.data.items.filter((x) =>
          [x.canonical_name, ...x.aliases].some((v) =>
            norm(v).includes(norm(query)),
          ),
        );
        body.page = { next_cursor: null, total_items: body.data.items.length };
      } else {
        const qs = new URLSearchParams({ limit: "20" });
        if (query) qs.set("q", query);
        if (more && cursor) qs.set("cursor", cursor);
        body = await api("/api/v1/foods?" + qs);
      }
      if (g !== generation || s !== searchSequence) return;
      if (!Array.isArray(body.data.items))
        throw Error("음식 목록 응답 형식이 다릅니다.");
      metadata(body);
      foods = more ? foods.concat(body.data.items) : body.data.items;
      cursor = body.page?.next_cursor || null;
      el("load-more").hidden = !cursor;
      el("food-results").innerHTML = foods.length
        ? '<p class="data-note">' +
          foods.length +
          "개 표시 / 전체 " +
          esc(body.page?.total_items ?? "미확인") +
          '개</p><div class="food-cards">' +
          foods
            .map(
              (f, i) =>
                '<button class="food-data-card" data-food-index="' +
                i +
                '"><span class="eyebrow">' +
                esc(f.entity_kind) +
                "</span><strong>" +
                esc(f.canonical_name) +
                "</strong><span>" +
                esc(f.aliases.join(" · ")) +
                "</span><small>" +
                esc(f.food_id) +
                '</small><span class="card-action">관측 살펴보기 ↗</span></button>',
            )
            .join("") +
          "</div>"
        : '<p class="empty-data">검색 결과가 없습니다. 다른 음식명이나 별칭을 입력해 주세요.</p>';
      el("food-results")
        .querySelectorAll("[data-food-index]")
        .forEach(
          (b) =>
            (b.onclick = () => openFood(foods[Number(b.dataset.foodIndex)])),
        );
    } catch (e) {
      if (g === generation && s === searchSequence) notice(e.message, true);
    } finally {
      if (g === generation && s === searchSequence)
        el("food-results").removeAttribute("aria-busy");
    }
  }
  async function openFood(food) {
    selected = food;
    activeEvidence = null;
    const section = el("food-observation");
    section.innerHTML =
      '<div class="dataset-head"><h2>' +
      esc(food.canonical_name) +
      '</h2><button id="save-food">검토함에 담기</button></div><p>' +
      esc(food.food_id) +
      '</p><div class="data-controls"><button id="go-products">이 메뉴의 제품 후보 비교하기 →</button></div><div class="data-controls"><label>기간<select id="data-period"><option value="7d">7일</option><option value="30d">30일</option><option value="90d">90일 · 주 단위</option></select></label><label>출처<select id="data-source"><option value="instagram_graph">Instagram</option><option value="google_trends_rss">Google 트렌드 RSS</option><option value="youtube">YouTube · 정책상 미제공</option><option value="naver_datalab">NAVER · 저장하지 않음</option></select></label></div><p id="food-detail-note" class="data-note"></p><div id="timeline"></div>';
    el("save-food").onclick = () =>
      saveReview((source === "fixture" ? "mock:" : "api:") + food.food_id, {
        name: food.canonical_name,
        kind: "음식",
        origin: el("api-badge").textContent,
        food_id: food.food_id,
        evidence: activeEvidence,
      });
    el("go-products").onclick = () => {
      chosenMenu = {
        food_id: food.food_id,
        name: food.canonical_name,
        origin: el("api-badge").textContent,
        evidence: activeEvidence,
      };
      switchPane("products");
    };
    el("data-period").onchange = loadTimeline;
    el("data-source").onchange = loadTimeline;
    await loadTimeline();
    if(document.body.classList.contains("mobile-app")) section.scrollIntoView({behavior:"smooth",block:"start"});
  }
  async function loadTimeline() {
    const g = generation,
      s = ++detailSequence,
      food = selected,
      period = el("data-period").value,
      code = el("data-source").value;
    activeEvidence = null;
    el("save-food").disabled = true;
    el("go-products").disabled = true;
    el("timeline").innerHTML = "<p>관측을 불러오는 중…</p>";
    try {
      let body;
      if (source === "fixture") {
        if (
          food.food_id !== "FOOD_MOCK_DUJJONKU" &&
          !["FOOD_MOCK_HEUGIMJA", "FOOD_MOCK_YAKGWA"].includes(food.food_id)
        ) {
          el("timeline").innerHTML =
            '<p class="empty-data">이 음식의 추이는 묶음 예시에 없습니다. 실제 Mock 서버에 연결하면 전체 음식을 조회할 수 있습니다.</p>';
          return;
        }
        if (food.food_id !== "FOOD_MOCK_DUJJONKU" && period !== "7d") {
          el("timeline").innerHTML =
            '<p class="empty-data">이 예시는 7일 기간만 제공합니다. 다른 기간은 Mock 서버에서 확인해 주세요.</p>';
          return;
        }
        body = await fixture(
          food.food_id === "FOOD_MOCK_HEUGIMJA"
            ? "food-timeline-not-observed"
            : food.food_id === "FOOD_MOCK_YAKGWA"
              ? "food-timeline-partial"
              : period === "90d"
                ? "food-timeline-90d-week"
                : "food-timeline-" + period,
        );
      } else
        body = await api(
          "/api/v1/foods/" +
            encodeURIComponent(food.food_id) +
            "/timeline?" +
            new URLSearchParams({
              period,
              source: code,
              granularity: period === "90d" ? "week" : "day",
            }),
        );
      if (g !== generation || s !== detailSequence) return;
      metadata(body);
      el("food-detail-note").textContent =
        body.data.period.start_date +
        " ~ " +
        body.data.period.end_date +
        " · Asia/Seoul · 확인된 고유 콘텐츠 수. 출처별 수치를 합산하지 않습니다.";
      const series = body.data.series.find((x) => x.source_code === code);
      if (!series) throw Error("이 출처의 추이가 응답에 없습니다.");
      activeEvidence = core.snapshotEvidence(food, body, code);
      el("save-food").disabled = false;
      el("go-products").disabled = false;
      if (series.availability !== "INCLUDED") {
        el("timeline").innerHTML =
          '<p class="empty-data">' +
          esc(series.availability_reason || series.availability) +
          "</p>";
        return;
      }
      const points = series.points,
        max = Math.max(1, ...points.map((p) => core.pointValue(p) ?? 0));
      el("timeline").innerHTML =
        "<h3>" +
        esc(series.source_name) +
        " · " +
        esc(series.metric.item_label) +
        '</h3><div class="live-chart" aria-label="일자별 관측 막대그래프">' +
        points
          .map((p) => {
            const v = core.pointValue(p);
            return (
              '<div class="live-column"><strong>' +
              esc(core.pointLabel(p)) +
              '</strong><div class="bar-space">' +
              (v === null
                ? '<span class="missing-mark" aria-label="미관측 또는 계산 불가">—</span>'
                : '<div class="live-bar" style="height:' +
                  Math.max(v === 0 ? 2 : 4, (v / max) * 130) +
                  'px"></div>') +
              "</div><span>" +
              esc(p.start_date.slice(5)) +
              "</span></div>"
            );
          })
          .join("") +
        '</div><details class="point-details"><summary>관측 상태와 범위 자세히 보기</summary><div class="data-table-wrap"><table class="data-table"><thead><tr><th>기간</th><th>값</th><th>관측 범위</th><th>완결 여부</th></tr></thead><tbody>' +
        points
          .map(
            (p) =>
              "<tr><td>" +
              esc(p.start_date) +
              " ~ " +
              esc(p.end_date) +
              "</td><td>" +
              esc(core.pointLabel(p)) +
              "</td><td>" +
              esc(p.coverage) +
              "</td><td>" +
              (p.complete ? "완결" : "진행 중 / 부분 기간") +
              "</td></tr>",
          )
          .join("") +
        '</tbody></table></div></details><p class="data-note">0은 관측 결과 0건, —는 미관측·근거 만료·관측 부족입니다. PARTIAL/LIMITED는 전체를 관측한 수치가 아닙니다.</p>';
    } catch (e) {
      if (g === generation && s === detailSequence)
        el("timeline").innerHTML =
          '<p class="data-error">' + esc(e.message) + "</p>";
    }
  }

  function renderImport() {
    el("data-content").innerHTML = '<div class="dataset-head"><div><span class="eyebrow">02 / PRODUCT EVIDENCE</span><h2>우리 메뉴에 맞는 제품 찾기</h2></div></div>'
      + (chosenMenu ? '<p class="selected-menu-context">검토 메뉴: <strong>'+esc(chosenMenu.name)+'</strong><br>'+esc(chosenMenu.origin)+'</p>' : '')
      + '<p>음식별 제품과 재료별 제품 자료를 열고, 선택 이유를 남겨보세요.</p>'
      + '<div class="data-controls"><label class="file-label">제품 자료 열기<input id="report-file" type="file" accept=".json,application/json"></label><a class="app-evidence-link" href="#/evidence-reviews">저장한 근거 ↗</a></div>'
      + '<details class="source-guide"><summary>어떤 자료를 사용할 수 있나요?</summary><p>팀 식품안전나라 모듈의 음식별 JSON, 재료별 JSON, 재료 보고서가 포함된 JSON을 지원해요. 파일은 서버로 전송하지 않아요.</p><p>현재 제품 조회 서버는 연결 전이에요. 파일의 출처와 조회일은 제공자가 기재한 내용이므로 원본과 대조해 주세요.</p><p><a href="https://www.foodsafetykorea.go.kr/api/openApiInfo.do?svc_no=I1250" target="_blank" rel="noopener noreferrer">I1250 제품 정보</a> · <a href="https://www.foodsafetykorea.go.kr/api/openApiInfo.do?svc_no=C002" target="_blank" rel="noopener noreferrer">C002 원재료 정보</a></p><p>제품명·용도 기반 관련성은 추정이에요. 제조업체, 원재료 기재 내용과 구매·납품 가능성은 각각 확인해야 해요.</p></details><div id="report-picker"></div><div id="report-body"></div>';
    el("report-file").onchange = async e => {
      const file = e.target.files[0]; if (!file) return;
      const g = generation, seq = ++importSequence;
      try {
        if (file.size > 8*1024*1024) throw Error("8MB 이하 JSON 파일을 선택해 주세요.");
        const text = await file.text(), options = core.reportChoices(JSON.parse(text));
        const digest = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(text));
        if (g !== generation || seq !== importSequence) return;
        importedFile = {name:file.name, sha256:[...new Uint8Array(digest)].map(n=>n.toString(16).padStart(2,"0")).join(""), imported_at:new Date().toISOString()};
        reportOptions = options; report = options[0]; compare.clear();
        renderReportPicker(); renderReport();
        notice(file.name+" · "+options.length+"개 보고서를 열었어요.");
      } catch (err) { if (g === generation && seq === importSequence) notice("새 자료를 열지 못했어요. "+err.message+(report?" 이전 자료를 유지합니다.":""),true); }
    };
    if (report) {renderReportPicker();renderReport();}
    else notice("제품 자료를 열면 여기서 비교할 수 있어요. 새로고침하면 파일을 다시 열어야 하며, 저장한 검토 근거는 남아요.");
  }
  function renderReportPicker() {
    el("report-picker").innerHTML = reportOptions.length>1
      ? '<label class="report-select">살펴볼 자료<select id="report-choice">'+reportOptions.map((r,i)=>'<option value="'+i+'" '+(r===report?'selected':'')+'>'+esc((r.report_kind==="ingredient"?"재료 · ":"음식 · ")+r.food)+'</option>').join("")+'</select></label>' : '';
    if (el("report-choice")) el("report-choice").onchange = e => {report=reportOptions[Number(e.target.value)];compare.clear();renderReport();};
  }
  function renderReport() {
    const stats=core.reportSummary(report);
    el("report-body").innerHTML =
      '<div class="dataset-head"><h3>'+esc(report.food)+'</h3><span class="source-mode">'+esc(core.reportOrigin(report))+'</span></div>'
      +'<details class="source-guide"><summary>출처와 조회 범위</summary><dl><dt>파일에 적힌 출처</dt><dd>'+esc(report.source||"미기재")+'</dd><dt>자료 조회일</dt><dd>'+esc(report.fetched_at||"미기재")+'</dd><dt>검색어</dt><dd>'+esc(report.search_term || report.aliases_searched?.join(" · ") || report.food)+'</dd><dt>가져온 파일</dt><dd>'+esc(importedFile?.name||"미상")+'</dd></dl><p>이 화면에 열린 제품 목록의 범위예요. 전체 시장이나 판매량을 나타내지 않아요.</p></details>'
      +(stats.warning?'<p class="data-error">'+esc(stats.warning)+'</p>':'')
      +'<div class="report-stats"><div><strong>'+stats.products+'</strong>열린 제품</div><div><strong>'+stats.withMaterials+'</strong>원재료 기재 제품</div></div>'
      +'<div class="data-controls"><label>제품·제조사·보고번호<input type="search" id="product-query" placeholder="제품을 좁혀보세요"></label><label>원재료 원문에서 찾기<input type="search" id="ingredient-query" placeholder="예: 버터, 피스타치오"></label></div>'
      +'<p id="ingredient-result" class="data-note"></p>'
      +(chosenMenu?'<div class="menu-link-confirm"><label><input type="checkbox" id="link-menu"> '+esc(chosenMenu.name)+'의 후보로 연결</label><label>이 메뉴와 관련 있다고 판단한 이유<textarea id="link-reason" maxlength="2000" placeholder="제품명·용도·원재료 중 확인한 근거를 적어주세요"></textarea></label></div>':'')
      +'<div class="data-controls"><button id="compare-products" disabled>2개를 골라 비교해 보세요</button></div><div id="product-table"></div><div id="comparison"></div>';
    el("product-query").oninput=renderProductRows;el("ingredient-query").oninput=renderProductRows;
    el("compare-products").onclick=renderComparison;
    renderProductRows();
  }
  function renderProductRows() {
    const q=el("product-query").value.trim().toLowerCase(), ing=core.ingredientEvidence(report,el("ingredient-query").value);
    const ids=new Set(ing.matched);
    const rows=report.products.filter(p=>[p.name,p.maker,p.report_no].join(" ").toLowerCase().includes(q)&&(!ing.term||ids.has(p.report_no)));
    el("ingredient-result").textContent=ing.term
      ? '“'+ing.term+'” 원문 포함 '+ing.matched.length+'개 / 원재료 기재 '+ing.withMaterials+'개. 동의어·배합비를 추정하지 않아요.'
      : '원재료를 입력하면 원문에 해당 표현이 기재된 제품을 찾을 수 있어요.';
    el("product-table").innerHTML='<p class="data-note">검색 '+rows.length+'개 · 최대 100개 표시</p><div class="product-evidence-list">'
      +rows.slice(0,100).map(p=>'<article class="product-evidence-card"><header><h4>'+esc(p.name||"제품명 미제공")+'</h4><label class="compare-toggle"><input type="checkbox" data-compare-no="'+esc(p.report_no)+'" aria-label="'+esc(p.name||p.report_no)+' 비교" '+(compare.has(p.report_no)?'checked':'')+'>비교</label></header><p>'+esc(p.maker||"제조사 미기재")+' · '+esc(p.food_type||"유형 미기재")+'</p><p class="relation-label">'+esc(core.relationLabel(p,report))+'</p><details><summary>제품 기록 보기</summary><dl>'+[
        ["품목보고번호",p.report_no],["신고일",p.permitted_on],["원재료 원문",p.raw_materials],["용도 원문",p.usage]
      ].map(([k,v])=>'<dt>'+k+'</dt><dd>'+esc(v||"미기재")+'</dd>').join("")+'</dl></details><button data-product-save="'+esc(p.report_no)+'">검토에 담기</button></article>').join("")+'</div>';
    el("product-table").querySelectorAll("[data-compare-no]").forEach(c=>c.onchange=()=>{
      if(c.checked&&compare.size>=2){c.checked=false;notice("비교는 최대 2개까지 가능해요.",true);return;}
      c.checked?compare.add(c.dataset.compareNo):compare.delete(c.dataset.compareNo);
      el("compare-products").disabled=compare.size!==2;el("compare-products").textContent="선택 "+compare.size+"개 · 비교하기";
      if(el("comparison").hasChildNodes())renderComparison(false);
    });
    el("product-table").querySelectorAll("[data-product-save]").forEach(b=>b.onclick=()=>{
      const p=report.products.find(x=>x.report_no===b.dataset.productSave);
      const linked=chosenMenu&&el("link-menu")?.checked?chosenMenu:null;
      const reason=el("link-reason")?.value.trim()||"";
      if(linked&&!reason){notice("이 메뉴와 제품을 연결한 이유를 적어주세요.",true);el("link-reason").focus();return;}
      saveReview("product:"+importedFile.sha256+":"+p.report_no+":"+(linked?.food_id||"unlinked"),{
        name:p.name||p.report_no,kind:"제품",origin:core.reportOrigin(report),report_no:p.report_no,
        menu:linked,evidence:linked?.evidence||null,reason:linked?reason:"",
        product_evidence:{...p,source:report.source,fetched_at:report.fetched_at,report_subject:report.food,report_kind:report.report_kind,relation:core.relationLabel(p,report),file:{...importedFile}}
      });
    });
  }
  function renderComparison(scroll=true) {
    const rows=report.products.filter(p=>compare.has(p.report_no));
    const fields = [["보고번호","report_no"],["제조사","maker"],["식품 유형","food_type"],["관련성 판단",null],["원재료 원문","raw_materials"],["용도 원문","usage"],["신고일","permitted_on"]];
    el("comparison").innerHTML=rows.length===2
      ? '<h3 tabindex="-1">선택 제품 비교</h3><p class="data-note">원재료 기재 내용과 용도를 비교하고, 실제 구매 조건은 별도로 확인해 보세요.</p><table class="product-matrix"><caption class="sr-only">선택한 두 제품의 동일 항목 비교</caption><thead><tr><th scope="col">항목</th>'+rows.map(p=>'<th scope="col">'+esc(p.name||"미기재")+'</th>').join("")+'</tr></thead><tbody>'+fields.map(([label,key])=>'<tr><th scope="row">'+label+'</th>'+rows.map(p=>'<td>'+esc((key?p[key]:core.relationLabel(p,report))||"미기재")+'</td>').join("")+'</tr>').join("")+'</tbody></table>' : '<p>제품 2개를 선택해 주세요.</p>';
    if(scroll&&rows.length===2){el("comparison").scrollIntoView({behavior:"smooth",block:"start"});el("comparison").querySelector("h3").focus({preventScroll:true});}
  }
  function persist() {
    try {
      localStorage.setItem(reviewKey, JSON.stringify(reviews));
      return true;
    } catch {
      notice(
        "브라우저 저장을 사용할 수 없습니다. 이 창을 닫으면 변경 내용이 사라집니다.",
        true,
      );
      return false;
    }
  }
  function saveReview(key, item) {
    reviews[key] = {
      ...item,
      memo: reviews[key]?.memo || "",
      reason: item.reason || reviews[key]?.reason || "",
      product_form: reviews[key]?.product_form || "확인 필요",
      status: reviews[key]?.status || "검토 중",
    };
    if (persist())
      notice(item.name + " · 이 브라우저의 검토함에 저장했습니다.");
  }
  function evidenceMarkup(r) {
    const p = r.product_evidence,
      e = r.evidence;
    if (!p && !e) return "";
    let html =
      '<details class="review-evidence"><summary>검토할 때 확인한 근거</summary>';
    if (e)
      html +=
        "<p>관측 출처: " +
        esc(e.source) +
        "<br>조회 기간: " +
        esc(e.period?.start_date) +
        " ~ " +
        esc(e.period?.end_date) +
        "<br>응답 시각: " +
        esc(e.generated_at) +
        "<br>" +
        esc(e.origin) +
        '</p>' + (e.series ? '<p>저장 당시 관측값 '+esc(e.series.points?.length || 0)+'개와 상태·조회 조건을 함께 보관했어요.</p>' : '<p>이전 기록은 조회 조건만 보관되어 있어요.</p>');
    if (p)
      html +=
        "<dl>" +
        [
          ["자료 출처", p.source],
          ["검색 주제", p.report_subject],
          ["관련성 판단", p.relation],
          ["원본 파일명", p.file?.name],
          ["파일 SHA-256", p.file?.sha256],
          ["자료 조회일", p.fetched_at],
          ["제조사", p.maker],
          ["공식 식품 유형", p.food_type],
          ["신고일", p.permitted_on],
          ["원재료", p.raw_materials],
          ["용도", p.usage],
        ]
          .map(
            ([k, v]) => "<dt>" + k + "</dt><dd>" + esc(v || "미제공") + "</dd>",
          )
          .join("") +
        "</dl>";
    if (e?.series?.points) html += '<div class="snapshot-points"><h4>저장 당시 관측</h4><ul>'+e.series.points.map(p=>"<li>"+esc(p.start_date)+" · "+esc(core.pointLabel(p))+" · "+esc(p.coverage)+"</li>").join("")+"</ul></div>";
    return html + "</details>";
  }
  function renderReviews() {
    el("data-content").innerHTML =
      '<div class="dataset-head"><div><span class="eyebrow">03 / MY REVIEW</span><h2>우리 가게의 다음 메뉴, 검토를 이어가요</h2></div></div><p>메뉴와 제품 후보를 고른 이유, 다음에 확인할 일을 남기세요. 같은 브라우저에 보관되며 계정 동기화는 연결되지 않았습니다.</p><div class="review-data-cards">' +
      Object.entries(reviews)
        .map(
          ([key, r], i) =>
            '<article data-review-index="' +
            i +
            '"><span class="source-mode">' +
            esc(r.origin) +
            "</span><h3>" +
            esc(r.name) +
            "</h3><p>" +
            esc(r.kind) +
            " · " +
            esc(r.food_id || r.report_no) +
            "</p>" +
            (r.menu
              ? '<p class="selected-menu-context">검토 메뉴: ' +
                esc(r.menu.name) +
                "<br><small>사용자가 관련성을 확인해 연결한 후보</small></p>"
              : "") +
            evidenceMarkup(r) +
            '<label>상태<select class="review-status">' +
            ["검토 중", "시식 검토", "보류", "검토 마침"]
              .map(
                (v) =>
                  "<option " +
                  (v === r.status ? "selected" : "") +
                  ">" +
                  v +
                  "</option>",
              )
              .join("") +
            "</select></label>" +
            (r.kind === "제품"
              ? '<label>검토할 제품 형태 · 직접 선택<select class="review-form">' +
                ["확인 필요", "생지·반죽", "믹스", "스프레드", "키트", "기타"]
                  .map(
                    (v) =>
                      "<option " +
                      (v === r.product_form ? "selected" : "") +
                      ">" +
                      v +
                      "</option>",
                  )
                  .join("") +
                "</select></label><small>서비스 검토 분류이며 공식 식품 유형과 다릅니다.</small>"
              : "") +
            '<label>이 후보를 고른 이유<textarea class="review-reason" maxlength="2000" placeholder="우리 가게에서 검토해 볼 이유">' +
            esc(r.reason) +
            '</textarea></label><label>다음 확인사항<textarea class="review-memo" maxlength="2000" placeholder="예: 최소 주문량, 구매처, 조리·보관 조건 확인">' +
            esc(r.memo) +
            '</textarea></label><button class="review-save">변경 저장</button><button class="review-remove secondary">기록 삭제</button></article>',
        )
        .join("") +
      "</div>";
    const keys = Object.keys(reviews);
    if (!keys.length)
      el("data-content").insertAdjacentHTML(
        "beforeend",
        '<p class="empty-data">아직 담은 기록이 없습니다. 음식 데이터에서 메뉴를 고른 뒤 제품 후보를 비교해 보세요. 제품 없이 메뉴만 먼저 저장해도 괜찮아요.</p>',
      );
    el("data-content")
      .querySelectorAll("[data-review-index]")
      .forEach((a) => {
        const k = keys[Number(a.dataset.reviewIndex)];
        a.querySelector(".review-save").onclick = () => {
          reviews[k].memo = a.querySelector(".review-memo").value;
          reviews[k].reason = a.querySelector(".review-reason").value;
          reviews[k].status = a.querySelector(".review-status").value;
          if (a.querySelector(".review-form"))
            reviews[k].product_form = a.querySelector(".review-form").value;
          if (persist()) notice("선택 이유와 다음 확인사항을 저장했습니다.");
        };
        a.querySelector(".review-remove").onclick = () => {
          delete reviews[k];
          const ok = persist();
          renderReviews();
          if (ok) notice("기록을 삭제했습니다.");
        };
      });
  }
})();
