(function (global) {
  "use strict";
  const safeText = (v) => (v == null ? "" : String(v));
  function validateReport(report) {
    if (
      !report ||
      typeof report !== "object" ||
      !Array.isArray(report.products) ||
      (
      !(typeof report.food === "string" && report.food.trim()) &&
      !(typeof report.ingredient === "string" && report.ingredient.trim()))
    )
      throw Error(
        "foodsafety.lookup 결과 JSON이 필요합니다. food 또는 ingredient, products를 확인하세요.",
      );
    if (report.products.length > 10000)
      throw Error("최대 10,000개 제품까지 열 수 있습니다.");
    for (const key of ['source','fetched_at','search_term']) {
      if (report[key] != null && typeof report[key] !== 'string') throw Error(key + '는 문자열이어야 합니다.');
    }
    if (report.aliases_searched != null && (!Array.isArray(report.aliases_searched) || report.aliases_searched.some(v => typeof v !== 'string')))
      throw Error('aliases_searched는 문자열 목록이어야 합니다.');
    const seen = new Set();
    const products = report.products.map((p) => {
      if (
        !p ||
        typeof p !== "object" ||
        typeof p.report_no !== "string" ||
        !p.report_no
      )
        throw Error("제품 report_no 문자열이 필요합니다.");
      if (seen.has(p.report_no))
        throw Error("중복 품목보고번호가 있습니다. 원본을 검토해 주세요.");
      seen.add(p.report_no);
      if (p.raw_materials != null && typeof p.raw_materials !== "string")
        throw Error("raw_materials는 문자열 또는 null이어야 합니다.");
      return {
        ...p,
        report_no: p.report_no,
        name: safeText(p.name),
        maker: safeText(p.maker),
        food_type: safeText(p.food_type),
        permitted_on: safeText(p.permitted_on),
        raw_materials: p.raw_materials ?? null,
      };
    });
    return { ...report, products, food: typeof report.food === "string" && report.food.trim() ? report.food : report.ingredient, report_kind: typeof report.food === "string" && report.food.trim() ? "food" : "ingredient" };
  }
  function modeLabel(meta) {
    if (meta?.data_mode === "mock" || meta?.observation_origin === "mock")
      return "Mock · 가상 관측 데이터";
    if (
      meta?.data_mode === "database" &&
      meta?.observation_origin === "live_api"
    )
      return "DB 관측 기록 · 실시간 조회 아님";
    return "출처 확인 필요";
  }
  function validEndpoint(path) {
    return /^\/api\/v1\/(health|foods(?:\/[A-Za-z0-9_-]+(?:\/(?:timeline|metrics))?)?|sources\/status)(?:\?[^#]*)?$/.test(
      path,
    );
  }
  function pointValue(p) {
    return p?.value_state === "OBSERVED" &&
      typeof p.value === "number" &&
      Number.isFinite(p.value) &&
      p.value >= 0
      ? p.value
      : null;
  }
  function pointLabel(p) {
    const n = pointValue(p);
    return n !== null
      ? String(n)
      : {
          NOT_OBSERVED: "미관측",
          EVIDENCE_EXPIRED: "근거 만료",
          INSUFFICIENT_COVERAGE: "관측 부족",
        }[p?.value_state] || "확인 필요";
  }

  function reportChoices(input) {
    const main = validateReport(input);
    const children = input.ingredient_suppliers ?? [];
    if (!Array.isArray(children) || children.length > 20)
      throw Error("재료별 보고서 목록은 최대 20개까지 가능합니다.");
    const reports = [main, ...children.map(validateReport)];
    if (reports.reduce((n, r) => n + r.products.length, 0) > 10000)
      throw Error("파일 전체에서 최대 10,000개 제품까지 열 수 있습니다.");
    return reports;
  }
  function reportOrigin(report) {
    const text = [report.source, report.data_mode, ...report.products.map(p => p.report_no)].join(" ");
    return /mock|synthetic|합성|가상|TEST_/i.test(text)
      ? "합성 예시 · 실제 수집 자료 아님"
      : "가져온 자료 · 원본 대조 필요";
  }
  function relationLabel(product, report) {
    if (report.report_kind === "ingredient") {
      if (product.ingredient_use !== true) return "재료용 여부 확인 필요";
      return product.basis === "usage" ? "용도 문구 기준 · 재료용 추정"
        : product.basis === "name" ? "제품명 기준 · 재료용 추정" : "재료용 추정 · 규칙 확인 필요";
    }
    return product.match === "core" ? "음식 자체·변형 후보 · 이름 규칙"
      : product.match === "derived" ? "파생 제품 후보 · 이름 규칙" : "메뉴 관련성 확인 필요";
  }
  function reportSummary(report) {
    const products = report.products;
    const expected = report.report_kind === "ingredient" ? report.counts?.ingredient_use : report.counts?.products;
    return {
      products: products.length,
      withMaterials: products.filter(p => typeof p.raw_materials === "string" && p.raw_materials.trim()).length,
      warning: typeof expected === "number" && expected !== products.length
        ? "파일의 요약 건수와 제품 목록 수가 다릅니다. 원본 집계를 확인해 주세요." : null
    };
  }
  function ingredientEvidence(report, term) {
    const needle = String(term || "").trim().toLocaleLowerCase();
    if (!needle) return {term: "", matched: [], withMaterials: reportSummary(report).withMaterials};
    return {
      term: String(term).trim(),
      matched: report.products.filter(p => typeof p.raw_materials === "string" && p.raw_materials.toLocaleLowerCase().includes(needle)).map(p => p.report_no),
      withMaterials: reportSummary(report).withMaterials
    };
  }
  function snapshotEvidence(food, body, sourceCode) {
    const series = body?.data?.series?.find(s => s.source_code === sourceCode);
    if (!series) throw Error("선택한 관측 출처가 응답에 없습니다.");
    return JSON.parse(JSON.stringify({
      food_id: food.food_id, source: sourceCode, period: body.data.period,
      generated_at: body.meta?.generated_at, origin: modeLabel(body.meta),
      series, snapshot_version: 1
    }));
  }

  const api = {
    reportChoices, reportOrigin, relationLabel, reportSummary, ingredientEvidence, snapshotEvidence,
    validateReport,
    modeLabel,
    validEndpoint,
    pointValue,
    pointLabel,
  };
  if (typeof module !== "undefined") module.exports = api;
  global.MZDataCore = api;
})(typeof window !== "undefined" ? window : globalThis);
