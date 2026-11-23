const test = require("node:test"),
  assert = require("node:assert/strict"),
  fs = require("node:fs"),
  path = require("node:path");
const core = require("../public/data-core.js");
const base = {
  food: "테스트 음식",
  products: [
    {
      report_no: "001",
      name: "테스트 제품",
      raw_materials: "밀가루, 버터(우유)",
    },
  ],
};
test("원재료 문자열과 앞자리 0을 보존한다", () => {
  const r = core.validateReport(base);
  assert.equal(r.products[0].raw_materials, "밀가루, 버터(우유)");
  assert.equal(r.products[0].report_no, "001");
});
test("원재료 null을 없는 정보로 보존한다", () =>
  assert.equal(
    core.validateReport({
      food: "a",
      products: [{ report_no: "x", raw_materials: null }],
    }).products[0].raw_materials,
    null,
  ));
test("잘못된 원재료 배열은 조용히 버리지 않고 거부한다", () =>
  assert.throws(() =>
    core.validateReport({
      food: "a",
      products: [{ report_no: "x", raw_materials: [] }],
    }),
  ));
test("중복 품목보고번호는 거부한다", () =>
  assert.throws(() =>
    core.validateReport({
      ...base,
      products: [...base.products, ...base.products],
    }),
  ));
test("숫자 보고번호는 정밀도 손실 방지를 위해 거부한다", () =>
  assert.throws(() =>
    core.validateReport({ food: "a", products: [{ report_no: 123 }] }),
  ));
test("잘못된 파일을 거부한다", () =>
  [null, [], {}, { food: "a", products: null }].forEach((x) =>
    assert.throws(() => core.validateReport(x)),
  ));
test("0과 null을 구별한다", () => {
  assert.equal(core.pointValue({ value: 0, value_state: "OBSERVED" }), 0);
  assert.equal(
    core.pointLabel({ value: null, value_state: "NOT_OBSERVED" }),
    "미관측",
  );
  assert.equal(core.pointValue({ value: null, value_state: "OBSERVED" }), null);
});
test("문자열 숫자와 잘못된 상태는 관측값으로 그리지 않는다", () => {
  assert.equal(core.pointValue({ value: "5", value_state: "OBSERVED" }), null);
  assert.equal(core.pointValue({ value: 5, value_state: "NEW_ENUM" }), null);
});
test("DB에 저장한 mock도 mock으로 표시한다", () =>
  assert.match(
    core.modeLabel({ data_mode: "database", observation_origin: "mock" }),
    /Mock/,
  ));
test("모르는 메타데이터는 실데이터라고 주장하지 않는다", () =>
  assert.equal(core.modeLabel({}), "출처 확인 필요"));
test("API 경로에 관리·외부 URL·상위 경로를 허용하지 않는다", () => {
  [
    "https://evil.test",
    "/api/collect",
    "/api/v1/foods/../health",
    "/api/v1/admin",
  ].forEach((x) => assert.equal(core.validEndpoint(x), false));
  assert.equal(
    core.validEndpoint("/api/v1/foods/FOOD_A/timeline?period=7d"),
    true,
  );
});
test("실제 저장소 예시의 모든 미관측 값은 null로 유지된다", () => {
  let count = 0;
  for (const file of fs
    .readdirSync(path.join(__dirname, "../public/fixtures"))
    .filter((x) => x.includes("timeline"))) {
    const r = JSON.parse(
      fs.readFileSync(path.join(__dirname, "../public/fixtures", file)),
    ).response;
    for (const s of r.data.series)
      for (const p of s.points) {
        if (p.value === null) {
          count++;
          assert.equal(core.pointValue(p), null);
        }
        if (p.value === 0 && p.value_state === "OBSERVED")
          assert.equal(core.pointLabel(p), "0");
      }
  }
  assert.ok(count > 0);
});

const ingredientReport = {
  ingredient: '피스타치오', search_term: '피스타치오', source: '식품안전나라 I1250',
  counts: {products: 5, ingredient_use: 2},
  products: [
    {report_no:'TEST_101',name:'[TEST] 피스타치오 페이스트',maker:'가상 제조사',ingredient_use:true,basis:'name',usage:'제과 원료'},
    {report_no:'TEST_102',name:'[TEST] 피스타치오 분말',ingredient_use:true,basis:'usage',usage:'제빵 원료'}
  ]
};
test('재료별 보고서는 원재료 목록을 만들지 않고 후보로 연다',()=>{
  const r=core.validateReport(ingredientReport);
  assert.equal(r.report_kind,'ingredient');assert.equal(r.food,'피스타치오');
  assert.equal(r.products[0].raw_materials,null);
  assert.equal(core.reportSummary(r).warning,null);
  assert.match(core.relationLabel(r.products[0],r),/제품명.*추정/);
});
test('음식 보고서 안의 재료별 보고서도 별도 자료로 유지한다',()=>{
  const r=core.reportChoices({...base,ingredient_suppliers:[ingredientReport]});
  assert.equal(r.length,2);assert.equal(r[0].report_kind,'food');assert.equal(r[1].report_kind,'ingredient');
  assert.equal(r[0].products.length,1);
});
test('비교 자료의 유형·중복 식별자와 검색어 형식 오류를 거부한다',()=>{
  assert.throws(()=>core.reportChoices({...base,ingredient_suppliers:{}}));
  assert.throws(()=>core.validateReport({...base,aliases_searched:'a'}));
  assert.throws(()=>core.validateReport({...base,source:{}}));
  assert.throws(()=>core.validateReport({...ingredientReport,products:[ingredientReport.products[0],ingredientReport.products[0]]}));
});
test('제품 원문 포함 검색은 동의어·함량을 추론하지 않는다',()=>{
  const r=core.validateReport({...base,products:[...base.products,{report_no:'002',raw_materials:'가공유지'},{report_no:'003',raw_materials:null}]});
  const found=core.ingredientEvidence(r,'버터');assert.deepEqual(found.matched,['001']);assert.equal(found.withMaterials,2);
});
test('요약 수치 불일치를 보이고 전체 시장 건수로 사용하지 않는다',()=>{
  const r=core.validateReport({...base,counts:{products:108}});
  assert.equal(core.reportSummary(r).products,1);assert.match(core.reportSummary(r).warning,/다릅니다/);
});
test('합성과 실제 출처 미검증을 구분한다',()=>{
  assert.match(core.reportOrigin(core.validateReport(ingredientReport)),/합성/);
  assert.match(core.reportOrigin(core.validateReport({...base,source:'식품안전나라'})),/원본 대조/);
});
test('관측값·미관측 상태 스냅샷은 원본 변경 이후에도 유지된다',()=>{
  const body={meta:{data_mode:'mock',generated_at:'2026-11-17'},data:{period:{start_date:'2026-11-01',end_date:'2026-11-02'},series:[{source_code:'rss',availability:'INCLUDED',points:[{value:0,value_state:'OBSERVED'},{value:null,value_state:'NOT_OBSERVED'}]}]}};
  const snapshot=core.snapshotEvidence({food_id:'F'},body,'rss');body.data.series[0].points[0].value=99;
  assert.equal(snapshot.series.points[0].value,0);assert.equal(snapshot.series.points[1].value,null);
  assert.throws(()=>core.snapshotEvidence({food_id:'F'},body,'missing'));
});
