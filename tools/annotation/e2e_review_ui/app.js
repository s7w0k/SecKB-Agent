const state = {
  meta: null,
  cases: [],
  currentId: null,
  current: null,
  decision: null,
  reveal: false,
  dirty: false,
  autosaveTimer: null,
};

const $ = (id) => document.getElementById(id);
const els = {
  reviewerId: $("reviewerId"), exportBtn: $("exportBtn"), progressPercent: $("progressPercent"),
  progressText: $("progressText"), progressBar: $("progressBar"), passCount: $("passCount"),
  modifyCount: $("modifyCount"), uncertainCount: $("uncertainCount"), searchInput: $("searchInput"),
  categoryFilter: $("categoryFilter"), decisionFilter: $("decisionFilter"), caseList: $("caseList"),
  emptyState: $("emptyState"), reviewCard: $("reviewCard"), categoryChip: $("categoryChip"),
  variantChip: $("variantChip"), positionText: $("positionText"), queryId: $("queryId"),
  passAllChecksBtn: $("passAllChecksBtn"), revealBtn: $("revealBtn"), copyIdBtn: $("copyIdBtn"), questionOk: $("questionOk"),
  questionEditor: $("questionEditor"), categoryEditor: $("categoryEditor"), behaviorEditor: $("behaviorEditor"),
  shouldAbstain: $("shouldAbstain"), requiresMultiHop: $("requiresMultiHop"), categoryOk: $("categoryOk"),
  behaviorOk: $("behaviorOk"), evidenceOk: $("evidenceOk"), passageList: $("passageList"),
  answerPointsOk: $("answerPointsOk"), answerPointsEditor: $("answerPointsEditor"), saveState: $("saveState"),
  notes: $("notes"), prevBtn: $("prevBtn"), saveNextBtn: $("saveNextBtn"), toast: $("toast"),
  exportDialog: $("exportDialog"), humanConfirm: $("humanConfirm"), confirmExportBtn: $("confirmExportBtn"),
};

async function api(path, options = {}) {
  const response = await fetch(path, {headers: {"Content-Type": "application/json"}, ...options});
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(data.detail || `请求失败 (${response.status})`);
  return data;
}

function toast(message, error = false) {
  els.toast.textContent = message;
  els.toast.classList.toggle("error", error);
  els.toast.classList.remove("hidden");
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => els.toast.classList.add("hidden"), 3200);
}

function setProgress(progress) {
  els.progressPercent.textContent = `${progress.percent}%`;
  els.progressText.textContent = `${progress.completed} / ${progress.total} 已完成 · ${progress.remaining} 待完成`;
  els.progressBar.style.width = `${progress.percent}%`;
  els.passCount.textContent = progress.pass;
  els.modifyCount.textContent = progress.modify;
  els.uncertainCount.textContent = progress.uncertain;
}

function statusGlyph(decision) {
  return ({pass: "✓", modify: "M", uncertain: "?", pending: "·"})[decision] || "·";
}

function renderCaseList() {
  els.caseList.innerHTML = "";
  const fragment = document.createDocumentFragment();
  for (const item of state.cases) {
    const button = document.createElement("button");
    button.className = `case-item${item.query_id === state.currentId ? " active" : ""}`;
    button.dataset.decision = item.decision;
    button.innerHTML = `<span class="status-mark">${statusGlyph(item.decision)}</span>
      <span class="case-summary"><strong>${escapeHtml(item.category)} · ${item.index + 1}</strong><span>${escapeHtml(item.question)}</span></span>`;
    button.addEventListener("click", () => loadCase(item.query_id));
    fragment.appendChild(button);
  }
  els.caseList.appendChild(fragment);
}

function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>'"]/g, (char) => ({"&":"&amp;","<":"&lt;",">":"&gt;","'":"&#39;",'"':"&quot;"})[char]);
}

async function refreshList(selectFirst = false) {
  const params = new URLSearchParams({
    category: els.categoryFilter.value,
    decision: els.decisionFilter.value,
    search: els.searchInput.value.trim(),
  });
  const data = await api(`/api/cases?${params}`);
  state.cases = data.items;
  setProgress(data.progress);
  renderCaseList();
  if (selectFirst && state.cases.length) await loadCase(state.cases[0].query_id);
}

function roleSet(field) {
  return new Set((state.current?.proposed_roles?.[field]) || []);
}

function roleBadge(label, className = "") {
  return `<span class="role-badge ${className}">${label}</span>`;
}

function renderPassages(passages, review) {
  els.passageList.innerHTML = "";
  const saved = {
    relevant: new Set(review?.selected_evidence_ids || []),
    forbidden: new Set(review?.forbidden_evidence_ids || []),
    forbiddenCitation: new Set(review?.forbidden_citation_ids || []),
    injection: new Set(review?.injection_evidence_ids || []),
  };
  const proposed = {
    relevant: roleSet("required_evidence_ids"),
    forbidden: roleSet("forbidden_evidence_ids"),
    forbiddenCitation: roleSet("forbidden_citation_ids"),
    injection: roleSet("injection_evidence_ids"),
    conflict: roleSet("conflicting_evidence_ids"),
  };
  for (const passage of passages) {
    const key = passage.stable_key;
    const card = document.createElement("article");
    card.className = `passage-card${state.reveal && proposed.relevant.has(key) ? " proposed-required" : ""}`;
    const badges = state.reveal ? [
      proposed.relevant.has(key) ? roleBadge("正例") : "",
      proposed.forbidden.has(key) ? roleBadge("禁止检索", "forbidden") : "",
      proposed.forbiddenCitation.has(key) ? roleBadge("禁止引用", "forbidden") : "",
      proposed.injection.has(key) ? roleBadge("注入", "injection") : "",
      proposed.conflict.has(key) ? roleBadge("冲突", "conflict") : "",
    ].join("") : "";
    const meta = passage.metadata || {};
    card.innerHTML = `<div class="passage-head"><code>${escapeHtml(key)}</code><span>${badges}</span>
      <span class="passage-meta">org ${meta.organization_id ?? "–"} · level ${meta.classification_level ?? "–"} · ${escapeHtml(meta.generation_id ?? "–")}</span></div>
      <div class="passage-content">${escapeHtml(passage.content)}</div>
      <div class="passage-roles">
        ${roleCheck(key, "relevant", "相关证据", saved.relevant.has(key))}
        ${roleCheck(key, "forbidden", "禁止检索", saved.forbidden.has(key))}
        ${roleCheck(key, "forbiddenCitation", "禁止引用", saved.forbiddenCitation.has(key))}
        ${roleCheck(key, "injection", "注入证据", saved.injection.has(key))}
      </div>`;
    els.passageList.appendChild(card);
  }
  els.passageList.querySelectorAll("input").forEach((input) => input.addEventListener("change", markDirty));
}

function roleCheck(key, role, label, checked) {
  return `<label><input type="checkbox" data-role="${role}" data-key="${escapeHtml(key)}" ${checked ? "checked" : ""}>${label}</label>`;
}

function selectDecision(decision) {
  state.decision = decision;
  document.querySelectorAll(".decision").forEach((button) => button.classList.toggle("selected", button.dataset.decision === decision));
  markDirty();
}

function passAllChecks() {
  if (!state.currentId) return;
  for (const checkbox of [els.questionOk, els.categoryOk, els.evidenceOk, els.answerPointsOk, els.behaviorOk]) {
    checkbox.checked = true;
  }
  markDirty();
  toast("5 个检查项已勾选，请确认复核结论后保存");
}

function markDirty() {
  state.dirty = true;
  els.saveState.textContent = "有未保存修改";
  els.saveState.style.color = "#a26210";
  clearTimeout(state.autosaveTimer);
  if (state.decision && els.reviewerId.value.trim()) {
    state.autosaveTimer = setTimeout(() => saveCurrent(false, true), 1500);
  }
}

function applyReview(caseData, review) {
  const c = caseData.case;
  els.categoryChip.textContent = c.category;
  els.variantChip.textContent = c.scenario_variant || "";
  els.variantChip.classList.toggle("hidden", !c.scenario_variant);
  els.positionText.textContent = `${caseData.position + 1} / ${caseData.total}`;
  els.queryId.textContent = c.query_id;
  els.questionEditor.value = review?.edited_question ?? c.question;
  els.categoryEditor.value = review?.edited_category ?? c.category;
  els.behaviorEditor.value = review?.edited_expected_behavior ?? c.expected_retrieval_behavior ?? "";
  els.shouldAbstain.checked = review?.edited_should_abstain ?? Boolean(c.should_abstain);
  els.requiresMultiHop.checked = Boolean(c.requires_multi_hop);
  els.answerPointsEditor.value = (review?.edited_answer_points?.length ? review.edited_answer_points : c.answer_points || []).join("\n");
  els.questionOk.checked = review?.question_ok ?? false;
  els.categoryOk.checked = review?.category_ok ?? false;
  els.evidenceOk.checked = review?.evidence_ok ?? false;
  els.answerPointsOk.checked = review?.answer_points_ok ?? false;
  els.behaviorOk.checked = review?.behavior_ok ?? false;
  els.notes.value = review?.notes ?? "";
  state.decision = review?.decision ?? null;
  document.querySelectorAll(".decision").forEach((button) => button.classList.toggle("selected", button.dataset.decision === state.decision));
  renderPassages(caseData.candidate_passages, review);
  state.dirty = false;
  els.saveState.textContent = review ? `已保存 · ${new Date(review.updated_at).toLocaleString()}` : "尚未保存";
  els.saveState.style.color = "";
}

async function loadCase(qid, preserveScroll = false) {
  if (state.dirty && !confirm("当前修改尚未保存，仍要切换吗？")) return;
  state.currentId = qid;
  const data = await api(`/api/cases/${encodeURIComponent(qid)}?reveal=${state.reveal}`);
  state.current = data;
  els.emptyState.classList.add("hidden");
  els.reviewCard.classList.remove("hidden");
  applyReview(data, data.review);
  renderCaseList();
  if (!preserveScroll) document.querySelector(".review-pane").scrollTop = 0;
}

function checkedKeys(role) {
  return [...els.passageList.querySelectorAll(`input[data-role="${role}"]:checked`)].map((input) => input.dataset.key);
}

function payload() {
  const c = state.current.case;
  const answerPoints = els.answerPointsEditor.value.split("\n").map((v) => v.trim()).filter(Boolean);
  return {
    reviewer_id: els.reviewerId.value.trim(),
    decision: state.decision,
    question_ok: els.questionOk.checked,
    category_ok: els.categoryOk.checked,
    evidence_ok: els.evidenceOk.checked,
    answer_points_ok: els.answerPointsOk.checked,
    behavior_ok: els.behaviorOk.checked,
    edited_question: els.questionEditor.value.trim() === c.question ? null : els.questionEditor.value.trim(),
    edited_category: els.categoryEditor.value === c.category ? null : els.categoryEditor.value,
    edited_answer_points: JSON.stringify(answerPoints) === JSON.stringify(c.answer_points || []) ? [] : answerPoints,
    edited_expected_behavior: els.behaviorEditor.value.trim() === (c.expected_retrieval_behavior || "") ? null : els.behaviorEditor.value.trim(),
    edited_should_abstain: els.shouldAbstain.checked === Boolean(c.should_abstain) ? null : els.shouldAbstain.checked,
    selected_evidence_ids: checkedKeys("relevant"),
    forbidden_evidence_ids: checkedKeys("forbidden"),
    forbidden_citation_ids: checkedKeys("forbiddenCitation"),
    injection_evidence_ids: checkedKeys("injection"),
    notes: els.notes.value.trim(),
  };
}

async function saveCurrent(goNext = false, quiet = false) {
  if (!state.currentId || !state.decision) {
    if (!quiet) toast("请先选择通过、修改后通过或不确定", true);
    return false;
  }
  if (!els.reviewerId.value.trim()) {
    if (!quiet) toast("请填写真实复核者标识", true);
    els.reviewerId.focus();
    return false;
  }
  try {
    const result = await api(`/api/reviews/${encodeURIComponent(state.currentId)}`, {method: "PUT", body: JSON.stringify(payload())});
    state.dirty = false;
    els.saveState.textContent = "已自动保存";
    els.saveState.style.color = "#12664f";
    setProgress(result.progress);
    const row = state.cases.find((item) => item.query_id === state.currentId);
    if (row) row.decision = state.decision;
    renderCaseList();
    if (!quiet) toast("复核结论已保存");
    if (goNext) await goRelative(1);
    return true;
  } catch (error) {
    if (!quiet) toast(error.message, true);
    return false;
  }
}

async function goRelative(offset) {
  const all = await api("/api/cases");
  const index = all.items.findIndex((item) => item.query_id === state.currentId);
  if (index < 0) return;
  let nextIndex = index + offset;
  if (offset > 0) {
    const pending = all.items.findIndex((item, i) => i > index && item.decision === "pending");
    if (pending >= 0) nextIndex = pending;
  }
  nextIndex = Math.max(0, Math.min(all.items.length - 1, nextIndex));
  await refreshList(false);
  await loadCase(all.items[nextIndex].query_id);
}

async function toggleReveal() {
  const selections = {
    relevant: checkedKeys("relevant"),
    forbidden: checkedKeys("forbidden"),
    forbiddenCitation: checkedKeys("forbiddenCitation"),
    injection: checkedKeys("injection"),
  };
  state.reveal = !state.reveal;
  els.revealBtn.textContent = state.reveal ? "隐藏原标注" : "显示原标注";
  if (state.currentId) {
    const data = await api(`/api/cases/${encodeURIComponent(state.currentId)}?reveal=${state.reveal}`);
    state.current = data;
    renderPassages(data.candidate_passages, data.review);
    for (const [role, keys] of Object.entries(selections)) {
      for (const key of keys) {
        const input = [...els.passageList.querySelectorAll(`input[data-role="${role}"]`)]
          .find((item) => item.dataset.key === key);
        if (input) input.checked = true;
      }
    }
  }
}

async function init() {
  state.meta = await api("/api/meta");
  setProgress(state.meta.progress);
  for (const category of state.meta.categories) {
    const option = document.createElement("option"); option.value = category; option.textContent = category;
    els.categoryFilter.appendChild(option);
    els.categoryEditor.appendChild(option.cloneNode(true));
  }
  els.reviewerId.value = localStorage.getItem("e2eReviewerId") || "";
  await refreshList(true);
}

els.searchInput.addEventListener("input", () => { clearTimeout(els.searchInput.timer); els.searchInput.timer = setTimeout(() => refreshList(false), 250); });
els.categoryFilter.addEventListener("change", () => refreshList(true));
els.decisionFilter.addEventListener("change", () => refreshList(true));
els.reviewerId.addEventListener("change", () => localStorage.setItem("e2eReviewerId", els.reviewerId.value.trim()));
els.passAllChecksBtn.addEventListener("click", passAllChecks);
els.revealBtn.addEventListener("click", toggleReveal);
els.copyIdBtn.addEventListener("click", async () => { await navigator.clipboard.writeText(state.currentId || ""); toast("query_id 已复制"); });
document.querySelectorAll(".decision").forEach((button) => button.addEventListener("click", () => selectDecision(button.dataset.decision)));
els.prevBtn.addEventListener("click", () => goRelative(-1));
els.saveNextBtn.addEventListener("click", () => saveCurrent(true));
els.exportBtn.addEventListener("click", () => { els.humanConfirm.checked = false; els.exportDialog.showModal(); });
els.confirmExportBtn.addEventListener("click", async (event) => {
  event.preventDefault();
  try {
    const result = await api("/api/export", {method: "POST", body: JSON.stringify({confirm_primary_human_review: els.humanConfirm.checked})});
    els.exportDialog.close(); toast(`已导出 ${result.cases} 条首审 Gold`);
  } catch (error) { toast(error.message, true); }
});

for (const element of [els.questionEditor, els.categoryEditor, els.behaviorEditor, els.shouldAbstain, els.questionOk, els.categoryOk, els.behaviorOk, els.evidenceOk, els.answerPointsOk, els.answerPointsEditor, els.notes]) {
  element.addEventListener("input", markDirty);
  element.addEventListener("change", markDirty);
}

document.addEventListener("keydown", (event) => {
  if (event.ctrlKey && event.key === "Enter") { event.preventDefault(); saveCurrent(true); return; }
  const tag = document.activeElement?.tagName;
  if (["INPUT", "TEXTAREA", "SELECT"].includes(tag)) return;
  if (event.key === "1") selectDecision("pass");
  if (event.key === "2") selectDecision("modify");
  if (event.key === "3") selectDecision("uncertain");
  if (event.key === "ArrowLeft") goRelative(-1);
  if (event.key === "ArrowRight") goRelative(1);
});

window.addEventListener("beforeunload", (event) => { if (state.dirty) { event.preventDefault(); event.returnValue = ""; } });
init().catch((error) => toast(error.message, true));
