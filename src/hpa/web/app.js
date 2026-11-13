"use strict";

const $ = (sel) => document.querySelector(sel);
const esc = (s) =>
  String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

function toast(text, ms = 2600) {
  const el = document.createElement("div");
  el.className = "toast";
  el.textContent = text;
  document.body.appendChild(el);
  setTimeout(() => el.remove(), ms);
}

async function api(path, body) {
  const res = await fetch(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body || {}),
  });
  const data = await res.json().catch(() => ({ error: "响应不是合法 JSON" }));
  if (!res.ok) throw new Error(data.error || `HTTP ${res.status}`);
  return data;
}

let editing = false;
let timer = null;
let paused = false;

function fmtTime(ts) {
  if (!ts) return "从未";
  return String(ts).replace("T", " ").replace(/\.\d+.*$/, "").replace(/\+00:00$/, "");
}

function renderStatus(st) {
  const c = st.counts || {};
  $("#statusbar").innerHTML =
    `<span>邮件 ${c.messages ?? 0}</span>` +
    `<span>待办 ${c.todos_open ?? 0}</span>` +
    `<span>待确认 ${c.drafts_pending ?? 0}</span>` +
    `<span>已发 ${c.sent ?? 0}</span>` +
    `<span>上次同步 ${esc(fmtTime(st.last_sync))}</span>` +
    `<span>上次索引 ${esc(fmtTime(st.last_index))}</span>` +
    (st.paused ? `<span class="paused-pill">⏸ 已暂停</span>` : "");
  paused = !!st.paused;
  const btn = $("#pause");
  btn.textContent = paused ? "恢复自动处理" : "暂停自动处理";
  btn.classList.toggle("primary", paused);
  btn.classList.toggle("secondary", !paused);
  $("#paused-hint").hidden = !paused;
}

function renderDrafts(drafts) {
  $("#draft-count").textContent = drafts.length;
  $("#drafts-empty").hidden = drafts.length > 0;
  const box = $("#drafts");
  box.innerHTML = "";
  for (const d of drafts) {
    const card = document.createElement("article");
    card.className = "card";
    const err = d.last_error
      ? `<div class="warn">上次发送失败：${esc(d.last_error)}</div>`
      : "";
    const sending = d.status === "sending"
      ? `<div class="warn">状态未知：上次发送中断，可能已发出。请到邮箱核对后选择「已发出 / 未发出」。</div>`
      : "";
    const resolveBtns = d.status === "sending"
      ? `<button class="secondary" data-act="resolve-sent">已发出</button>
         <button class="secondary" data-act="resolve-failed">未发出</button>`
      : "";
    card.innerHTML = `
      <div class="meta">给 <strong>${esc(d.to_addr)}</strong> · 版本 ${d.version} · ${esc(d.status)}</div>
      <div class="meta">原邮件：${esc(d.mail_subject || "")} · 来自 ${esc(d.mail_from || "")} · ${esc(fmtTime(d.mail_received_at))}</div>
      <input class="subject" value="${esc(d.subject)}" aria-label="主题">
      <textarea class="body" aria-label="正文">${esc(d.body)}</textarea>
      <div class="citations">引用：${d.citations && d.citations.length ? d.citations.map(esc).join(" · ") : "（无）"}</div>
      ${err}${sending}
      <div class="actions">
        <button class="primary" data-act="confirm">确认发送</button>
        <button data-act="save">保存修改</button>
        <button class="secondary" data-act="correct">记录纠正</button>
        <button class="danger" data-act="reject">驳回</button>
        ${resolveBtns}
      </div>`;
    const subject = card.querySelector(".subject");
    const body = card.querySelector(".body");
    const focusIn = () => { editing = true; };
    const focusOut = () => { editing = document.activeElement === subject || document.activeElement === body; };
    subject.addEventListener("focus", focusIn);
    body.addEventListener("focus", focusIn);
    subject.addEventListener("blur", focusOut);
    body.addEventListener("blur", focusOut);

    card.querySelectorAll("button[data-act]").forEach((btn) => {
      btn.addEventListener("click", async () => {
        const act = btn.dataset.act;
        btn.disabled = true;
        try {
          if (act === "confirm") {
            const ok = window.confirm(
              `确认把下面这封邮件发出去？\n\n收件人：${d.to_addr}\n主题：${subject.value}\n\n发送后不可撤回。`
            );
            if (!ok) return;
            const r = await api(`/api/drafts/${d.id}/confirm`, {});
            if (r.status === "sent") toast("已发送");
            else if (r.status === "failed") toast(`发送失败：${r.error || "未知原因"}`, 5000);
            else toast(`状态：${r.status}`);
          } else if (act === "save") {
            const r = await api(`/api/drafts/${d.id}/edit`, { subject: subject.value, body: body.value });
            toast(r.status === "edited" ? `已保存为新版本 v${r.version}，需重新确认` : `状态：${r.status}`);
          } else if (act === "reject") {
            if (!window.confirm("驳回后这封草稿不再出现在队列里，确定？")) return;
            await api(`/api/drafts/${d.id}/reject`, { reason: "手机端驳回" });
            toast("已驳回");
          } else if (act === "correct") {
            const note = window.prompt("哪里做错了？写清楚，成长循环会把它固化成规则。", "");
            if (!note) return;
            await api(`/api/drafts/${d.id}/correct`, { note });
            toast("已记录纠正");
          } else if (act === "resolve-sent") {
            await api(`/api/drafts/${d.id}/resolve`, { sent: true });
            toast("已标记为已发出");
          } else if (act === "resolve-failed") {
            await api(`/api/drafts/${d.id}/resolve`, { sent: false });
            toast("已标记为未发出，可重新确认");
          }
        } catch (e) {
          toast(String(e.message || e), 5000);
        } finally {
          btn.disabled = false;
          await loadState();
        }
      });
    });
    box.appendChild(card);
  }
}

function renderTodos(todos) {
  $("#todo-count").textContent = todos.length;
  $("#todos-empty").hidden = todos.length > 0;
  const box = $("#todos");
  box.innerHTML = todos
    .map(
      (t) => `<div class="todo" data-id="${t.id}">` +
        `<button class="check" data-act="done" aria-label="标记完成" title="标记完成">✓</button>` +
        `<div class="title">${esc(t.title)}</div>` +
        `<div class="deadline">${esc(t.deadline || "")}</div></div>`
    )
    .join("");
  box.querySelectorAll("button[data-act=done]").forEach((btn) => {
    btn.addEventListener("click", async () => {
      const id = btn.closest(".todo").dataset.id;
      btn.disabled = true;
      try {
        await api(`/api/todos/${id}/done`, {});
        toast("已完成");
      } catch (e) {
        toast(String(e.message || e), 5000);
      } finally {
        await loadState();
      }
    });
  });
}

function renderCorrections(rows) {
  $("#corrections-empty").hidden = rows.length > 0;
  $("#corrections").innerHTML = rows
    .map((c) => `<div class="correction"><time>${esc(fmtTime(c.created_at))}</time>${esc(c.raw)}</div>`)
    .join("");
}

// ---- 健身：记录一次训练 ----
const TAB_KEY = "hpa:tab";
let fxCatalog = [];
let fxPending = [];
let profileDirty = false;

function shanghaiNow() {
  try {
    const parts = new Intl.DateTimeFormat("en-CA", {
      timeZone: "Asia/Shanghai",
      year: "numeric", month: "2-digit", day: "2-digit",
      hour: "2-digit", minute: "2-digit", hourCycle: "h23",
    }).formatToParts(new Date());
    const get = (type) => parts.find((part) => part.type === type).value;
    return {
      date: `${get("year")}-${get("month")}-${get("day")}`,
      time: `${get("hour")}:${get("minute")}`,
    };
  } catch (_) {
    // 旧浏览器没有时区参数时退回设备时间；保存时服务端仍会按上海日期校验。
    const d = new Date();
    const pad = (n) => String(n).padStart(2, "0");
    return {
      date: `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`,
      time: `${pad(d.getHours())}:${pad(d.getMinutes())}`,
    };
  }
}

function localToday() {
  return shanghaiNow().date;
}

function fmtVolume(volume, kind) {
  if (kind === "reps") return `${volume} 次`;
  if (volume >= 1000) return `${(volume / 1000).toFixed(1)}t`;
  return `${volume}kg`;
}

function fmtSet(set, bodyweight) {
  return bodyweight ? `${set.reps} 次` : `${set.weight_kg}×${set.reps}`;
}

function parseRest(value) {
  const text = String(value || "").trim();
  if (!text) return { min: null, max: null };
  const match = text.match(/^(\d+)(?:\s*[-~–—至]\s*(\d+))?\s*(?:秒|s)?$/i);
  if (!match) throw new Error("组间歇写成 90 或 60-90");
  const min = Number(match[1]);
  const max = match[2] === undefined ? null : Number(match[2]);
  if (min > 600 || (max !== null && max > 600)) throw new Error("组间歇应在 0–600 秒");
  if (max !== null && max < min) throw new Error("组间歇最长时间不能小于最短时间");
  return { min, max };
}

function parseDrops(value) {
  const text = String(value || "").trim();
  if (!text) return [];
  return text.split(/[,，+]/).map((part) => part.trim()).filter(Boolean).map((part) => {
    const match = part.match(/^(\d+(?:\.\d+)?)\s*[x×*]\s*(\d+)$/i);
    if (!match) throw new Error("递减组写成 7.5x10, 2.5x10");
    const weight = Number(match[1]);
    const reps = Number(match[2]);
    if (!(weight >= 0 && weight <= 500)) throw new Error("递减组重量应在 0–500 kg");
    if (!Number.isInteger(reps) || reps < 1 || reps > 100) throw new Error("递减组次数应在 1–100");
    return { weight, reps };
  });
}

function restText(min, max) {
  if (min === null || min === undefined) return "";
  return max !== null && max !== undefined && max !== min ? `休息 ${min}–${max}s` : `休息 ${min}s`;
}

function setTab(name, persist = true) {
  const tab = name === "fitness" ? "fitness" : "mail";
  $("#tab-mail").hidden = tab !== "mail";
  $("#tab-fitness").hidden = tab !== "fitness";
  document.querySelectorAll(".tab").forEach((btn) => {
    const active = btn.dataset.tab === tab;
    btn.classList.toggle("active", active);
    btn.setAttribute("aria-selected", active ? "true" : "false");
  });
  if (persist) {
    try { localStorage.setItem(TAB_KEY, tab); } catch (_) { /* 隐私模式忽略 */ }
  }
}

function renderExerciseOptions() {
  const select = $("#fx-exercise");
  const current = select.value;
  select.innerHTML = fxCatalog
    .map((ex) => `<option value="${esc(ex.id)}">${esc(ex.name)}${ex.bodyweight ? "（自重）" : ""}</option>`)
    .join("");
  if (current && fxCatalog.some((ex) => ex.id === current)) select.value = current;
  syncWeightInput();
}

function syncWeightInput() {
  const ex = fxCatalog.find((item) => item.id === $("#fx-exercise").value);
  const weight = $("#fx-weight");
  const bodyweight = !!(ex && ex.bodyweight);
  weight.disabled = bodyweight;
  if (bodyweight) weight.value = "";
  weight.placeholder = bodyweight ? "自重，不用填" : "重量 kg";
}

function renderPending() {
  const box = $("#fx-pending");
  box.innerHTML = fxPending
    .map((ex, i) => {
      const kind = ex.bodyweight ? "reps" : "kg";
      const volume = ex.warmup
        ? 0
        : ex.sets.reduce(
          (sum, set) => sum + set.segments.reduce(
            (part, seg) => part + (ex.bodyweight
              ? Number(seg.reps || 0)
              : Number(seg.weight || 0) * Number(seg.reps || 0)),
            0,
          ),
          0,
        );
      const rows = ex.sets
        .map((set, j) => set.segments
          .map((seg, k) => {
            const where = `第 ${j + 1} 组${set.segments.length > 1 ? `第 ${k + 1} 段` : ""}`;
            const weightInput = ex.bodyweight
              ? ""
              : `<input type="number" data-ex="${i}" data-set="${j}" data-seg="${k}" data-field="weight" value="${seg.weight ?? ""}" min="0" max="500" step="0.5" inputmode="decimal" aria-label="${where}重量"><span class="fx-unit">kg</span>`;
            return `<div class="fx-set${k > 0 ? " fx-drop" : ""}">
              <span class="fx-set-no">${k === 0 ? j + 1 : "↳"}</span>
              ${weightInput}
              ${ex.bodyweight ? "" : '<span class="fx-x">×</span>'}
              <input type="number" data-ex="${i}" data-set="${j}" data-seg="${k}" data-field="reps" value="${seg.reps}" min="1" max="100" inputmode="numeric" aria-label="${where}次数">
              <span class="muted">次</span>
            </div>`;
          })
          .join(""))
        .join("");
      const tags = [];
      if (ex.warmup) tags.push("热身");
      const rest = restText(ex.restSeconds, ex.restSecondsMax);
      if (rest) tags.push(rest);
      return `<article class="fx-item">
        <div class="fx-item-head">
          <strong>${esc(ex.name)}</strong>
          <span class="fx-summary muted">${ex.sets.length} 组${tags.length ? ` · ${tags.join(" · ")}` : ""} · ${fmtVolume(Math.round(volume), kind)}</span>
          <button class="secondary" data-remove="${i}" title="只从这次未保存的记录里移除">移除动作</button>
        </div>
        ${rows}
      </article>`;
    })
    .join("");
}

function addExercise() {
  const ex = fxCatalog.find((item) => item.id === $("#fx-exercise").value);
  if (!ex) return;
  const reps = Number($("#fx-reps").value);
  const sets = Number($("#fx-sets").value);
  const weight = Number($("#fx-weight").value);
  if (!Number.isInteger(sets) || sets < 1 || sets > 20) { toast("组数应在 1–20"); return; }
  if (!Number.isInteger(reps) || reps < 1 || reps > 100) { toast("次数应在 1–100"); return; }
  if (!ex.bodyweight && ($("#fx-weight").value === "" || !(weight >= 0 && weight <= 500))) {
    toast("重量应在 0–500 kg");
    return;
  }
  let rest;
  let drops;
  try {
    rest = parseRest($("#fx-rest").value);
    drops = parseDrops($("#fx-drops").value);
  } catch (e) {
    toast(e.message);
    return;
  }
  if (ex.bodyweight && drops.length) {
    toast("自重动作不用填递减组");
    return;
  }
  const segments = [{ weight: ex.bodyweight ? null : weight, reps }, ...drops];
  fxPending.push({
    exercise: ex.id,
    name: ex.name,
    bodyweight: ex.bodyweight,
    restSeconds: rest.min,
    restSecondsMax: rest.max,
    warmup: $("#fx-warmup").checked,
    sets: Array.from({ length: sets }, () => ({
      segments: segments.map((seg) => ({ ...seg })),
    })),
  });
  renderPending();
  $("#fx-reps").value = "";
  $("#fx-weight").value = "";
  $("#fx-drops").value = "";
  $("#fx-warmup").checked = false;
}

async function parseQuick() {
  const text = $("#fx-quick-text").value.trim();
  if (!text) {
    toast("先写一句话");
    return;
  }
  if (fxPending.length && !confirm("替换当前未保存的动作？")) return;
  let data;
  try {
    data = await api("/api/fitness/parse", { text });
  } catch (e) {
    toast(String(e.message || e), 5000);
    return;
  }
  const hint = $("#fx-quick-hint");
  hint.hidden = false;
  if (data.parser === "none") {
    fxPending = [];
    renderPending();
    hint.textContent = data.message || "没解析出来，请手动填下面的表单。";
    return;
  }
  const draft = data.draft || {};
  fxPending = (draft.exercises || []).map((ex) => ({
    exercise: ex.exercise,
    name: ex.name,
    bodyweight: !!ex.bodyweight,
    restSeconds: null,
    restSecondsMax: null,
    warmup: false,
    sets: (ex.sets || []).map((set) => ({
      segments: [{ weight: set.weight_kg, reps: set.reps }],
    })),
  }));
  renderPending();
  const uncertain = data.uncertain || [];
  hint.textContent = uncertain.length
    ? `已回填。不确定：${uncertain.join("、")}，请核对后保存。`
    : "已回填，核对后保存。";
  toast(data.parser === "llm" ? "已用模型解析，请核对" : "本地规则已回填");
}

function clearWorkoutForm() {
  fxPending = [];
  renderPending();
  $("#fx-quick-hint").hidden = true;
  $("#fx-note").value = "";
  $("#fx-time").value = shanghaiNow().time;
  $("#fx-rest").value = "";
  $("#fx-drops").value = "";
  $("#fx-warmup").checked = false;
}

async function saveWorkout() {
  if (!fxPending.length) { toast("先添加至少一个动作"); return; }
  for (const ex of fxPending) {
    for (const set of ex.sets) {
      for (const seg of set.segments) {
        const reps = Number(seg.reps);
        if (!Number.isInteger(reps) || reps < 1 || reps > 100) {
          toast(`${ex.name} 的次数应在 1–100`);
          return;
        }
        if (!ex.bodyweight) {
          const weight = Number(seg.weight);
          if (seg.weight === null || seg.weight === "" || !Number.isFinite(weight) || weight < 0 || weight > 500) {
            toast(`${ex.name} 的重量应在 0–500 kg`);
            return;
          }
        }
      }
    }
  }
  const exercises = fxPending.map((ex) => ({
    exercise: ex.exercise,
    rest_seconds: ex.restSeconds,
    rest_seconds_max: ex.restSecondsMax,
    warmup: ex.warmup === true,
    sets: ex.sets.map((set) => ({
      segments: set.segments.map((seg) => (ex.bodyweight
        ? { reps: Number(seg.reps) }
        : { weight_kg: Number(seg.weight), reps: Number(seg.reps) })),
    })),
  }));
  const btn = $("#fx-save");
  btn.disabled = true;
  try {
    await api("/api/fitness/record", {
      kind: "workout",
      performed_on: $("#fx-date").value || localToday(),
      started_at: $("#fx-time").value || null,
      note: $("#fx-note").value,
      exercises,
    });
    clearWorkoutForm();
    toast("已记录");
    await loadState();
  } catch (e) {
    toast(String(e.message || e), 5000);
  } finally {
    btn.disabled = false;
  }
}

async function deleteWorkout(id) {
  if (!window.confirm("删除这次训练？组数据会一起删掉，建议不再引用它。")) return;
  try {
    await api(`/api/fitness/workouts/${id}/delete`, { confirm: true });
    toast("已删除");
    await loadState();
  } catch (e) {
    toast(String(e.message || e), 5000);
  }
}

function renderRecent(workouts) {
  $("#fitness-recent-empty").hidden = workouts.length > 0;
  $("#fitness-recent").innerHTML = workouts
    .map((workout) => {
      const exercises = workout.exercises
        .map((ex) => {
          const groups = new Map();
          for (const set of ex.sets) {
            if (!groups.has(set.set_index)) groups.set(set.set_index, []);
            groups.get(set.set_index).push(set);
          }
          const bodyweight = ex.volume_kind === "reps";
          const rows = [...groups.values()]
            .map((segs) => {
              const first = segs[0];
              const body = segs.map((seg) => fmtSet(seg, bodyweight)).join(" + ");
              const rest = restText(first.rest_seconds, first.rest_seconds_max);
              const label = first.warmup ? "热身" : `第 ${first.set_index} 组`;
              return `<div class="fx-recent-sets">${label}：${body}${rest ? ` · ${rest}` : ""}</div>`;
            })
            .join("");
          const hasWarmup = ex.sets.some((set) => set.warmup);
          const count = ex.set_count || groups.size;
          return `<div class="fx-recent-ex">
          <strong>${esc(ex.name)}</strong>
          <span class="muted">${count} 组${hasWarmup ? "（含热身）" : ""} · ${fmtVolume(ex.volume, ex.volume_kind)}</span>
          ${rows}
        </div>`;
        })
        .join("");
      const note = workout.note ? `<div class="fx-recent-note">备注（仅本机）：${esc(workout.note)}</div>` : "";
      const when = workout.started_at ? `${workout.performed_on} ${workout.started_at}` : workout.performed_on;
      return `<article class="card">
        <div class="meta">${esc(when)}</div>
        ${exercises}${note}
        <div class="actions"><button class="danger" data-delete="${workout.id}">删除这次训练</button></div>
      </article>`;
    })
    .join("");
}

function renderWeights(weights) {
  $("#fitness-weights-empty").hidden = weights.length > 0;
  const today = localToday();
  const mine = weights.find((w) => w.logged_on === today);
  $("#fx-weight-today").textContent = mine ? `今天已记 ${Number(mine.weight_kg).toFixed(1)} kg` : "";
  if (!weights.length) {
    $("#fitness-weights").innerHTML = "";
    return;
  }
  const values = weights.map((w) => Number(w.weight_kg));
  const min = Math.min(...values);
  const max = Math.max(...values);
  const span = max - min;
  $("#fitness-weights").innerHTML = weights
    .map((w, i) => {
      const value = Number(w.weight_kg);
      const pct = span > 0 ? 20 + ((value - min) / span) * 80 : 60;
      const prev = weights[i + 1];
      const delta = prev ? value - Number(prev.weight_kg) : null;
      const deltaText = delta === null
        ? ""
        : ` <span class="muted">${delta >= 0 ? "+" : ""}${delta.toFixed(1)}</span>`;
      return `<div class="fx-weight-row">
        <span class="fx-weight-date">${esc(w.logged_on.slice(5))}</span>
        <span class="fx-weight-bar"><span style="width:${pct.toFixed(0)}%"></span></span>
        <span class="fx-weight-kg">${value.toFixed(1)} kg${deltaText}</span>
      </div>`;
    })
    .join("");
}

function renderProfile(profile) {
  const missing = profile.missing || [];
  const box = $("#pf-missing");
  box.hidden = missing.length === 0;
  box.textContent = missing.length
    ? `画像还缺：${missing.join("、")}。建议会明说缺什么，不会瞎猜。`
    : "";
  if (profileDirty) return;
  const active = document.activeElement;
  const fields = ["pf-goal", "pf-days", "pf-equipment", "pf-injuries"];
  if (active && fields.includes(active.id)) return;
  $("#pf-goal").value = profile.goal || "";
  $("#pf-days").value = profile.days_per_week == null ? "" : profile.days_per_week;
  $("#pf-equipment").value = profile.equipment || "";
  $("#pf-injuries").value = profile.injuries || "";
}

async function saveWeight() {
  const raw = $("#fx-weight-input").value;
  const value = Number(raw);
  if (raw === "" || !(value >= 20 && value <= 300)) {
    toast("体重应在 20–300 kg", 5000);
    return;
  }
  try {
    await api("/api/fitness/record", { kind: "weight", weight_kg: value });
    $("#fx-weight-input").value = "";
    toast("已记录体重");
    await loadState();
  } catch (e) {
    toast(String(e.message || e), 5000);
  }
}

async function saveProfile() {
  const goal = $("#pf-goal").value;
  const days = Number($("#pf-days").value);
  const equipment = $("#pf-equipment").value.trim();
  const injuries = $("#pf-injuries").value.trim();
  if (!goal) {
    toast("先选目标", 5000);
    return;
  }
  if (!(Number.isInteger(days) && days >= 1 && days <= 7)) {
    toast("每周可练填 1–7 的整数", 5000);
    return;
  }
  if (!equipment || !injuries) {
    toast("器械和伤病都要填；没有就写「无」", 5000);
    return;
  }
  try {
    await api("/api/fitness/record", {
      kind: "profile",
      goal,
      days_per_week: days,
      equipment,
      injuries,
    });
    profileDirty = false;
    toast("画像已保存");
    await loadState();
  } catch (e) {
    toast(String(e.message || e), 5000);
  }
}

function renderAdvice(advice) {
  const box = $("#fx-advice-result");
  box.textContent = "";
  if (!advice || !Array.isArray(advice.items)) {
    const empty = document.createElement("p");
    empty.className = "empty";
    empty.textContent = "还没有建议。点上面的按钮生成一次。";
    box.appendChild(empty);
    return;
  }
  const meta = document.createElement("p");
  meta.className = "fx-advice-meta";
  const when = advice.generated_at
    ? new Date(advice.generated_at).toLocaleString("zh-CN", {
        timeZone: "Asia/Shanghai",
        hour12: false,
      })
    : "";
  meta.textContent = `${when}${advice.llm_used ? " · 模型措辞" : " · 本地规则"}`;
  box.appendChild(meta);

  if (!advice.items.length) {
    const empty = document.createElement("p");
    empty.className = "empty";
    empty.textContent = "这次没有可执行的建议，原因见下。";
    box.appendChild(empty);
  }

  const list = document.createElement("div");
  list.className = "fx-advice-list";
  box.appendChild(list);

  const labels = { frequency: "频率", volume: "容量", stagnation: "停滞" };
  for (const item of advice.items) {
    const card = document.createElement("article");
    card.className = "fx-advice-item";
    const title = document.createElement("h3");
    title.textContent = labels[item.rule] || item.rule;
    card.appendChild(title);
    for (const [field, className] of [
      ["conclusion", "fx-advice-conclusion"],
      ["evidence", "fx-advice-evidence"],
      ["action", "fx-advice-action"],
    ]) {
      const line = document.createElement("p");
      line.className = className;
      line.textContent = field === "action" ? `→ ${item.action || ""}` : item[field] || "";
      card.appendChild(line);
    }
    list.appendChild(card);
  }

  if (Array.isArray(advice.insufficient) && advice.insufficient.length) {
    const list = document.createElement("ul");
    list.className = "fx-insufficient";
    for (const note of advice.insufficient) {
      const item = document.createElement("li");
      item.textContent = note;
      list.appendChild(item);
    }
    box.appendChild(list);
  }
}

async function generateAdvice() {
  const button = $("#fx-advice");
  const status = $("#fx-advice-status");
  button.disabled = true;
  status.textContent = "生成中…";
  try {
    const advice = await api("/api/fitness/advice", {});
    renderAdvice(advice);
    toast("建议已生成");
  } catch (e) {
    toast(String(e.message || e), 5000);
  } finally {
    button.disabled = false;
    status.textContent = "";
  }
}

async function submitCorrection() {
  const input = $("#fx-correction");
  const note = input.value.trim();
  if (!note) {
    toast("先写一句纠正");
    return;
  }
  const button = $("#fx-correct");
  button.disabled = true;
  try {
    await api("/api/fitness/correct", { note });
    input.value = "";
    toast("已记录，下次 growth run 固化");
    await loadState();
  } catch (e) {
    toast(String(e.message || e), 5000);
  } finally {
    button.disabled = false;
  }
}

function renderFitness(fitness) {
  const demo = $("#fx-demo-banner");
  demo.hidden = !fitness.has_demo;
  const pending = $("#fx-correction-pending");
  const count = Number(fitness.pending_corrections || 0);
  pending.textContent = count ? `待固化 ${count} 条` : "";
  fxCatalog = fitness.exercises || [];
  renderExerciseOptions();
  const date = $("#fx-date");
  if (!date.value) date.value = localToday();
  const time = $("#fx-time");
  if (!time.value) time.value = shanghaiNow().time;
  renderWeights(fitness.weights || []);
  renderProfile(fitness.profile || {});
  renderRecent(fitness.recent_workouts || []);
  renderAdvice(fitness.last_advice || null);
}

async function clearDemo() {
  if (!window.confirm("清除所有演示数据？真实记录不会动。")) return;
  try {
    const res = await api("/api/fitness/demo/clear", { confirm: true });
    toast(`已清除演示训练 ${res.workouts} 次、体重 ${res.weights} 条`);
  } catch (e) {
    toast(String(e.message || e), 5000);
  }
  await loadState();
}

async function loadState() {
  try {
    const res = await fetch("/api/state", { headers: { Accept: "application/json" } });
    if (res.status === 401) {
      document.body.innerHTML =
        "<main><h1>需要访问令牌</h1><p>请打开启动 hpa web 时打印的带 <code>?token=</code> 的完整地址。</p></main>";
      return;
    }
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const data = await res.json();
    renderStatus(data.status || {});
    renderDrafts(data.drafts || []);
    renderTodos(data.todos || []);
    renderCorrections(data.corrections || []);
    renderFitness(data.fitness || {});
    $("#updated").textContent = "更新于 " + new Date().toLocaleTimeString("zh-CN", { hour12: false });
  } catch (e) {
    $("#statusbar").textContent = "载入失败：" + String(e.message || e);
  }
}

function schedule() {
  clearTimeout(timer);
  timer = setTimeout(async () => {
    if (!editing && document.visibilityState === "visible") await loadState();
    schedule();
  }, 20000);
}

$("#refresh").addEventListener("click", async () => {
  if (profileDirty && !window.confirm("放弃未保存的画像修改？")) return;
  profileDirty = false;
  await loadState();
});
$("#pause").addEventListener("click", async () => {
  const next = !paused;
  if (next && !window.confirm("暂停后 daemon 不再拉新邮件、也不再自动起草；已排队的草稿仍可确认。确定暂停？")) return;
  try {
    const r = await api("/api/pause", { paused: next });
    toast(r.paused ? "已暂停自动处理" : "已恢复自动处理");
  } catch (e) {
    toast(String(e.message || e), 5000);
  }
  await loadState();
});
document.addEventListener("visibilitychange", () => {
  if (document.visibilityState === "visible") loadState();
});

$("#fx-add").addEventListener("click", addExercise);
$("#fx-quick-parse").addEventListener("click", parseQuick);
$("#fx-quick-text").addEventListener("keydown", (e) => {
  if (e.key === "Enter") parseQuick();
});
$("#fx-save").addEventListener("click", saveWorkout);
$("#fx-clear").addEventListener("click", clearWorkoutForm);
$("#fx-exercise").addEventListener("change", syncWeightInput);
$("#fx-weight-save").addEventListener("click", saveWeight);
$("#fx-advice").addEventListener("click", generateAdvice);
$("#fx-correct").addEventListener("click", submitCorrection);
$("#fx-demo-clear").addEventListener("click", clearDemo);
$("#fx-weight-input").addEventListener("keydown", (e) => {
  if (e.key === "Enter") saveWeight();
});
$("#pf-save").addEventListener("click", saveProfile);
document.querySelectorAll("#pf-goal, #pf-days, #pf-equipment, #pf-injuries").forEach((el) => {
  el.addEventListener("input", () => { profileDirty = true; });
  el.addEventListener("change", () => { profileDirty = true; });
});
$("#fx-pending").addEventListener("input", (e) => {
  const el = e.target;
  if (!el.dataset.field) return;
  const ex = fxPending[Number(el.dataset.ex)];
  const set = ex && ex.sets[Number(el.dataset.set)];
  const seg = set && set.segments[Number(el.dataset.seg)];
  if (!seg) return;
  if (el.dataset.field === "weight") seg.weight = el.value === "" ? null : Number(el.value);
  else seg.reps = el.value === "" ? 0 : Number(el.value);
});
$("#fx-pending").addEventListener("click", (e) => {
  const remove = e.target.closest("[data-remove]");
  if (!remove) return;
  fxPending.splice(Number(remove.dataset.remove), 1);
  renderPending();
});
$("#fitness-recent").addEventListener("click", (e) => {
  const btn = e.target.closest("[data-delete]");
  if (btn) deleteWorkout(Number(btn.dataset.delete));
});
document.querySelectorAll(".tab").forEach((btn) => {
  btn.addEventListener("click", () => setTab(btn.dataset.tab));
});
try { setTab(localStorage.getItem(TAB_KEY) || "mail", false); } catch (_) { setTab("mail", false); }

loadState();
schedule();
