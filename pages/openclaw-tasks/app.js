let refreshTimer = null;

function escapeHtml(value) {
  return String(value ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

function setText(id, value) {
  const el = document.getElementById(id);
  if (el) el.textContent = value;
}

function showError(message) {
  const old = document.getElementById("page-error");
  if (old) old.remove();
  document.body.insertAdjacentHTML(
    "afterbegin",
    `<div id="page-error" class="empty">加载失败：${escapeHtml(message)}</div>`
  );
}

function showNotice(message) {
  const old = document.getElementById("page-notice");
  if (old) old.remove();
  document.body.insertAdjacentHTML(
    "afterbegin",
    `<div id="page-notice" class="notice">${escapeHtml(message)}</div>`
  );
}

function fmtTime(ts) {
  if (!ts) return "-";
  const d = new Date(ts * 1000);
  return d.toLocaleString("zh-CN", { hour12: false });
}

function fmtDuration(start, end) {
  if (!start || !end) return "-";
  const s = Math.round(end - start);
  if (s < 60) return s + "s";
  if (s < 3600) return Math.floor(s / 60) + "m" + (s % 60) + "s";
  return Math.floor(s / 3600) + "h" + Math.floor((s % 3600) / 60) + "m";
}

function badge(status) {
  const valid = ["running", "done", "failed", "cancelled", "no_recipient"];
  const cls = valid.includes(status) ? status : "failed";
  return `<span class="badge ${cls}">${escapeHtml(status || "unknown")}</span>`;
}

function actionsCell(t) {
  const taskId = escapeHtml(t.task_id || "");
  const label = t.status === "running" ? "取消并删除" : "删除";
  return `<td><button class="danger" data-action="delete" data-task-id="${taskId}" data-label="${label}">${label}</button></td>`;
}

function renderRow(t, includeStatus) {
  const project = escapeHtml(t.project || "");
  const taskText = escapeHtml(t.task_text || "");
  const mode = escapeHtml(t.mode || "");
  const taskId = escapeHtml(t.task_id || "");
  const idCell = `<td class="id">${taskId}</td>`;
  if (includeStatus) {
    return `<tr>
      <td>${project}</td>
      <td class="task" title="${taskText}">${taskText}</td>
      <td>${badge(t.status)}</td>
      <td>${mode}</td>
      <td>${fmtTime(t.created_at)}</td>
      <td>${fmtTime(t.finished_at)}</td>
      <td>${fmtDuration(t.created_at, t.finished_at)}</td>
      ${idCell}
      ${actionsCell(t)}
    </tr>`;
  } else {
    return `<tr>
      <td>${project}</td>
      <td class="task" title="${taskText}">${taskText}</td>
      <td>${mode}</td>
      <td>${fmtTime(t.created_at)}</td>
      ${idCell}
      ${actionsCell(t)}
    </tr>`;
  }
}

async function deleteTask(taskId, btn) {
  if (!taskId) return;
  if (btn) {
    btn.disabled = true;
    btn.textContent = "删除中...";
  }
  try {
    const bridge = window.AstrBotPluginPage;
    if (!bridge) throw new Error("AstrBotPluginPage bridge 未加载");
    const data = await bridge.apiPost("delete", { task_id: taskId });
    if (!data || !data.ok) throw new Error(data?.error || "删除失败");
    showNotice(`已删除任务 ${taskId}`);
    await refresh();
  } catch (e) {
    showError(e?.message || e || "删除失败");
    if (btn) {
      btn.disabled = false;
      btn.dataset.confirmDelete = "";
      btn.textContent = btn.dataset.label || "删除";
    }
  }
}

window.handleDeleteClick = function handleDeleteClick(btn) {
  if (!btn || btn.disabled) return;
  const now = Date.now();
  const lastActivation = Number(btn.dataset.lastActivation || 0);
  if (now - lastActivation < 300) return;
  btn.dataset.lastActivation = String(now);
  const taskId = btn.dataset.taskId;
  if (btn.dataset.confirmDelete !== "yes") {
    btn.dataset.confirmDelete = "yes";
    btn.textContent = "确认删除";
    showNotice("再次点击确认删除任务");
    setTimeout(() => {
      if (btn.dataset.confirmDelete === "yes" && !btn.disabled) {
        btn.dataset.confirmDelete = "";
        btn.textContent = btn.dataset.label || "删除";
      }
    }, 5000);
    return;
  }
  deleteTask(taskId, btn);
};

function bindActionButtons() {
  document.querySelectorAll('button[data-action="delete"]').forEach((btn) => {
    btn.onpointerdown = (event) => {
      event.preventDefault();
      window.handleDeleteClick(btn);
    };
    btn.onclick = (event) => {
      event.preventDefault();
      window.handleDeleteClick(btn);
    };
  });
}

async function refresh() {
  let data;
  try {
    const bridge = window.AstrBotPluginPage;
    if (!bridge) throw new Error("AstrBotPluginPage bridge 未加载");
    data = await bridge.apiGet("tasks");
  } catch (e) {
    showError(e?.message || e || "Plugin Page API 调用失败");
    return;
  }
  if (!data || !data.ok) {
    showError(data?.error || "Plugin Page API 返回异常");
    return;
  }
  const oldError = document.getElementById("page-error");
  if (oldError) oldError.remove();
  const tasks = data.tasks || [];
  const running = tasks.filter(t => t.status === "running");
  const history = tasks.filter(t => t.status !== "running");

  setText("cnt-running", data.running_count ?? running.length);
  setText("cnt-done", history.filter(t => t.status === "done").length);
  setText("cnt-failed", history.filter(t => t.status === "failed").length);
  setText("cnt-no-recipient", history.filter(t => t.status === "no_recipient").length);
  setText("cnt-total", data.total_count ?? tasks.length);

  const tbR = document.querySelector("#tbl-running tbody");
  if (tbR) {
    tbR.innerHTML = running.length === 0
      ? `<tr><td colspan="6" class="empty">无运行中任务</td></tr>`
      : running.map(t => renderRow(t, false)).join("");
  }

  const tbH = document.querySelector("#tbl-history tbody");
  if (tbH) {
    tbH.innerHTML = history.length === 0
      ? `<tr><td colspan="9" class="empty">无历史任务</td></tr>`
      : history.map(t => renderRow(t, true)).join("");
  }
  bindActionButtons();
}

// 用 IIFE 包 await——script 不是 module，顶层 await 会报 SyntaxError
(async () => {
  try {
    const bridge = window.AstrBotPluginPage;
    if (!bridge) throw new Error("AstrBotPluginPage bridge 未加载");
    await bridge.ready();
    const refreshBtn = document.getElementById("refresh");
    if (refreshBtn) refreshBtn.addEventListener("click", refresh);
    await refresh();
    const startTimer = () => {
      if (!refreshTimer) refreshTimer = setInterval(refresh, 5000);
    };
    const stopTimer = () => {
      if (refreshTimer) clearInterval(refreshTimer);
      refreshTimer = null;
    };
    document.addEventListener("visibilitychange", () => {
      if (document.hidden) stopTimer();
      else {
        refresh();
        startTimer();
      }
    });
    startTimer();
  } catch (e) {
    showError(e?.message || e || "Plugin Page 初始化失败");
  }
})();
