// Expandable bug cards on the kanban board.
(function () {
  const LABELS = {
    pending: "待处理", fixing: "修复中", need_solution: "需方案",
    await_review: "待审查", rejected: "已打回", merged: "已合入", closed: "已结案"
  };

  function esc(value) {
    if (value === null || value === undefined) return "";
    return String(value)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  const IMAGE_EXTS = ["png", "jpg", "jpeg", "gif", "bmp", "webp"];

  function isImage(ext) {
    return IMAGE_EXTS.indexOf(String(ext || "").toLowerCase()) >= 0;
  }

  const ANALYSIS_ROWS = [
    ["一句话结论", "conclusion"], ["现象", "symptom"], ["根因", "root_cause"],
    ["定位依据", "evidence"], ["涉及链路", "call_chain"], ["改动内容", "change_desc"],
    ["影响面", "impact"], ["验证方式与结果", "verify"], ["未验证项", "unverified"],
    ["回退方式", "rollback"]
  ];
  const KIND_LABELS = { commit: "修复完成时", block: "卡点时", manual: "人工补记" };

  function renderAnalysis(a) {
    if (!a) return "";
    const rows = ANALYSIS_ROWS
      .filter(([ , key]) => a[key])
      .map(([label, key]) => {
        const cls = key === "unverified" ? ' style="color:#b3261e"' : "";
        return `<tr><th>${label}</th><td${cls}>${esc(a[key])}</td></tr>`;
      }).join("");
    let gates = "";
    const g = a.gates_parsed || {};
    const keys = Object.keys(g).sort();
    if (keys.length) {
      gates = `<tr><th>提交闸门</th><td>${keys.map(k => {
        const v = String(g[k]);
        const ok = v.startsWith("pass") || v.startsWith("通过");
        const warn = v.startsWith("未验证") || v.startsWith("warn");
        const color = ok ? "#188038" : (warn ? "#b06000" : "#b3261e");
        return `<span class="badge" style="background:${color};color:#fff">${esc(k)}: ${esc(v)}</span>`;
      }).join(" ")}</td></tr>`;
    }
    return `<h4>分析结论（${esc(KIND_LABELS[a.kind] || a.kind)} · ${esc(a.author || "ai")} · ${esc(a.created_at)}）</h4>
      <table class="grid">${rows}${gates}</table>`;
  }

  // SVN numbers are shown with the r prefix; git:/PENDING/DRYRUN- stay verbatim.
  function revLabel(value) {
    const text = String(value == null ? "" : value);
    return /^\d+$/.test(text) ? "r" + text : text;
  }

  function renderBug(b) {
    const revs = (b.revisions || []).map(r =>
      `<li><b>${esc(revLabel(r.revision))}</b> · ${esc(r.created_at)} · ${esc(r.branch || b.branch || "")}<br>${esc(r.message)}</li>`
    ).join("");
    const needs = (b.needs || []).map(n => `
      <li><b>[${esc(LABELS[n.status] || n.status)}]</b> ${esc(n.question)}
        ${n.ai_options ? `<br>可选方案：${esc(n.ai_options)}` : ""}
        ${n.ai_advice ? `<br>AI 建议：${esc(n.ai_advice)}` : ""}
        ${n.owner_reply ? `<br><span style="color:#188038">主人答复：${esc(n.owner_reply)}</span>` : ""}
      </li>`).join("");
    const reviews = (b.reviews || []).map(r => `
      <li><b>[${r.result === "pass" ? "通过" : "打回"}]</b> ${esc(r.created_at)}
        ${r.reject_reason ? `<br>打回原因：${esc(r.reject_reason)}` : ""}
        ${r.merged_revision ? `<br>合入 trunk：r${esc(r.merged_revision)}` : ""}
      </li>`).join("");
    const files = (b.files_changed || []).map(f => `<li class="mono">${esc(f)}</li>`).join("");

    // Notes ("备注") and screenshots only exist after a detail refresh.
    const notes = (b.comments || []).map(c => `
      <li><b>${esc(c.actor || "-")}</b> · ${esc(c.action || "")} · ${esc(c.date || "")}
        ${c.comment ? `<br>${esc(c.comment)}` : ""}
        ${c.changed && c.changed.length ? `<br><span class="muted">变更：${esc(c.changed.join("；"))}</span>` : ""}
      </li>`).join("");
    const pics = (b.images || []).map(a =>
      `<a href="${esc(a.web_path)}" target="_blank" title="${esc(a.name || a.filename)}">
         <img class="thumb" src="${esc(a.web_path)}" alt="${esc(a.name || a.filename)}"></a>`).join("");
    const docs = (b.docs || []).filter(a => !isImage(a.ext)).map(a =>
      `<li>${a.web_path ? `<a href="${esc(a.web_path)}" target="_blank">${esc(a.name || a.filename)}</a>`
                        : esc(a.name || a.filename)}
       <span class="muted">${a.size ? (a.size / 1024).toFixed(0) + "KB" : ""}</span></li>`).join("");

    let html = `
      <h4>缺陷描述 / 复现步骤</h4>
      <pre>${esc(b.steps || "(禅道未提供描述，或尚未同步详情)")}</pre>`;

    if (!b.detail_synced_at) {
      html += `
        <form method="post" action="/bug/${b.id}/detail" class="row-actions"
              data-confirm="将从禅道拉取该 bug 的完整描述、截图和备注，确认？">
          <button class="btn btn-primary" type="submit">拉取截图 / 备注 / 附件</button>
          <span class="hint">列表同步只带回了文字描述，截图与备注需要按条拉取。</span>
        </form>`;
    } else {
      html += `<p class="hint">详情更新于 ${esc(b.detail_synced_at)}；
        <form method="post" action="/bug/${b.id}/detail" style="display:inline">
          <button class="btn" type="submit">重新拉取</button>
        </form></p>`;
    }
    if (pics) html += `<h4>截图（${(b.images || []).length} 张）</h4><div class="gallery">${pics}</div>`;
    if (docs) html += `<h4>附件</h4><ul class="tight">${docs}</ul>`;
    if (notes) html += `<h4>禅道备注 / 操作记录</h4><ul class="tight">${notes}</ul>`;

    html += `
      <h4>基本信息</h4>
      <pre>模块：${esc(b.module || "-")}    产品：${esc(b.product_name || "-")}    指派给：${esc(b.assigned_to_name || b.assigned_to || "-")}    提交人：${esc(b.opened_by || "-")}    迭代：${esc(b.opened_build || "-")}
禅道状态：${esc(b.zentao_status || "-")}    分支：${esc(b.branch || "尚未建分支")}    最近同步：${esc(b.synced_at || "-")}</pre>`;

    if (b.fix_summary || b.verify_steps) {
      html += `<h4>修复说明 / 验证步骤</h4><pre>${esc(b.fix_summary || "")}\n\n验证：${esc(b.verify_steps || "")}</pre>`;
    }
    // The executor's written analysis (root cause / evidence / gates) is the main
    // thing a human reads before approving, so it goes right above the diff list.
    html += renderAnalysis(b.analysis);
    if (!(b.analyses || []).length && ["await_review", "merged", "closed"].indexOf(b.status) >= 0) {
      html += `<h4>分析结论</h4><p class="hint" style="color:#b3261e">（缺失：要求执行器补
        <code>python -m app.cli analyze ${esc(b.zentao_id)} --analysis-file a.json</code>）</p>`;
    }
    if (files) html += `<h4>修改文件</h4><ul class="tight">${files}</ul>`;
    html += `<h4>SVN 提交记录</h4>` + (revs ? `<ul class="tight">${revs}</ul>` : `<p class="muted">无</p>`);
    html += `<h4>需方案记录</h4>` + (needs ? `<ul class="tight">${needs}</ul>` : `<p class="muted">无</p>`);
    html += `<h4>审查记录</h4>` + (reviews ? `<ul class="tight">${reviews}</ul>` : `<p class="muted">无</p>`);

    if (b.status === "merged") {
      html += `
        <form method="post" action="/bug/${b.id}/close" class="row-actions">
          <button class="btn" type="submit">标记已结案</button>
          <span class="hint">仅更新本地状态并在禅道留言，禅道结案状态由主人手动确认。</span>
        </form>`;
    }
    return html;
  }

  document.addEventListener("click", async function (event) {
    const card = event.target.closest(".card[data-bug-id]");
    if (!card) return;
    if (event.target.closest("button") || event.target.closest("form")) return;
    const box = card.querySelector(".detail");
    box.classList.toggle("open");
    if (!box.classList.contains("open") || box.dataset.loaded === "1") return;
    box.innerHTML = '<p class="muted">加载中…</p>';
    try {
      const resp = await fetch(`/api/bug/${card.dataset.bugId}`);
      const data = await resp.json();
      box.innerHTML = renderBug(data);
      box.dataset.loaded = "1";
    } catch (err) {
      box.innerHTML = `<p class="muted">加载失败：${esc(err.message)}</p>`;
    }
  });
})();

// Destructive / comment-writing actions ask for confirmation.
document.addEventListener("submit", function (event) {
  const form = event.target;
  const tip = form.dataset.confirm;
  if (tip && !window.confirm(tip)) event.preventDefault();
});
