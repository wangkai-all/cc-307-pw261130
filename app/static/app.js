/* 检查排程台 · 前端 (无框架、无构建)
 * 虚构演示数据; 不涉及真实患者或医疗判断。
 */
(() => {
  "use strict";

  const state = {
    snapshot: null,
    trials: [],
    trial: null,            // 当前展示的试算结果 (完整)
    trialById: new Map(),   // 已加载的试算
    selectedExam: null,
    timelineSource: "trial",
    resourceKind: "all",
    compareIds: [],
    compareData: new Map(),
    replay: null,
    replayUpto: null,
    busy: false,
    recovery: null,
    mode: "global",
    tieBreak: "balanced",
    cascade: 0,
    planner: "计划员 甲",
    currentDate: null,
  };

  const $ = (sel, root = document) => root.querySelector(sel);
  const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));
  const esc = (value) => String(value ?? "")
    .replaceAll("&", "&amp;").replaceAll("<", "&lt;").replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;").replaceAll("'", "&#39;");
  const clamp = (n, lo, hi) => Math.min(hi, Math.max(lo, n));
  const hm = (min) => {
    if (min === null || min === undefined || Number.isNaN(Number(min))) return "--:--";
    const m = Math.max(0, Math.round(Number(min)));
    return `${String(Math.floor(m / 60)).padStart(2, "0")}:${String(m % 60).padStart(2, "0")}`;
  };
  const parseHM = (text) => {
    if (!text || !/^\d{1,2}:\d{2}$/.test(text)) return null;
    const [h, m] = text.split(":").map(Number);
    return h * 60 + m;
  };
  const DOW = ["周日", "周一", "周二", "周三", "周四", "周五", "周六"];
  const dow = (iso) => {
    const d = new Date(`${iso}T00:00:00`);
    return Number.isNaN(d.getTime()) ? "" : DOW[d.getDay()];
  };
  const priorityLabel = (p) => (p <= 1 ? "紧急" : p <= 2 ? "加急" : p <= 3 ? "优先" : "常规");
  const kindLabel = (k) => ({ equipment: "设备", staff: "人员", prep: "准备间", room: "记录间" }[k] || k);
  const statusLabel = (s) => ({
    SCHEDULED: "已排", UNSCHEDULED: "待排", STARTED: "进行中",
    COMPLETED: "已完成", CANCELLED: "已取消", NO_SHOW: "爽约",
  }[s] || s);

  /* ------------------------------------------------------------------ */
  /* API */

  async function api(path, options = {}) {
    const res = await fetch(path, {
      headers: { "Content-Type": "application/json" },
      ...options,
    });
    let data = null;
    try { data = await res.json(); } catch { data = null; }
    if (!res.ok) {
      const err = new Error((data && data.error) || `请求失败 (${res.status})`);
      err.status = res.status;
      err.payload = data;
      throw err;
    }
    return data;
  }

  const getJSON = (p) => api(p);
  const postJSON = (p, body) => api(p, { method: "POST", body: JSON.stringify(body || {}) });

  /* ------------------------------------------------------------------ */
  /* Toast / modal */

  function toast(message, type = "") {
    const host = $("#toastHost");
    const el = document.createElement("div");
    el.className = `toast ${type}`;
    el.textContent = message;
    host.appendChild(el);
    setTimeout(() => {
      el.style.opacity = "0";
      el.style.transform = "translateX(12px)";
      el.style.transition = "all .2s";
      setTimeout(() => el.remove(), 220);
    }, 4200);
  }

  function modal({ title, body, footer, onMount }) {
    const host = $("#modalHost");
    host.innerHTML = `
      <div class="modal-backdrop" role="dialog" aria-modal="true" aria-label="${esc(title)}">
        <div class="modal">
          <header><h3>${esc(title)}</h3><button type="button" class="close" aria-label="关闭">×</button></header>
          <div class="mbody">${body}</div>
          <footer>${footer || '<button type="button" class="ghost" data-close>关闭</button>'}</footer>
        </div>
      </div>`;
    const backdrop = host.firstElementChild;
    const close = () => { host.innerHTML = ""; document.removeEventListener("keydown", onKey); };
    const onKey = (event) => { if (event.key === "Escape") close(); };
    document.addEventListener("keydown", onKey);
    backdrop.addEventListener("click", (event) => { if (event.target === backdrop) close(); });
    $$("[data-close]", backdrop).forEach((btn) => btn.addEventListener("click", close));
    if (onMount) onMount(backdrop, close);
    return { close, root: backdrop };
  }

  function loading(button, on) {
    if (!button) return;
    if (on) {
      button.dataset.label = button.innerHTML;
      button.disabled = true;
      button.innerHTML = `<span class="spinner"></span>${button.textContent.trim()}`;
    } else {
      button.disabled = false;
      if (button.dataset.label) button.innerHTML = button.dataset.label;
    }
  }

  /* ------------------------------------------------------------------ */
  /* bootstrap & date */

  async function bootstrap(date) {
    try {
      const data = await getJSON(`/api/bootstrap${date ? `?date=${encodeURIComponent(date)}` : ""}`);
      state.snapshot = data.snapshot;
      state.currentDate = data.snapshot.date;
      state.trials = data.trials || [];
      state.recovery = data.recovery || null;
      state.limits = data.limits;
      if (!state.trial || state.trial.date !== state.currentDate) {
        const latest = state.trials.find(
          (t) => t.date === state.currentDate && t.status === "complete"
        );
        state.trial = latest ? await getJSON(`/api/trials/${latest.id}`) : null;
        if (state.trial) state.trialById.set(state.trial.id, state.trial);
      }
      renderAll();
      showRecoveryBanner();
      const pending = state.trials.filter((t) => t.status === "interrupted");
      if (pending.length && !sessionStorage.getItem("interruptNoted")) {
        sessionStorage.setItem("interruptNoted", "1");
        toast(`发现 ${pending.length} 个中断的试算，台账未受影响；可重新试算。`, "warn");
      }
    } catch (err) {
      toast(err.message, "error");
    }
  }

  function showRecoveryBanner() {
    const el = $("#recoveryBanner");
    const rec = state.recovery;
    if (!rec) { el.hidden = true; return; }
    const interrupted = rec.trials_marked_interrupted || [];
    const dropped = rec.corrupt_lines_dropped || 0;
    const awaiting = state.trials.filter((t) => t.status === "interrupted");
    if (!interrupted.length && !dropped && !awaiting.length) { el.hidden = true; return; }
    el.hidden = false;
    el.innerHTML = `
      <span class="grow">
        <strong>恢复检查</strong>
        台账重放到 v${rec.ledger_seq}；
        ${dropped ? `丢弃损坏事件 ${dropped} 条（已备份原文件）；` : ""}
        ${interrupted.length ? `标记中断试算 ${interrupted.length} 个；` : ""}
        ${awaiting.length ? `${awaiting.length} 个试算等待重新计算；` : ""}
        正式台账未被试算写入。
      </span>
      <button type="button" class="ghost small" id="btnRecomputeInterrupted">重新试算中断项</button>
      <button type="button" class="ghost small" id="btnDismissRecovery">知道了</button>`;
    $("#btnDismissRecovery").onclick = () => { el.hidden = true; };
    $("#btnRecomputeInterrupted").onclick = async () => {
      const target = awaiting[0];
      if (!target) return;
      await runTrial({ mode: target.mode, cascade: target.cascade || 0 });
    };
  }

  /* ------------------------------------------------------------------ */
  /* render orchestration */

  function renderAll() {
    renderDates();
    renderTopbar();
    renderKpis();
    renderAddons();
    renderWaiting();
    renderResources();
    renderTimeline();
    renderConflicts();
    renderPlanCard();
    renderTrialList();
    renderDetail();
    renderComparePick();
    renderCompareTable();
    renderLedger();
    renderReplay();
    updateStepHints();
  }

  function renderTopbar() {
    const snap = state.snapshot;
    if (!snap) return;
    $("#ledgerSeq").textContent = `台账 v${snap.seq}`;
    $("#clockPill").textContent = `时钟 ${snap.now_hm}`;
    $("#planner").value = state.planner;
    $("#cmpSeq").textContent = snap.seq;
    const stale = state.trial && state.trial.base_version !== snap.seq;
    const pill = $("#syncPill");
    pill.className = `pill live ${stale ? "stale" : ""}`;
    pill.innerHTML = `<i></i>${stale ? `方案基于 v${state.trial.base_version}` : "已同步"}`;
  }

  function renderDates() {
    const snap = state.snapshot;
    const row = $("#dateRow");
    row.innerHTML = (snap.dates || []).map((d) => `
      <button type="button" data-date="${esc(d)}" class="${d === snap.date ? "active" : ""}" aria-pressed="${d === snap.date}">
        ${esc(d.slice(5))}<span class="dow">${esc(dow(d))}</span>
      </button>`).join("");
    $$("button", row).forEach((btn) => {
      btn.onclick = async () => {
        state.currentDate = btn.dataset.date;
        state.trial = null;
        state.selectedExam = null;
        await bootstrap(btn.dataset.date);
      };
    });
  }

  function stepProgress() {
    const dateChosen = Boolean(state.currentDate);
    const addon = state.snapshot?.exams?.some((e) => e.kind === "addon");
    const conflictSeen = Boolean(state.trial);
    const confirmed = Boolean(state.snapshot?.last_confirm);
    const replayed = Boolean(state.replay);
    return { dateChosen, addon, conflictSeen, confirmed, replayed };
  }

  function updateStepHints() {
    const p = stepProgress();
    const steps = [
      ["选择日期", p.dateChosen, "按日期查看资源窗口与待排检查"],
      ["插入检查", p.addon, "从插单池加入加急/补做检查"],
      ["运行试算", p.conflictSeen, "比较整体优化与滚动局部调整"],
      ["确认方案", p.confirmed, "核对资源版本后写入正式台账"],
      ["回放", p.replayed, "沿事件流回到任一时点"],
    ];
    const host = $("#stepStrip");
    if (!host) return;
    host.innerHTML = steps.map(([label, done, hint], i) => `
      <div class="step-chip ${done ? "done" : ""}" title="${esc(hint)}">
        <span class="num">${done ? "✓" : i + 1}</span>${esc(label)}
      </div>`).join("");
  }

  function renderKpis() {
    const snap = state.snapshot;
    const trial = state.trial;
    const m = trial?.metrics || null;
    const c = snap.counts;
    const items = [
      { k: "当日检查", v: c.exams, d: `已排 ${c.scheduled} · 待排 ${c.unplaced}`, cls: c.unplaced ? "warn" : "" },
      { k: "执行进度", v: `${c.started + c.completed}/${c.exams}`, d: `进行中 ${c.started} · 完成 ${c.completed}`, cls: "" },
      trial
        ? { k: "试算已排", v: `${m.placed}`, d: m.unplaced ? `未排 ${m.unplaced} · 冲突 ${m.conflicts}` : `全部满足约束 · 冻结 ${m.frozen ?? 0}`, cls: m.unplaced ? "warn" : "good" }
        : { k: "试算", v: "—", d: "运行试算以查看指标", cls: "" },
      trial ? { k: "平均等待", v: `${m.avg_wait}m`, d: `紧急最长 ${m.urgent_max_wait}m`, cls: "" } : null,
      trial ? { k: "改期次数", v: `${m.reschedules}`, d: "相对正式台账的移动", cls: m.reschedules > 5 ? "warn" : "" } : null,
      trial ? { k: "资源公平", v: `${m.fairness}`, d: "各优先级等待均衡度", cls: "" } : null,
      trial ? { k: "利用率", v: `${m.utilization}%`, d: `完成时刻 ${m.makespan_hm}`, cls: "" } : null,
      { k: "方案依据", v: `v${snap.seq}`, d: `设备/人员版本 ${snap.resources.length} 项` , cls: "" },
    ].filter(Boolean);
    $("#kvRow").innerHTML = items.map((it) => `
      <div class="kv ${it.cls}"><div class="k">${esc(it.k)}</div><div class="v">${esc(it.v)}</div><div class="d">${esc(it.d)}</div></div>
    `).join("");
  }

  /* ------------------------------------------------------------------ */
  /* addons / waiting */

  function renderAddons() {
    const snap = state.snapshot;
    const pool = snap.addon_pool || [];
    const host = $("#addonPool");
    host.innerHTML = pool.length ? pool.map((a) => `
      <div class="addon-item">
        <div class="info">
          <b>${esc(a.label)}</b>
          <span>${esc(a.template_id)} · 优先级 ${a.priority}${a.prep_done ? " · 已准备" : " · 待准备"}</span>
        </div>
        <button type="button" class="secondary small" data-add="${esc(a.id)}">加入</button>
      </div>`).join("") : `<p class="empty-state">插单池已全部加入</p>`;
    $$("[data-add]", host).forEach((btn) => {
      btn.onclick = async () => {
        const item = pool.find((x) => x.id === btn.dataset.add);
        if (!item) return;
        await addExam({
          id: item.id,
          label: item.label,
          template_id: item.template_id,
          requested_date: state.currentDate,
          priority: item.priority,
          kind: "addon",
          prep_done: item.prep_done,
          notes: item.notes || "",
          due_by: item.priority <= 2 ? 12 * 60 : null,
        }, btn);
      };
    });

    const select = $("#templateSelect");
    select.innerHTML = (snap.templates || []).map(
      (t) => `<option value="${esc(t.id)}">${esc(t.name)} · ${t.total_minutes}m</option>`
    ).join("");
    const form = $("#addonForm");
    form.onsubmit = async (event) => {
      event.preventDefault();
      const fd = new FormData(form);
      const id = `ADD-${Date.now().toString(36).toUpperCase()}`;
      const due = parseHM(fd.get("due"));
      await addExam({
        id,
        label: fd.get("label"),
        template_id: fd.get("template_id"),
        requested_date: state.currentDate,
        priority: Number(fd.get("priority")),
        kind: "addon",
        prep_done: fd.get("prep_done") === "on",
        notes: "自定义插单（虚构）",
        due_by: due,
      }, form.querySelector("button[type=submit]"));
      form.reset();
    };
  }

  async function addExam(payload, button) {
    loading(button, true);
    try {
      await postJSON("/api/exams", payload);
      toast(`已加入待排：${payload.label}；运行试算后才会落地`, "ok");
      await refreshSnapshot();
    } catch (err) {
      toast(err.message, "error");
    } finally { loading(button, false); }
  }

  function renderWaiting() {
    const snap = state.snapshot;
    const waiting = snap.exams.filter((e) => e.status === "UNSCHEDULED");
    $("#waitingCount").textContent = waiting.length;
    $("#waitingHint").textContent = waiting.length ? "运行试算寻找合法时段" : "没有待排检查";
    const conflictFor = new Map();
    (state.trial?.unplaced || []).forEach((u) => conflictFor.set(u.exam_id, u.reason));
    const host = $("#waitingList");
    if (!waiting.length) {
      host.innerHTML = `<p class="empty-state">全部检查已有合法时段</p>`;
      return;
    }
    host.innerHTML = waiting.map((e) => {
      const reason = conflictFor.get(e.id);
      return `
        <div class="waiting-item">
          <div class="top">
            <span class="prio p${esc(e.priority)}">P${e.priority} ${esc(priorityLabel(e.priority))}</span>
            <b>${esc(e.label)}</b>
            ${e.kind === "addon" ? '<span class="badge complete">插单</span>' : ""}
          </div>
          <div class="why">${reason ? `未排原因：${esc(reason)}` : `准备${e.prep_done ? "已完成" : `需 ${e.prep_minutes} 分钟`} · 模板 ${esc(e.template_name)}`}</div>
        </div>`;
    }).join("");
  }

  /* ------------------------------------------------------------------ */
  /* resources */

  function renderResources() {
    const snap = state.snapshot;
    const host = $("#resourceList");
    const list = (snap.resources || []).filter(
      (r) => state.resourceKind === "all" || r.kind === state.resourceKind
    );
    host.innerHTML = list.map((r) => `
      <div class="res-item">
        <div class="r1">
          <b>${esc(r.name)}</b>
          <span class="ver" title="资源版本">v${r.version}</span>
          <span class="kind">${esc(kindLabel(r.kind))}</span>
        </div>
        <div class="caps">${esc((r.capabilities || []).join(" · ") || "—")}</div>
        <div class="wins">
          ${(r.windows || []).map((w) => `<span class="win">${esc(w.start_hm)}–${esc(w.end_hm)}</span>`).join("") || '<span class="blk">无班次</span>'}
          ${(r.blocks || []).map((b) => `<span class="blk" title="${esc(b.reason)}">${esc(hm(b.start))}–${esc(hm(b.end))} 封锁</span>`).join("")}
        </div>
        ${["equipment", "prep", "room"].includes(r.kind)
          ? `<button type="button" class="ghost" data-fault="${esc(r.id)}">登记故障</button>`
          : `<button type="button" class="ghost" data-shift="${esc(r.id)}">临时班次</button>`}
      </div>`).join("");
    $$("[data-fault]", host).forEach((btn) => {
      btn.onclick = () => faultModal(btn.dataset.fault);
    });
    $$("[data-shift]", host).forEach((btn) => {
      btn.onclick = () => shiftModal(btn.dataset.shift);
    });
    $$("#resourceFilter button").forEach((btn) => {
      btn.classList.toggle("active", btn.dataset.kind === state.resourceKind);
      btn.onclick = () => { state.resourceKind = btn.dataset.kind; renderResources(); };
    });
  }

  function faultModal(resourceId) {
    const res = state.snapshot.resources.find((r) => r.id === resourceId);
    const now = state.snapshot.now;
    modal({
      title: `登记设备故障 · ${res.name}`,
      body: `
        <p>登记封锁后，该资源版本 +1，所有基于旧版本的试算在确认时都会被拒绝，需要重新试算。</p>
        <div class="mini-form" style="margin-top:12px">
          <div class="two">
            <label><span>开始</span><input type="time" id="faultStart" value="${hm(now)}"></label>
            <label><span>结束</span><input type="time" id="faultEnd" value="${hm(now + 90)}"></label>
          </div>
          <label><span>原因</span><input type="text" id="faultReason" value="设备故障（演示）" maxlength="40"></label>
        </div>
        <div class="warnbox">影响：该时段内已排检查不会被自动挪走，但下一次试算会把它们标为冲突并给出具体原因。</div>`,
      footer: `<button type="button" class="ghost" data-close>取消</button>
               <button type="button" class="primary" id="faultGo">登记故障</button>`,
      onMount(root, close) {
        $("#faultGo", root).onclick = async () => {
          const start = parseHM($("#faultStart", root).value);
          const end = parseHM($("#faultEnd", root).value);
          if (start === null || end === null || end <= start) return toast("时间范围不合法", "error");
          try {
            const out = await postJSON(`/api/resources/${resourceId}/block`, {
              date: state.currentDate, start, end, reason: $("#faultReason", root).value,
            });
            toast(out.message || "故障已登记", "warn");
            close();
            await refreshSnapshot();
          } catch (err) { toast(err.message, "error"); }
        };
      },
    });
  }

  function shiftModal(resourceId) {
    const res = state.snapshot.resources.find((r) => r.id === resourceId);
    const w = (res.windows && res.windows[0]) || { start: 7 * 60, end: 15 * 60 };
    modal({
      title: `临时班次 · ${res.name}`,
      body: `
        <p>追加一段班次会为同一资源新增可用窗口，并使其版本 +1。</p>
        <div class="mini-form" style="margin-top:12px">
          <div class="two">
            <label><span>开始</span><input type="time" id="shiftStart" value="${hm(w.start)}"></label>
            <label><span>结束</span><input type="time" id="shiftEnd" value="${hm(w.end)}"></label>
          </div>
          <label><span>备注</span><input type="text" id="shiftNote" value="加班（演示）" maxlength="40"></label>
        </div>`,
      footer: `<button type="button" class="ghost" data-close>取消</button>
               <button type="button" class="primary" id="shiftGo">追加班次</button>`,
      onMount(root, close) {
        $("#shiftGo", root).onclick = async () => {
          const start = parseHM($("#shiftStart", root).value);
          const end = parseHM($("#shiftEnd", root).value);
          if (start === null || end === null || end <= start) return toast("时间范围不合法", "error");
          try {
            await postJSON(`/api/resources/${resourceId}/shift`, {
              date: state.currentDate, start, end, note: $("#shiftNote", root).value,
            });
            toast("班次已追加，资源版本 +1", "warn");
            close();
            await refreshSnapshot();
          } catch (err) { toast(err.message, "error"); }
        };
      },
    });
  }

  /* ------------------------------------------------------------------ */
  /* timeline */

  function timelineDomain() {
    const snap = state.snapshot;
    let lo = 24 * 60;
    let hi = 0;
    (snap.resources || []).forEach((r) => (r.windows || []).forEach((w) => {
      lo = Math.min(lo, w.start); hi = Math.max(hi, w.end);
    }));
    (snap.exams || []).forEach((e) => Object.values(e.slots || {}).forEach((s) => {
      lo = Math.min(lo, s.start); hi = Math.max(hi, s.end);
    }));
    if (lo > hi) { lo = 7 * 60; hi = 19 * 60; }
    lo = Math.min(lo, snap.now);
    return {
      start: Math.floor(lo / 60) * 60,
      end: Math.ceil(Math.max(hi + 10, snap.now + 60) / 60) * 60,
    };
  }

  function buildStepBlocks() {
    const snap = state.snapshot;
    const source = state.timelineSource;
    const trial = state.trial;
    const official = new Map(snap.exams.map((e) => [e.id, e]));
    const blocks = [];
    const add = (exam, stepKey, slot, variant, meta = {}) => {
      if (!slot || slot.start === undefined) return;
      blocks.push({
        examId: exam.id,
        label: exam.label,
        template: exam.template_name,
        stepKey,
        stepLabel: slot.label || stepKey,
        start: slot.start,
        end: slot.end,
        resourceId: slot.resource_id,
        resourceName: slot.resource_name,
        staffId: slot.staff_id,
        staffName: slot.staff_name,
        variant,
        status: meta.status || "SCHEDULED",
        priority: exam.priority,
        kind: exam.kind,
        delta: meta.delta || 0,
      });
    };

    if (source === "official" || !trial) {
      (snap.exams || []).forEach((exam) => {
        if (exam.status === "UNSCHEDULED" || exam.status === "CANCELLED") return;
        Object.entries(exam.slots || {}).forEach(([key, slot]) => {
          let variant = "scheduled";
          if (exam.status === "STARTED") variant = "started";
          else if (exam.status === "COMPLETED") variant = "completed";
          else if (exam.status === "NO_SHOW") variant = "noshow";
          else if (exam.kind === "addon") variant = "addon";
          add(exam, key, slot, variant, { status: exam.status });
        });
      });
      return blocks;
    }

    // trial overlay: 方案内检查来自试算, 冻结项来自方案携带的既有预约
    const planIds = Object.keys(trial.bookings || {});
    const frozen = trial.frozen || {};
    const frozenIds = Object.keys(frozen);
    if (frozenIds.length) {
      frozenIds.forEach((examId) => {
        const booking = frozen[examId] || {};
        const exam = official.get(examId) || {
          id: examId, label: examId, priority: 5, kind: "scheduled",
          template_name: booking.template_name || "",
        };
        const status = booking.status || "SCHEDULED";
        const variant = status === "STARTED" ? "started"
          : status === "COMPLETED" ? "completed"
            : status === "NO_SHOW" ? "noshow" : "frozen";
        Object.entries(booking.slots || {}).forEach(([key, slot]) => {
          add(exam, key, slot, variant, { status });
        });
      });
    } else {
      // 兼容早期试算记录: 只回填已开始/完成/爽约的正式预约
      (snap.exams || []).forEach((exam) => {
        if (planIds.includes(exam.id)) return;
        if (exam.status === "CANCELLED") return;
        if (!["STARTED", "COMPLETED", "NO_SHOW"].includes(exam.status)) return;
        Object.entries(exam.slots || {}).forEach(([key, slot]) => {
          const variant = exam.status === "STARTED" ? "started"
            : exam.status === "COMPLETED" ? "completed" : "noshow";
          add(exam, key, slot, variant, { status: exam.status });
        });
      });
    }
    Object.entries(trial.bookings || {}).forEach(([examId, booking]) => {
      const exam = official.get(examId) || {
        id: examId, label: examId, priority: 5, kind: "scheduled",
        template_name: booking.template_name || "",
      };
      const prev = official.get(examId);
      const prevStart = prev ? prev.start_hm : null;
      Object.entries(booking.slots || {}).forEach(([key, slot]) => {
        let variant = exam.kind === "addon" ? "addon" : "trial";
        const delta = prevStart && prevStart !== hm(slot.start)
          ? slot.start - (parseHM(prevStart) ?? slot.start) : 0;
        add(exam, key, slot, variant, { status: "SCHEDULED", delta });
      });
    });
    return blocks;
  }

  function renderTimeline() {
    const snap = state.snapshot;
    if (!snap) return;
    const host = $("#timeline");
    const domain = timelineDomain();
    const pxPerMin = Math.max(0.95, 900 / (domain.end - domain.start));
    const width = Math.ceil((domain.end - domain.start) * pxPerMin);
    const blocks = buildStepBlocks();
    const rowResources = (snap.resources || [])
      .slice()
      .sort((a, b) => {
        const order = { equipment: 0, prep: 1, room: 2, staff: 3 };
        return order[a.kind] - order[b.kind] || a.id.localeCompare(b.id);
      });
    const showNow = snap.now >= domain.start && snap.now <= domain.end;
    const ticks = [];
    for (let t = domain.start; t <= domain.end; t += 60) ticks.push(t);

    const head = `
      <div class="tl-head">
        <div class="label-cell">资源 / 人员</div>
        <div class="tl-scale" style="width:${width}px">
          ${ticks.map((t) => `<div class="tick" style="left:${(t - domain.start) * pxPerMin}px"><span>${hm(t)}</span></div>`).join("")}
        </div>
      </div>`;

    const rows = rowResources.map((res) => {
      const rowBlocks = blocks.filter((b) => b.resourceId === res.id);
      const staffBlocks = blocks.filter((b) => b.staffId === res.id);
      const isStaff = res.kind === "staff";
      const items = isStaff ? staffBlocks : rowBlocks;
      return `
        <div class="tl-row">
          <div class="label-cell">
            <b>${esc(res.name)}</b>
            <span>${esc(kindLabel(res.kind))} v${res.version}</span>
          </div>
          <div class="tl-track" style="width:${width}px">
            ${ticks.map((t) => `<div class="grid-line" style="left:${(t - domain.start) * pxPerMin}px"></div>`).join("")}
            ${(res.windows || []).map((w) => `<div class="win-bg" style="left:${(w.start - domain.start) * pxPerMin}px;width:${(w.end - w.start) * pxPerMin}px"></div>`).join("")}
            ${(res.blocks || []).map((b) => `<div class="block-bg" title="${esc(b.reason)}" style="left:${(b.start - domain.start) * pxPerMin}px;width:${(b.end - b.start) * pxPerMin}px"></div>`).join("")}
            ${showNow ? `<div class="nowline" style="left:${(snap.now - domain.start) * pxPerMin}px" title="当前时钟 ${esc(snap.now_hm)}"></div>` : ""}
            ${items.map((b) => {
              const left = (b.start - domain.start) * pxPerMin;
              const w = Math.max(14, (b.end - b.start) * pxPerMin - 1);
              const selected = state.selectedExam === b.examId;
              const short = b.label.length > 12 ? `${b.label.slice(0, 12)}…` : b.label;
              return `<div class="tl-block ${b.variant} ${selected ? "selected" : ""}"
                data-exam="${esc(b.examId)}" role="button" tabindex="0"
                style="left:${left}px;width:${w}px"
                title="${esc(`${b.label} · ${b.stepLabel} · ${hm(b.start)}–${hm(b.end)} · ${b.resourceName} / ${b.staffName}`)}">
                <span>${esc(isStaff ? b.label : `${short}`)}</span>
                ${b.delta ? `<span class="delta">${b.delta > 0 ? "+" : ""}${b.delta}m</span>` : ""}
                ${w > 110 ? `<span class="tag">${esc(isStaff ? `${hm(b.start)}–${hm(b.end)}` : b.resourceName)}</span>` : ""}
              </div>`;
            }).join("")}
          </div>
        </div>`;
    }).join("");

    host.innerHTML = head + rows;
    $("#timeRange").textContent = `${hm(domain.start)} – ${hm(domain.end)} · 每分钟 ${pxPerMin.toFixed(2)}px`;
    $("#timelineNote").textContent = state.timelineSource === "trial" && state.trial
      ? `试算 ${state.trial.id} · ${state.trial.mode === "global" ? "整体优化" : "滚动局部调整"} · 紫色为调整项，橙色为插单，绿色为已开始/完成，红色斜纹为封锁`
      : "正式台账时段 · 已开始的检查从此不可移动";

    $$(".tl-block", host).forEach((el) => {
      const activate = () => {
        state.selectedExam = el.dataset.exam;
        renderTimeline();
        renderDetail();
        $("#detailCard")?.scrollIntoView({ behavior: "smooth", block: "nearest" });
      };
      el.onclick = activate;
      el.onkeydown = (event) => {
        if (event.key === "Enter" || event.key === " ") { event.preventDefault(); activate(); }
      };
    });

    $$("#timelineSource button").forEach((btn) => {
      btn.classList.toggle("active", btn.dataset.src === state.timelineSource);
      btn.setAttribute("aria-pressed", btn.dataset.src === state.timelineSource);
      btn.onclick = () => { state.timelineSource = btn.dataset.src; renderTimeline(); };
    });
  }

  /* ------------------------------------------------------------------ */
  /* conflicts */

  function renderConflicts() {
    const trial = state.trial;
    const host = $("#conflictList");
    const snap = state.snapshot;
    if (!trial) {
      const unplaced = (snap.exams || []).filter((e) => e.status === "UNSCHEDULED");
      host.innerHTML = unplaced.length
        ? unplaced.map((e) => `
          <div class="conflict">
            <div class="ct"><b>${esc(e.label)}</b><span class="kind">待排</span></div>
            <div class="cm">尚未运行试算。资源窗口：${(snap.resources || []).length} 项；准备要求 ${e.prep_minutes} 分钟。</div>
          </div>`).join("")
        : `<div class="conflict ok"><div class="ct"><b>正式台账无待排检查</b></div><div class="cm">运行试算可查看整体优化与滚动调整的取舍。</div></div>`;
      $("#conflictHint").textContent = "运行试算后，无法满足的检查会列出具体原因";
      return;
    }
    const conflicts = trial.conflicts || [];
    $("#conflictHint").textContent = conflicts.length
      ? `${conflicts.length} 项无法满足 · 未塞入不合法时段`
      : "所有检查均满足资源、班次与准备约束";
    if (!conflicts.length) {
      const unplaced = trial.metrics.unplaced || 0;
      if (!unplaced) {
        host.innerHTML = `<div class="conflict ok">
          <div class="ct"><b>没有冲突</b><span class="kind">可行</span></div>
          <div class="cm">${trial.metrics.placed} 项检查均已安排，未排 0 项。</div>
        </div>`;
        return;
      }
      host.innerHTML = `<div class="conflict">
          <div class="ct"><b>${unplaced} 项未排</b><span class="kind">待人工处理</span></div>
          <div class="cm">这些检查没有进入方案，也不会占用任何资源；请换日期、补设备班次或放宽要求后重新试算。</div>
        </div>` + (trial.unplaced || []).map((u) => `
          <div class="conflict" data-exam="${esc(u.exam_id)}" role="button" tabindex="0">
            <div class="ct"><b>${esc(u.label)}</b><span class="kind">${esc(conflictKindLabel(u.conflict_kind))}</span></div>
            <div class="cm">${esc(u.reason)}</div>
          </div>`).join("");
      wireConflictRows(host);
      return;
    }
    host.innerHTML = conflicts.map((c) => `
      <div class="conflict" ${c.exam_id ? `data-exam="${esc(c.exam_id)}" role="button" tabindex="0"` : ""}>
        <div class="ct">
          <b>${esc(examLabel(c.exam_id) || "未排检查")}</b>
          <span class="kind">${esc(conflictKindLabel(c.kind))}</span>
        </div>
        <div class="cm">${esc(c.message)}</div>
        ${(c.detail?.reasons || []).length ? `<ul class="reasons">${c.detail.reasons.map((r) => `<li>${esc(r)}</li>`).join("")}</ul>` : ""}
      </div>`).join("");
    wireConflictRows(host);
  }

  function wireConflictRows(host) {
    $$("[data-exam]", host).forEach((el) => {
      const open = () => {
        state.selectedExam = el.dataset.exam;
        renderDetail();
        const card = document.getElementById("detailCard");
        if (card) card.scrollIntoView({ behavior: "smooth", block: "nearest" });
      };
      el.onclick = open;
      el.onkeydown = (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); open(); } };
    });
  }

  const conflictKindLabel = (k) => ({
    no_feasible_slot: "无合法时段", resource_unavailable: "资源不可用",
    prep_required: "准备未完成", capability_missing: "能力缺失",
    resource_version: "资源版本", resource_changed: "资源变更",
    exam_changed: "检查已变", overlap: "资源重叠", outside_window: "超出窗口",
    in_past: "已过时钟", locked_exam_moved: "已开始", due_missed: "超截止",
    stale_version: "版本过期", prep_missing: "准备缺失", prep_order: "准备顺序",
  }[k] || k);

  function examLabel(id) {
    if (!id) return "";
    const e = (state.snapshot.exams || []).find((x) => x.id === id);
    if (e) return e.label;
    const u = (state.trial?.unplaced || []).find((x) => x.exam_id === id);
    return u ? u.label : id;
  }

  /* ------------------------------------------------------------------ */
  /* plan card & trials */

  function tradeoffText(trial) {
    if (!trial) return "";
    const m = trial.metrics;
    if (trial.mode === "global") {
      return `整体优化重排当日，<strong>平均等待 ${m.avg_wait} 分钟</strong>、紧急最长 ${m.urgent_max_wait} 分钟，
        代价是 <strong>${m.reschedules} 次改期</strong>。适合开始前排定整日，或在设备故障后彻底重算。`;
    }
    return `滚动局部调整冻结既有预约，<strong>只改期 ${m.reschedules} 次</strong>，
      平均等待 ${m.avg_wait} 分钟、紧急最长 ${m.urgent_max_wait} 分钟。
      适合已开始执行、只插入少数新检查的时段。`;
  }

  function renderPlanCard() {
    const host = $("#planCard");
    const trial = state.trial;
    if (!trial) {
      host.innerHTML = `
        <div class="card-head"><h2>当前方案</h2></div>
        <div class="card-body"><p class="empty-state">运行试算后查看指标与取舍。<br>试算不会写入正式台账。</p></div>`;
      return;
    }
    const m = trial.metrics;
    const stale = trial.base_version !== state.snapshot.seq;
    const confirmed = trial.status === "confirmed";
    host.innerHTML = `
      <div class="plan-hero">
        <div class="mode">
          <h3>${trial.mode === "global" ? "整体优化" : "滚动局部调整"}</h3>
          <span class="badge ${trial.mode}">${trial.mode === "global" ? "GLOBAL" : "ROLLING"}</span>
          <span class="badge ${confirmed ? "confirmed" : "complete"}">${confirmed ? "已确认" : "试算中"}</span>
        </div>
        <div class="sub">${esc(trial.id)} · ${esc(trial.date)} · ${esc(trial.planner || "—")}</div>
        <div class="sub">依据台账 v${trial.base_version} · 当前 v${state.snapshot.seq} · ${trial.elapsed_ms}ms · 尝试 ${trial.attempts_done}/${trial.attempts_total}</div>
      </div>
      <div class="metric-grid">
        <div class="metric"><div class="k">已排 / 未排</div><div class="v">${m.placed} / ${m.unplaced}</div><div class="d">写入 ${m.moved ?? Object.keys(trial.bookings || {}).length} · 冻结 ${m.frozen ?? 0}</div></div>
        <div class="metric"><div class="k">平均等待</div><div class="v">${m.avg_wait}m</div><div class="d">最长 ${m.max_wait}m</div></div>
        <div class="metric"><div class="k">紧急等待</div><div class="v">${m.urgent_max_wait}m</div><div class="d">P1–P2 最长</div></div>
        <div class="metric"><div class="k">改期</div><div class="v">${m.reschedules}</div><div class="d">${trial.mode === "rolling" ? "滚动模式仅动邻域内预约" : "相对正式台账"}</div></div>
        <div class="metric"><div class="k">公平度</div><div class="v">${m.fairness}</div><div class="d">越高越均衡</div></div>
        <div class="metric"><div class="k">利用率</div><div class="v">${m.utilization}%</div><div class="d">完成 ${m.makespan_hm}</div></div>
      </div>
      <p class="tradeoff-note">${tradeoffText(trial)}</p>
      ${stale ? `<div class="banner" style="margin:0 15px 12px">台账已从 v${trial.base_version} 前进到 v${state.snapshot.seq}，确认将按资源版本逐项核对。</div>` : ""}
      <div class="plan-actions">
        <button type="button" class="primary" id="btnConfirm" ${confirmed ? "disabled" : ""}>${confirmed ? "已确认" : "确认方案"}</button>
        <button type="button" class="secondary" id="btnAddCompare">加入对比</button>
      </div>`;
    $("#btnConfirm").onclick = confirmModal;
    $("#btnAddCompare").onclick = () => {
      state.compareIds = Array.from(new Set([...state.compareIds, trial.id])).slice(-3);
      switchView("compare");
      renderComparePick();
      renderCompareTable();
    };
  }

  function renderTrialList() {
    const host = $("#trialList");
    const trials = state.trials || [];
    if (!trials.length) {
      host.innerHTML = `<p class="empty-state">暂无试算。运行一次试算开始。</p>`;
      return;
    }
    host.innerHTML = trials.map((t) => {
      const selected = state.trial && state.trial.id === t.id;
      const m = t.metrics || {};
      return `
        <div class="trial-item ${selected ? "selected" : ""}" data-trial="${esc(t.id)}" role="button" tabindex="0">
          <div class="t0">
            <span class="badge ${t.mode}">${t.mode === "global" ? "整体" : "滚动"}</span>
            <b>${esc(t.planner || "—")}</b>
            <span class="badge ${esc(t.status)}">${trialStatusLabel(t.status)}</span>
            <span class="id">${esc(t.id.slice(-6))}</span>
          </div>
          <div class="t1">
            <span>排 <b>${m.placed ?? "—"}</b></span>
            <span>未排 <b>${m.unplaced ?? "—"}</b></span>
            <span>等待 <b>${m.avg_wait ?? "—"}</b>m</span>
            <span>改期 <b>${m.reschedules ?? "—"}</b></span>
            <span>v${t.base_version ?? "—"}</span>
          </div>
        </div>`;
    }).join("");
    $$("[data-trial]", host).forEach((el) => {
      const load = async () => {
        await selectTrial(el.dataset.trial);
        switchView("schedule");
      };
      el.onclick = load;
      el.onkeydown = (e) => { if (e.key === "Enter") load(); };
    });
  }

  const trialStatusLabel = (s) => ({
    complete: "完成", confirmed: "已确认", interrupted: "中断", running: "计算中",
  }[s] || s || "—");

  async function selectTrial(id) {
    try {
      const trial = state.trialById.get(id) || await getJSON(`/api/trials/${id}`);
      if (!trial) return;
      state.trialById.set(trial.id, trial);
      state.trial = trial;
      state.timelineSource = "trial";
      renderTopbar(); renderKpis(); renderWaiting(); renderTimeline();
      renderConflicts(); renderPlanCard(); renderTrialList(); renderDetail();
      toast(`已载入试算 ${id}（${trial.status === "interrupted" ? "中断，需重新试算" : trial.mode === "global" ? "整体优化" : "滚动调整"}）`);
    } catch (err) { toast(err.message, "error"); }
  }

  /* ------------------------------------------------------------------ */
  /* run trial */

  async function runTrial(overrides = {}) {
    if (state.busy) return;
    const mode = overrides.mode || state.mode;
    const btn = $("#btnRun");
    state.busy = true;
    loading(btn, true);
    try {
      const payload = {
        date: state.currentDate,
        mode,
        tie_break: overrides.tie_break || state.tieBreak,
        planner: state.planner,
        cascade: mode === "rolling" ? (overrides.cascade ?? state.cascade) : 0,
      };
      const trial = await postJSON("/api/trials", payload);
      state.trialById.set(trial.id, trial);
      state.trial = trial;
      state.timelineSource = "trial";
      state.selectedExam = null;
      await refreshSnapshot({ keepTrial: true });
      toast(`试算完成：已排 ${trial.metrics.placed}，冲突 ${trial.metrics.conflicts}，改期 ${trial.metrics.reschedules}`, trial.metrics.conflicts ? "warn" : "ok");
    } catch (err) {
      toast(err.message, "error");
    } finally {
      state.busy = false;
      loading(btn, false);
    }
  }

  async function parallelRun() {
    if (state.busy) return;
    state.busy = true;
    const specs = [
      { planner: "计划员 甲", mode: "global", tie_break: "balanced" },
      { planner: "计划员 乙", mode: "global", tie_break: "fairness" },
      { planner: "计划员 丙", mode: "rolling", cascade: 3 },
    ];
    ["#btnParallel", "#btnParallel2"].forEach((sel) => loading($(sel), true));
    try {
      const out = await postJSON("/api/trials/parallel", { date: state.currentDate, specs });
      const ids = out.runs.map((r) => r.result?.id).filter(Boolean);
      out.runs.forEach((r) => {
        if (r.result) state.trialById.set(r.result.id, r.result);
        if (r.error) toast(`${r.spec.planner}: ${r.error}`, "error");
      });
      state.compareIds = ids;
      await refreshSnapshot({ keepTrial: true });
      switchView("compare");
      await Promise.all(ids.map(loadCompare));
      renderComparePick(); renderCompareTable(); renderCompareDetail();
      toast(`并行试算完成 ${ids.length} 个；确认时任选其一并核对版本`, "ok");
    } catch (err) {
      toast(err.message, "error");
    } finally {
      state.busy = false;
      ["#btnParallel", "#btnParallel2"].forEach((sel) => loading($(sel), false));
    }
  }

  async function interruptRun() {
    try {
      await postJSON("/api/demo/interrupt", {
        date: state.currentDate, mode: state.mode, planner: state.planner,
      });
    } catch (err) {
      if (err.status === 409 && err.payload?.interrupted) {
        toast(`计算中断：检查点 ${err.payload.trial_id} 已保留，正式台账未写入`, "warn");
        // 中断后立即走一次恢复流程: 把 running 的检查点标记为 interrupted,
        // 正式台账保持不变, 用户可据此重新试算。
        try {
          state.recovery = await postJSON("/api/recover", {});
        } catch (recErr) {
          toast(recErr.message, "error");
        }
        await refreshSnapshot({ keepTrial: true });
        switchView("ledger");
        renderRecoveryPanel();
      } else {
        toast(err.message, "error");
      }
    }
  }

  async function refreshSnapshot(opts = {}) {
    const data = await getJSON(`/api/snapshot?date=${encodeURIComponent(state.currentDate)}`);
    state.snapshot = data;
    const list = await getJSON("/api/trials");
    state.trials = list.trials || [];
    if (!opts.keepTrial) state.trial = null;
    renderAll();
    showRecoveryBanner();
  }

  /* ------------------------------------------------------------------ */
  /* confirm */

  async function confirmModal() {
    const trial = state.trial;
    if (!trial) return;
    const snap = state.snapshot;
    const rows = Object.entries(trial.resource_versions || {}).map(([rid, ver]) => {
      const cur = snap.resources.find((r) => r.id === rid);
      return { rid, ver, cur: cur ? cur.version : null, name: cur ? cur.name : rid };
    });
    const changed = rows.filter((r) => r.cur !== r.ver);
    const locked = (trial.locked_ids || []).map((id) => examLabel(id));
    const body = `
      <p>确认后，方案中 <strong>${Object.keys(trial.bookings || {}).length}</strong> 项检查将写入正式台账；
      <strong>${Object.keys(trial.frozen || {}).length}</strong> 项已开始/冻结的预约保持原样，
      未排 <strong>${(trial.unplaced || []).length}</strong> 项保持待排。试算记录仍保留，可回放。</p>
      <table class="ver">
        <tr><td>方案依据</td><td>v${trial.base_version} → 当前 v${snap.seq}</td></tr>
        <tr><td>资源版本核对</td><td>${changed.length ? `${changed.length} 项不一致` : `全部一致（${rows.length} 项）`}</td></tr>
        <tr><td>已开始/完成冻结</td><td>${locked.length ? esc(locked.slice(0, 4).join("、")) + (locked.length > 4 ? ` 等 ${locked.length} 项` : "") : "无"}</td></tr>
        <tr><td>改期次数</td><td>${trial.metrics.reschedules}</td></tr>
      </table>
      ${changed.length ? `<div class="errbox"><strong>资源已变更，不能直接确认：</strong>
        <ul>${changed.slice(0, 8).map((r) => `<li>${esc(r.name)} (${esc(r.rid)})：方案 v${r.ver} → 当前 v${r.cur}</li>`).join("")}</ul>
        请重新运行试算，让方案基于最新资源版本。</div>` : ""}
      <div class="warnbox">确认只写入未开始的检查；已开始/完成的检查不会被移动。若台账在此期间有变更，服务器会再次核对并拒绝不合法方案。</div>`;
    modal({
      title: "确认方案",
      body,
      footer: `<button type="button" class="ghost" data-close>取消</button>
        <button type="button" class="secondary" id="btnRecompute">重新试算</button>
        <button type="button" class="primary" id="btnConfirmGo" ${changed.length ? "disabled" : ""}>写入正式台账</button>`,
      onMount(root, close) {
        $("#btnRecompute", root).onclick = () => { close(); runTrial(); };
        $("#btnConfirmGo", root).onclick = async () => {
          const go = $("#btnConfirmGo", root);
          loading(go, true);
          try {
            const out = await postJSON(`/api/trials/${trial.id}/confirm`, {
              planner: state.planner, note: "界面确认",
            });
            close();
            state.trial.status = "confirmed";
            toast(`已确认：写入 ${out.bookings} 项（事件 #${out.confirmed_seq}）`, "ok");
            (out.warnings || []).forEach((w) => toast(w, "warn"));
            await refreshSnapshot({ keepTrial: true });
            state.timelineSource = "official";
            renderTimeline();
          } catch (err) {
            loading(go, false);
            const conflicts = err.payload?.conflicts || [];
            close();
            confirmErrorModal(err, conflicts);
          }
        };
      },
    });
  }

  function confirmErrorModal(err, conflicts) {
    const body = `
      <p><strong>${esc(err.message)}</strong></p>
      ${conflicts.length ? `<div class="errbox">服务器拒绝写入，原因逐项列出：
        <ul>${conflicts.map((c) => `<li>${esc(c.message || conflictKindLabel(c.kind))}</li>`).join("")}</ul></div>` : ""}
      <div class="warnbox">方案没有写入台账。请载入最新状态重新试算；既有正式预约保持不变。</div>`;
    modal({
      title: "确认被拒绝",
      body,
      footer: `<button type="button" class="ghost" data-close>关闭</button>
        <button type="button" class="primary" id="errRecompute">重新试算</button>`,
      onMount(root, close) {
        $("#errRecompute", root).onclick = () => { close(); runTrial(); };
      },
    });
  }

  /* ------------------------------------------------------------------ */
  /* detail panel */

  function currentExamView() {
    const id = state.selectedExam;
    if (!id) return null;
    const exam = (state.snapshot.exams || []).find((e) => e.id === id);
    if (!exam) return null;
    const trial = state.trial;
    const unplaced = (trial?.unplaced || []).find((u) => u.exam_id === id);
    const conflict = (trial?.conflicts || []).find((c) => c.exam_id === id);
    // 方案中明确未排的检查, 不展示试算时段 (旧时段只属于正式台账)
    const booking = unplaced
      ? null
      : (trial?.bookings?.[id] || trial?.frozen?.[id] || null);
    const officialSlots = exam.slots || {};
    const slots = booking ? booking.slots : (unplaced ? {} : officialSlots);
    const frozen = Boolean(trial?.frozen?.[id] && !trial?.bookings?.[id]);
    return { exam, booking, slots, unplaced, conflict, frozen, officialSlots };
  }

  function renderDetail() {
    const host = $("#detailBody");
    const view = currentExamView();
    if (!view) {
      host.innerHTML = `<p class="empty-state">点击时间线中的检查块查看步骤、准备要求与执行操作。</p>`;
      return;
    }
    const { exam, booking, slots, unplaced, conflict, frozen, officialSlots } = view;
    const scheduled = Object.keys(slots).length > 0;
    const steps = exam.steps || [];
    const firstStart = scheduled ? Math.min(...Object.values(slots).map((s) => s.start)) : null;
    host.innerHTML = `
      <div class="detail-exam">
        <h3>${esc(exam.label)}</h3>
        <div class="meta">${esc(exam.id)} · ${esc(exam.template_name)} · P${exam.priority} ${esc(priorityLabel(exam.priority))}</div>
        <div class="meta">正式状态 ${esc(statusLabel(exam.status))}${unplaced ? " · 本方案未排" : ""} · 准备 ${exam.prep_done ? "已完成" : `未完成（${exam.prep_minutes}m）`} ${exam.due_by ? `· 截止 ${hm(exam.due_by)}` : ""}</div>
      </div>
      ${unplaced ? `<div class="conflict" style="margin-top:10px"><div class="ct"><b>未排原因</b><span class="kind">${esc(conflictKindLabel(unplaced.conflict_kind))}</span></div><div class="cm">${esc(unplaced.reason)}</div></div>` : ""}
      ${!unplaced && conflict ? `<div class="conflict" style="margin-top:10px"><div class="ct"><b>冲突</b><span class="kind">${esc(conflictKindLabel(conflict.kind))}</span></div><div class="cm">${esc(conflict.message)}</div></div>` : ""}
      <div class="steps">
        ${steps.map((step) => {
          const slot = slots[step.key];
          return `<div class="step ${slot ? "" : "pending"}">
            <span class="t">${slot ? `${hm(slot.start)}` : "—"}</span>
            <span class="n">${esc(step.label)}<span>${esc(step.minutes)} 分钟 · 需 ${esc(step.capability)} / ${esc(step.role)}</span></span>
            <span class="r">${slot ? `${esc(slot.resource_name || slot.resource_id)}<br>${esc(slot.staff_name || slot.staff_id)}` : "未安排"}</span>
          </div>`;
        }).join("")}
      </div>
      <div class="detail-note">${exam.prep_instruction ? `准备要求：${esc(exam.prep_instruction)}` : "无额外准备要求。"}</div>
      <div class="detail-actions">
        ${!exam.prep_done && exam.prep_minutes > 0 ? `<button type="button" class="secondary small" data-act="prep">标记准备完成</button>` : ""}
        ${exam.status === "SCHEDULED" ? `<button type="button" class="secondary small" data-act="start">开始检查</button>` : ""}
        ${exam.status === "STARTED" ? `<button type="button" class="secondary small" data-act="complete">完成检查</button>` : ""}
        ${["SCHEDULED", "UNSCHEDULED"].includes(exam.status) ? `<button type="button" class="ghost small" data-act="no-show">标记爽约</button>` : ""}
        ${["SCHEDULED", "UNSCHEDULED"].includes(exam.status) ? `<button type="button" class="ghost danger small" data-act="cancel">取消检查</button>` : ""}
      </div>
      ${booking && !frozen ? `<div class="detail-note">试算时段 ${hm(booking.start)}–${hm(booking.end)} · 修订 r${booking.revision}</div>` : ""}
      ${frozen ? `<div class="detail-note">该检查在本方案中冻结未动，沿用正式台账时段 ${exam.start_hm || "--:--"}–${exam.end_hm || "--:--"}。</div>` : ""}
      ${unplaced && Object.keys(officialSlots).length ? `<div class="detail-note" style="color:var(--dim)">正式台账当前仍有预约 ${exam.start_hm}–${exam.end_hm}；本方案未排入它，未确认前正式预约不变。</div>` : ""}
      ${exam.status === "STARTED" || exam.status === "COMPLETED" ? `<div class="detail-note" style="color:var(--ok)">该检查已开始，后续试算会冻结其时段，不再移动。</div>` : ""}
      <div class="detail-note" style="color:var(--dim)">仅演示排程操作，不构成任何医疗建议。</div>`;
    $$("[data-act]", host).forEach((btn) => {
      btn.onclick = () => examAction(exam, btn.dataset.act, btn);
    });
  }

  async function examAction(exam, action, button) {
    const startAt = exam.start_hm ? parseHM(exam.start_hm) : state.snapshot.now;
    const endAt = exam.end_hm ? parseHM(exam.end_hm) : state.snapshot.now;
    const confirmations = {
      "no-show": `确认将「${exam.label}」标记为爽约？该检查不再占用时段。`,
      cancel: `确认取消「${exam.label}」？取消后不会自动恢复。`,
      start: `开始「${exam.label}」会把当日时钟推进到 ${hm(startAt)}，之后该检查不可再移动。`,
      complete: `完成「${exam.label}」会把时钟推进到 ${hm(endAt)}。`,
      prep: `确认「${exam.label}」的检查前准备已完成？`,
    };
    if (confirmations[action]) {
      const ok = await ask(confirmations[action]);
      if (!ok) return;
    }
    loading(button, true);
    try {
      if (action === "prep") await postJSON(`/api/exams/${exam.id}/prep`, { done: true, by: state.planner });
      else if (action === "start") await postJSON(`/api/exams/${exam.id}/start`, { at: startAt, by: state.planner });
      else if (action === "complete") await postJSON(`/api/exams/${exam.id}/complete`, { at: endAt, by: state.planner });
      else if (action === "no-show") await postJSON(`/api/exams/${exam.id}/no-show`, { by: state.planner });
      else if (action === "cancel") await postJSON(`/api/exams/${exam.id}/cancel`, { reason: "演示取消", by: state.planner });
      toast(`已记录：${exam.label} · ${action}`, "ok");
      await refreshSnapshot({ keepTrial: true });
    } catch (err) {
      toast(err.message, "error");
    } finally { loading(button, false); }
  }

  function ask(message) {
    return new Promise((resolve) => {
      modal({
        title: "请确认",
        body: `<p>${esc(message)}</p>`,
        footer: `<button type="button" class="ghost" data-close>取消</button>
                 <button type="button" class="primary" id="askYes">确认</button>`,
        onMount(root, close) {
          $("#askYes", root).onclick = () => { resolve(true); close(); };
          $$("[data-close]", root).forEach((b) => b.addEventListener("click", () => resolve(false)));
        },
      });
    });
  }

  /* ------------------------------------------------------------------ */
  /* compare */

  function renderComparePick() {
    const host = $("#comparePick");
    if (!host) return;
    const trials = (state.trials || []).filter((t) => t.status !== "running");
    if (!trials.length) {
      host.innerHTML = `<p class="empty-state">还没有试算。先运行一次，或点击上方并行试算。</p>`;
      return;
    }
    host.innerHTML = trials.slice(0, 12).map((t) => {
      const m = t.metrics || {};
      return `<label class="pick">
        <input type="checkbox" value="${esc(t.id)}" ${state.compareIds.includes(t.id) ? "checked" : ""}>
        <span class="pinfo">
          <b>${esc(t.planner || "—")} · ${t.mode === "global" ? "整体优化" : "滚动调整"} <span class="badge ${esc(t.status)}">${trialStatusLabel(t.status)}</span></b>
          <span class="pm">
            <span>排 ${m.placed ?? "—"}</span><span>未排 ${m.unplaced ?? "—"}</span>
            <span>等待 ${m.avg_wait ?? "—"}m</span><span>改期 ${m.reschedules ?? "—"}</span>
            <span>公平 ${m.fairness ?? "—"}</span>
          </span>
        </span>
        <span class="pk">v${t.base_version ?? "—"}<br>${esc((t.created_at || "").slice(11, 19))}</span>
      </label>`;
    }).join("");
    $$("input[type=checkbox]", host).forEach((box) => {
      box.onchange = async () => {
        const id = box.value;
        if (box.checked) {
          state.compareIds = [...state.compareIds.filter((x) => x !== id), id].slice(-3);
          await loadCompare(id);
        } else {
          state.compareIds = state.compareIds.filter((x) => x !== id);
        }
        renderComparePick(); renderCompareTable(); renderCompareDetail();
      };
    });
    $("#cmpBadge").textContent = state.compareIds.length;
  }

  async function loadCompare(id) {
    if (state.compareData.has(id)) return state.compareData.get(id);
    try {
      const trial = await getJSON(`/api/trials/${id}`);
      state.compareData.set(id, trial);
      state.trialById.set(id, trial);
      return trial;
    } catch (err) { toast(err.message, "error"); return null; }
  }

  function compareTrials() {
    return state.compareIds.map((id) => state.compareData.get(id)).filter(Boolean);
  }

  function renderCompareTable() {
    const host = $("#compareTable");
    if (!host) return;
    const trials = compareTrials();
    if (trials.length < 1) {
      host.innerHTML = `<p class="empty-state">勾选左侧试算查看指标对照。</p>`;
      $("#compareTradeoff").innerHTML = "";
      return;
    }
    const metrics = [
      ["已排", (t) => t.metrics.placed, "high"],
      ["写入 / 冻结", (t) => `${t.metrics.moved ?? Object.keys(t.bookings || {}).length} / ${t.metrics.frozen ?? 0}`, null],
      ["未排", (t) => t.metrics.unplaced, "low"],
      ["冲突", (t) => t.metrics.conflicts, "low"],
      ["平均等待 m", (t) => t.metrics.avg_wait, "low"],
      ["最长等待 m", (t) => t.metrics.max_wait, "low"],
      ["紧急最长 m", (t) => t.metrics.urgent_max_wait, "low"],
      ["改期次数", (t) => t.metrics.reschedules, "low"],
      ["公平度", (t) => t.metrics.fairness, "high"],
      ["利用率 %", (t) => t.metrics.utilization, "high"],
      ["完成时刻", (t) => t.metrics.makespan_hm, "time"],
    ];
    const head = `<tr><th>指标</th>${trials.map((t) => `<th>${esc(t.planner || t.id.slice(-5))}<br><span style="font-weight:400">${t.mode === "global" ? "整体" : "滚动"} · v${t.base_version}</span></th>`).join("")}</tr>`;
    const rows = metrics.map(([label, get, dir]) => {
      const values = trials.map(get);
      let bestIdx = -1;
      if (dir === "time") {
        bestIdx = values.indexOf(values.slice().sort()[0]);
      } else if (typeof values[0] === "number") {
        const target = dir === "high" ? Math.max(...values) : Math.min(...values);
        bestIdx = values.indexOf(target);
      }
      const cells = values.map((v, i) => {
        let cls = "";
        if (trials.length > 1 && values.length > 1) {
          if (i === bestIdx) cls = "best";
          else if (dir !== "high" ? v > values[bestIdx] : v < values[bestIdx]) cls = "worst";
        }
        return `<td class="${cls}">${esc(v)}</td>`;
      }).join("");
      return `<tr><td>${esc(label)}</td>${cells}</tr>`;
    }).join("");
    host.innerHTML = `<table><thead>${head}</thead><tbody>${rows}</tbody></table>`;
    renderTradeoff(trials);
  }

  function renderTradeoff(trials) {
    const host = $("#compareTradeoff");
    if (trials.length < 2) {
      host.innerHTML = `<div class="tr">再选择一个方案即可看到取舍解释。</div>`;
      return;
    }
    const byWait = trials.slice().sort((a, b) => a.metrics.avg_wait - b.metrics.avg_wait);
    const byChange = trials.slice().sort((a, b) => a.metrics.reschedules - b.metrics.reschedules);
    const byFair = trials.slice().sort((a, b) => b.metrics.fairness - a.metrics.fairness);
    const waitBest = byWait[0];
    const changeBest = byChange[0];
    const fairBest = byFair[0];
    const lines = [];
    lines.push(`<div class="tr"><b>等待</b>：${esc(waitBest.planner || waitBest.id)} 的平均等待最低（${waitBest.metrics.avg_wait} 分钟），紧急检查最长等待 ${waitBest.metrics.urgent_max_wait} 分钟。</div>`);
    lines.push(`<div class="tr"><b>改期</b>：${esc(changeBest.planner || changeBest.id)} 只改期 ${changeBest.metrics.reschedules} 次，对既有预约扰动最小。</div>`);
    lines.push(`<div class="tr"><b>资源公平</b>：${esc(fairBest.planner || fairBest.id)} 的公平度最高（${fairBest.metrics.fairness}），不同优先级之间的等待差距更小。</div>`);
    if (waitBest.id !== changeBest.id) {
      lines.push(`<div class="tr"><b>取舍</b>：最低等待与最少改期不在同一方案上。整体优化通常压低等待，但会移动更多既有预约；滚动调整保留稳定，却可能让新插单等待更久。</div>`);
    } else {
      lines.push(`<div class="tr"><b>取舍</b>：本组中等待与改期恰好同向，可优先选该方案；仍应核对紧急检查的截止与设备版本。</div>`);
    }
    host.innerHTML = lines.join("");
  }

  function renderCompareDetail() {
    const host = $("#compareDetail");
    if (!host) return;
    const trials = compareTrials();
    if (!trials.length) {
      host.innerHTML = `<p class="empty-state">选择方案后展示每项检查的时段差异与冲突原因。</p>`;
      return;
    }
    const snap = state.snapshot;
    const examIds = Array.from(new Set(trials.flatMap((t) => [
      ...Object.keys(t.bookings || {}), ...Object.keys(t.frozen || {}),
    ])));
    const planSlot = (t, id) => t.bookings?.[id] || t.frozen?.[id] || null;
    const cols = trials.map((t) => {
      const changes = examIds.filter((id) => {
        const b = planSlot(t, id);
        const official = snap.exams.find((e) => e.id === id);
        return b && official && official.start_hm && official.start_hm !== hm(b.start);
      });
      const lines = examIds.map((id) => {
        const b = planSlot(t, id);
        if (!b) return "";
        const official = snap.exams.find((e) => e.id === id);
        const changed = official && official.start_hm && official.start_hm !== hm(b.start);
        const frozen = Boolean(t.frozen?.[id] && !t.bookings?.[id]);
        return `<div class="diff-line">
          <span class="t">${esc(hm(b.start))}</span>
          <span class="n">${esc(examLabel(id))}${frozen ? ' <span class="kind" style="border-color:#3b4a5d;color:#9fb2c7;background:#141d27">冻结</span>' : ""}</span>
          ${changed ? `<span class="d">原 ${esc(official.start_hm)}</span>` : `<span class="d">—</span>`}
        </div>`;
      }).join("");
      const unplaced = (t.unplaced || []).map((u) => `
        <div class="conflict" style="margin-top:8px">
          <div class="ct"><b>${esc(u.label)}</b><span class="kind">${esc(conflictKindLabel(u.conflict_kind))}</span></div>
          <div class="cm">${esc(u.reason)}</div>
        </div>`).join("");
      return `<div class="diff-col">
        <header>
          <b>${esc(t.planner || t.id.slice(-6))}</b>
          <span class="badge ${t.mode}">${t.mode === "global" ? "整体" : "滚动"}</span>
          <span class="badge ${esc(t.status)}">${trialStatusLabel(t.status)}</span>
          <span class="d" style="margin-left:auto;color:var(--dim);font-size:10.5px">${changes.length} 项改期</span>
        </header>
        <div class="body">${lines || '<p class="empty-state">无已排检查</p>'}${unplaced}</div>
      </div>`;
    }).join("");
    host.innerHTML = `<div class="diff-grid">${cols}</div>`;
  }

  /* ------------------------------------------------------------------ */
  /* ledger & recovery */

  function renderLedger() {
    const host = $("#ledgerOps");
    if (!host) return;
    const ops = (state.snapshot?.ops || []).slice().reverse();
    if (!ops.length) {
      host.innerHTML = `<p class="empty-state">暂无执行记录。开始/完成/爽约等操作会追加到这里。</p>`;
    } else {
      host.innerHTML = ops.map((op) => {
        const when = op.at ? "" : "";
        return `<div class="op">
          <span class="s">#${esc(op.seq ?? "")}</span>
          <span class="t">${esc(op.type)}</span>
          <span>${esc(opSummary(op))}${when}</span>
        </div>`;
      }).join("");
    }
    renderRecoveryPanel();
  }

  function opSummary(op) {
    const exam = op.exam_id ? examLabel(op.exam_id) : "";
    switch (op.type) {
      case "booking.started": return `${exam} 开始 · 时钟 ${hm(op.at)}`;
      case "booking.completed": return `${exam} 完成 · 时钟 ${hm(op.at)}`;
      case "exam.no_show": return `${exam} 标记爽约`;
      case "exam.cancelled": return `${exam} 取消 · ${op.reason || ""}`;
      case "booking.reset": return `${exam} 退回未排 · ${op.reason || ""}`;
      case "booking.confirmed": return `确认方案 ${op.plan_id || ""} · ${op.mode === "global" ? "整体优化" : "滚动调整"} · ${op.bookings} 项 · 计划员 ${op.planner || "—"}`;
      default: return JSON.stringify(op).slice(0, 120);
    }
  }

  function renderRecoveryPanel() {
    const host = $("#recoveryPanel");
    if (!host) return;
    const rec = state.recovery || {};
    const interrupted = (state.trials || []).filter((t) => t.status === "interrupted");
    const rows = [];
    rows.push(`<div class="rec-item"><b>台账</b><span class="grow">当前 v${state.snapshot?.seq ?? "—"} · 重放 ${rec.ledger_seq ?? "—"} 个事件</span><span class="badge complete">只追加</span></div>`);
    rows.push(`<div class="rec-item"><b>损坏行</b><span class="grow">${rec.corrupt_lines_dropped ? `已丢弃 ${rec.corrupt_lines_dropped} 行并备份` : "未发现"}</span><span class="badge ${rec.corrupt_lines_dropped ? "interrupted" : "complete"}">${rec.corrupt_lines_dropped ? "已修复" : "正常"}</span></div>`);
    rows.push(`<div class="rec-item"><b>中断试算</b><span class="grow">${interrupted.length ? interrupted.map((t) => t.id).join("、") : "无"}</span><span class="badge ${interrupted.length ? "interrupted" : "complete"}">${interrupted.length || "0"}</span></div>`);
    rows.push(`<div class="rec-item"><b>试算/正式</b><span class="grow">试算独立目录；中断不写正式台账</span><span class="badge complete">隔离</span></div>`);
    host.innerHTML = rows.join("");
  }

  /* ------------------------------------------------------------------ */
  /* replay */

  async function loadReplay(upto) {
    try {
      const q = new URLSearchParams({ date: state.currentDate });
      if (upto !== null && upto !== undefined) q.set("upto", upto);
      const data = await getJSON(`/api/replay?${q}`);
      state.replay = data;
      state.replayUpto = data.current_seq;
      renderReplay();
      updateStepHints();
    } catch (err) { toast(err.message, "error"); }
  }

  function renderReplay() {
    const data = state.replay;
    if (!data) return;
    const range = $("#replayRange");
    range.min = "0";
    range.max = String(data.seq);
    range.value = String(data.current_seq ?? data.seq);
    $("#replaySeqLabel").textContent = `v${data.current_seq ?? data.seq} / v${data.seq}`;
    $("#replayCount").textContent = data.count;
    $("#replayMeta").innerHTML = `
      <span>日期 ${esc(data.date)}</span>
      <span>当前时钟 ${esc(data.now_hm)}</span>
      <span>事件 ${data.count} 条</span>
      <span>台账末尾 v${data.seq}</span>`;
    $("#replayClock").textContent = `时钟 ${data.now_hm}`;

    const confirmSeq = Math.max(0, ...data.events.filter((e) => e.type === "booking.confirmed").map((e) => e.seq));
    const host = $("#replayEvents");
    const events = data.events.slice().reverse();
    host.innerHTML = events.map((e) => `
      <li class="${e.seq > (data.current_seq || 0) ? "future" : ""} ${e.type === "booking.confirmed" ? "focus" : ""}">
        <span class="seq">#${e.seq}</span>
        <span>
          <div class="es">${esc(e.summary)}</div>
          <div class="et">${esc(e.type)} · ${esc((e.at || "").replace("T", " ").replace("Z", ""))}</div>
        </span>
      </li>`).join("") || `<li class="empty-state">暂无事件</li>`;

    const exams = (data.exams || []).slice().sort((a, b) => {
      if (a.start_hm === "--:--" && b.start_hm !== "--:--") return 1;
      if (b.start_hm === "--:--" && a.start_hm !== "--:--") return -1;
      return a.start_hm.localeCompare(b.start_hm);
    });
    $("#replayExams").innerHTML = exams.map((e) => `
      <div class="replay-exam">
        <span class="rt">${esc(e.start_hm)}–${esc(e.end_hm)}</span>
        <span class="rn">${esc(e.label)}</span>
        <span class="rs">${esc(statusLabel(e.status))}</span>
      </div>`).join("") || `<p class="empty-state">该时刻无检查</p>`;

    range.oninput = () => {
      const v = Number(range.value);
      $("#replaySeqLabel").textContent = `v${v} / v${data.seq}`;
      window.clearTimeout(state._replayTimer);
      state._replayTimer = window.setTimeout(() => loadReplay(v), 120);
    };
    range.onchange = () => loadReplay(Number(range.value));
    $("#btnReplayLatest").onclick = () => loadReplay(null);
    $("#btnReplayConfirm").onclick = () => {
      if (confirmSeq) loadReplay(confirmSeq);
      else toast("当前日期还没有确认记录", "warn");
    };
  }

  /* ------------------------------------------------------------------ */
  /* view switching & wiring */

  function switchView(name) {
    $$(".tabs button").forEach((btn) => {
      const active = btn.dataset.view === name;
      btn.classList.toggle("active", active);
      btn.setAttribute("aria-selected", active);
    });
    $$(".view").forEach((v) => v.classList.toggle("active", v.id === `view-${name}`));
    if (name === "replay") loadReplay(state.replayUpto);
    if (name === "compare") {
      renderComparePick();
      renderCompareTable();
      renderCompareDetail();
    }
  }

  function bind() {
    $$(".tabs button").forEach((btn) => {
      btn.onclick = () => switchView(btn.dataset.view);
    });
    $$(".seg[aria-label='排程模式'] button").forEach((btn) => {
      btn.onclick = () => {
        state.mode = btn.dataset.mode;
        $$(".seg[aria-label='排程模式'] button").forEach((b) => {
          const on = b === btn;
          b.classList.toggle("active", on);
          b.setAttribute("aria-pressed", on);
        });
        $("#cascadeField").hidden = state.mode !== "rolling";
        $("#tieField").style.visibility = state.mode === "rolling" ? "hidden" : "visible";
        renderTimeline();
      };
    });
    $("#tieBreak").onchange = (e) => { state.tieBreak = e.target.value; };
    $("#cascade").oninput = (e) => {
      state.cascade = Number(e.target.value);
      $("#cascadeVal").textContent = state.cascade;
    };
    $("#planner").onchange = (e) => { state.planner = e.target.value.trim() || "计划员"; };
    $("#planner").oninput = (e) => { state.planner = e.target.value.trim() || "计划员"; };
    $("#btnRun").onclick = () => runTrial();
    $("#btnParallel").onclick = parallelRun;
    $("#btnParallel2").onclick = parallelRun;
    $("#btnInterrupt").onclick = interruptRun;
    $("#btnInterrupt2").onclick = interruptRun;
    $("#btnRecover").onclick = doRecover;
    $("#btnRecover2").onclick = doRecover;
    $("#btnRefreshTrials").onclick = async () => {
      const list = await getJSON("/api/trials");
      state.trials = list.trials || [];
      renderTrialList(); renderComparePick(); renderCompareTable();
      toast("试算记录已刷新");
    };
    $("#btnReset").onclick = async () => {
      const ok = await ask("重置会归档当前台账与试算，并重建演示数据。确定继续？");
      if (!ok) return;
      try {
        const out = await postJSON("/api/demo/reset", {});
        toast(`已重置：台账 v${out.seq}；旧数据归档 ${out.ledger_archive || "—"}`, "ok");
        state.trial = null; state.compareIds = []; state.compareData.clear();
        state.trialById.clear(); state.selectedExam = null; state.replay = null;
        await bootstrap(state.currentDate);
      } catch (err) { toast(err.message, "error"); }
    };
  }

  async function doRecover() {
    try {
      const out = await postJSON("/api/recover", {});
      state.recovery = out;
      await refreshSnapshot({ keepTrial: true });
      switchView("ledger");
      const interrupted = out.trials_marked_interrupted || [];
      toast(
        `恢复完成：台账 v${out.ledger_seq}；中断试算 ${interrupted.length} 个；损坏行 ${out.corrupt_lines_dropped} 条`,
        interrupted.length ? "warn" : "ok"
      );
    } catch (err) { toast(err.message, "error"); }
  }

  /* 演示步骤条 (动态插入 toolbar 下方) */
  function injectStepStrip() {
    const anchor = $("#recoveryBanner");
    const strip = document.createElement("div");
    strip.id = "stepStrip";
    strip.className = "step-strip";
    strip.setAttribute("aria-label", "演示流程");
    anchor.parentNode.insertBefore(strip, anchor);
    updateStepHints();
  }

  /* ------------------------------------------------------------------ */

  document.addEventListener("DOMContentLoaded", async () => {
    bind();
    injectStepStrip();
    await bootstrap();
  });
})();
