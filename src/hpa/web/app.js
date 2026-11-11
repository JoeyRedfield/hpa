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
loadState();
schedule();
