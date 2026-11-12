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
      const volume = ex.sets.reduce(
        (sum, set) => sum + (ex.bodyweight
          ? Number(set.reps || 0)
          : Number(set.weight || 0) * Number(set.reps || 0)),
        0,
      );
      const rows = ex.sets
        .map((set, j) => {
          const weightInput = ex.bodyweight
            ? ""
            : `<input type="number" data-ex="${i}" data-set="${j}" data-field="weight" value="${set.weight ?? ""}" min="0" max="500" step="0.5" inputmode="decimal" aria-label="第 ${j + 1} 组重量"><span class="fx-unit">kg</span>`;
          return `<div class="fx-set">
            <span class="fx-set-no">${j + 1}</span>
            ${weightInput}
            ${ex.bodyweight ? "" : '<span class="fx-x">×</span>'}
            <input type="number" data-ex="${i}" data-set="${j}" data-field="reps" value="${set.reps}" min="1" max="100" inputmode="numeric" aria-label="第 ${j + 1} 组次数">
            <span class="muted">次</span>
          </div>`;
        })
        .join("");
      return `<article class="fx-item">
        <div class="fx-item-head">
          <strong>${esc(ex.name)}</strong>
          <span class="fx-summary muted">${ex.sets.length} 组 · ${fmtVolume(volume, kind)}</span>
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
  fxPending.push({
    exercise: ex.id,
    name: ex.name,
    bodyweight: ex.bodyweight,
    sets: Array.from({ length: sets }, () => ({ weight: ex.bodyweight ? null : weight, reps })),
  });
  renderPending();
  $("#fx-reps").value = "";
  $("#fx-weight").value = "";
}

function clearWorkoutForm() {
  fxPending = [];
  renderPending();
  $("#fx-note").value = "";
  $("#fx-time").value = shanghaiNow().time;
}

async function saveWorkout() {
  if (!fxPending.length) { toast("先添加至少一个动作"); return; }
  for (const ex of fxPending) {
    for (const set of ex.sets) {
      const reps = Number(set.reps);
      if (!Number.isInteger(reps) || reps < 1 || reps > 100) {
        toast(`${ex.name} 的次数应在 1–100`);
        return;
      }
      if (!ex.bodyweight) {
        const weight = Number(set.weight);
        if (set.weight === null || set.weight === "" || !Number.isFinite(weight) || weight < 0 || weight > 500) {
          toast(`${ex.name} 的重量应在 0–500 kg`);
          return;
        }
      }
    }
  }
  const exercises = fxPending.map((ex) => ({
    exercise: ex.exercise,
    sets: ex.sets.map((set) => (ex.bodyweight
      ? { reps: Number(set.reps) }
      : { weight_kg: Number(set.weight), reps: Number(set.reps) })),
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
        .map((ex) => `<div class="fx-recent-ex">
          <strong>${esc(ex.name)}</strong>
          <span class="muted">${ex.sets.length} 组 · ${fmtVolume(ex.volume, ex.volume_kind)}</span>
          <div class="fx-recent-sets">${ex.sets.map((set) => fmtSet(set, ex.volume_kind === "reps")).map(esc).join(" · ")}</div>
        </div>`)
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

function renderFitness(fitness) {
  fxCatalog = fitness.exercises || [];
  renderExerciseOptions();
  const date = $("#fx-date");
  if (!date.value) date.value = localToday();
  const time = $("#fx-time");
  if (!time.value) time.value = shanghaiNow().time;
  renderRecent(fitness.recent_workouts || []);
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

$("#refresh").addEventListener("click", loadState);
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
$("#fx-save").addEventListener("click", saveWorkout);
$("#fx-clear").addEventListener("click", clearWorkoutForm);
$("#fx-exercise").addEventListener("change", syncWeightInput);
$("#fx-pending").addEventListener("input", (e) => {
  const el = e.target;
  if (!el.dataset.field) return;
  const ex = fxPending[Number(el.dataset.ex)];
  const set = ex && ex.sets[Number(el.dataset.set)];
  if (!set) return;
  if (el.dataset.field === "weight") set.weight = el.value === "" ? null : Number(el.value);
  else set.reps = el.value === "" ? 0 : Number(el.value);
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
