// Shared UI: bug / form dialogs driven by the URL hash (so "back" always works),
// plus collapsible kanban bands.
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

  // AUTO_LOOP.md G8 shape for the real svn log message; the owner edits it before
  // pushing, so it is only a starting point that already carries the bug number.
  function pushMessage(b) {
    const a = b.analysis || {};
    const one = String(a.conclusion || b.fix_summary || b.title || "")
      .split("\n")[0].trim();
    return `fix #${b.zentao_id} ${one}`.slice(0, 200).trim();
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
        ${r.merged_revision ? `<br>合入 trunk：${esc(revLabel(r.merged_revision))}` : ""}
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

    if (b.status === "await_review") {
      html += `
        <h4>推入 SVN（正式进库，只有主人能做）</h4>
        <form method="post" action="/bug/${esc(b.id)}/svn-push" data-json="1">
          <textarea name="message" rows="3" style="width:100%">${esc(pushMessage(b))}</textarea>
          <div class="row-actions">
            <button class="btn" type="submit" name="mode" value="dry"
              data-ask="预检：核对镜像、草稿基线，并逐个比对 trunk 上这些文件是否已被别人改动。不写入任何东西。">预检（不写入）</button>
            <button class="btn btn-pass" type="submit" name="mode" value="push"
              data-ask="确认正式推入 SVN？上方文字会原样作为 svn ci 的提交说明进入 trunk；成功即记为已合入，失败会写异常备注并打回给 AI 继续修。">正式推入 SVN</button>
            <span class="hint">分支 <code>${esc(b.branch || "-")}</code> 的草稿将按文件落到稀疏工作副本再提交；trunk 已被他人改动的文件不会被覆盖。</span>
          </div>
        </form>`;
    }

    if (b.status === "merged") {
      html += `
        <form method="post" action="/bug/${b.id}/close" class="row-actions">
          <button class="btn" type="submit">标记已结案</button>
          <span class="hint">仅更新本地状态并在禅道留言，禅道结案状态由主人手动确认。</span>
        </form>`;
    }
    return html;
  }

  // ------------------------------------------------------------------
  // dialog: mounted through the URL hash so the browser back button returns
  // to the list, and so a dialog can be shared / reloaded by URL.
  // ------------------------------------------------------------------
  const dialog = document.getElementById("dialog");
  const dialogTitle = dialog.querySelector("#dialog-title");
  const dialogBody = dialog.querySelector(".dialog-body");
  const initialHash = location.hash;
  let currentBug = null;

  function mount(title) {
    dialogTitle.textContent = title;
    dialogBody.innerHTML = "";
    dialog.hidden = false;
    document.body.classList.add("dialog-open");
    return dialogBody;
  }

  function hideDialog() {
    dialog.hidden = true;
    dialogBody.innerHTML = "";
    currentBug = null;
    document.body.classList.remove("dialog-open");
  }

  function closeDialog() {
    // If the hash came from this page, going back lands on the list again;
    // if the page was opened straight on a hash, rewriting it avoids leaving the site.
    if (location.hash && location.hash !== initialHash) {
      history.back();
      return;
    }
    if (location.hash) {
      history.replaceState(null, "", location.pathname + location.search);
    }
    hideDialog();
  }

  function openAt(hash) {
    if (location.hash === hash) {
      route();
      return;
    }
    location.hash = hash;
  }

  function loadBug(bugId, note, bad) {
    currentBug = bugId;
    mount("Bug 详情");
    dialogBody.innerHTML = '<p class="muted">加载中…</p>';
    return fetch(`/api/bug/${bugId}`)
      .then(resp => resp.json())
      .then(function (data) {
        if (currentBug !== bugId) return;
        const title = `禅道 #${data.zentao_id || "-"} · ${data.title || "(无标题)"}`;
        const color = bad ? "#b3261e" : "#188038";
        const tip = note ? `<p class="hint" style="color:${color};white-space:pre-wrap">${esc(note)}</p>` : "";
        mount(title);
        dialogBody.innerHTML = `<div class="bugdoc">${tip}${renderBug(data)}</div>`;
      })
      .catch(function (err) {
        mount("加载失败");
        dialogBody.innerHTML = `<p class="muted">${esc(err.message)}</p>`;
      });
  }

  // Bind-form data is rendered as JSON on the repos page; parsed once on demand.
  let repoForms = null;

  function formsData() {
    if (repoForms) return repoForms;
    const tag = document.getElementById("repo-forms");
    if (!tag) return null;
    try {
      repoForms = JSON.parse(tag.textContent);
    } catch (err) {
      return null;
    }
    return repoForms;
  }

  function showRepo(key) {
    const forms = formsData();
    if (!forms) return;
    const data = forms[key] || forms.new;
    const tpl = document.getElementById("repo-form-tpl");
    const label = String(data.product_id || "");
    const body = mount(label ? `修改绑定 · 产品 ${label}` : "新增绑定");
    body.appendChild(tpl.content.cloneNode(true));
    const form = body.querySelector("form");
    Object.keys(data).forEach(function (name) {
      const input = form.elements[name];
      if (!input) return;
      if (input.type === "checkbox") input.checked = Number(data[name]) === 1;
      else input.value = data[name] === null || data[name] === undefined ? "" : String(data[name]);
    });
    // The product id is the primary key of the binding: it must not drift while editing.
    const idField = form.elements.product_id;
    if (idField && label) idField.readOnly = true;
  }

  function route() {
    const bug = /^#\/bug\/(\d+)$/.exec(location.hash || "");
    if (bug) {
      loadBug(Number(bug[1]));
      return;
    }
    const repo = /^#\/repo\/(.+)$/.exec(location.hash || "");
    if (repo && formsData()) {
      showRepo(decodeURIComponent(repo[1]));
      return;
    }
    hideDialog();
  }

  window.addEventListener("hashchange", route);

  // ------------------------------------------------------------------
  // one-click copy: the dispatch page hands prompts to another window,
  // so nobody has to retype (or worse, re-transcribe by hand) a rules block.
  // ------------------------------------------------------------------
  function flashCopied(button) {
    const label = button.textContent;
    button.textContent = "已复制";
    button.disabled = true;
    window.setTimeout(function () {
      button.textContent = label;
      button.disabled = false;
    }, 1600);
  }

  function copyViaTextArea(text) {
    const box = document.createElement("textarea");
    box.value = text;
    box.setAttribute("readonly", "");
    box.style.position = "fixed";
    box.style.top = "-2000px";
    document.body.appendChild(box);
    box.select();
    try {
      document.execCommand("copy");
    } catch (err) {
      window.alert("浏览器拒绝了复制操作，请手动选中这段文字。");
    }
    document.body.removeChild(box);
  }

  function copyFrom(button) {
    const src = document.querySelector('[data-copy-src="' + button.dataset.copy + '"]');
    if (!src) return;
    const text = src.textContent;
    const done = function () { flashCopied(button); };
    if (navigator.clipboard && window.isSecureContext) {
      navigator.clipboard.writeText(text).then(done, function () {
        copyViaTextArea(text);
        done();
      });
    } else {
      copyViaTextArea(text);
      done();
    }
  }

  document.addEventListener("click", function (event) {
    if (!event.target || !event.target.closest) return;
    const copy = event.target.closest("[data-copy]");
    if (copy) {
      event.preventDefault();
      copyFrom(copy);
      return;
    }
    if (event.target.closest("[data-dialog-close]")) {
      event.preventDefault();
      closeDialog();
      return;
    }
    const card = event.target.closest(".card[data-bug-id]");
    if (!card) return;
    if (event.target.closest("a") || event.target.closest("button") || event.target.closest("form")) return;
    event.preventDefault();
    openAt(`#/bug/${card.dataset.bugId}`);
  });

  document.addEventListener("keydown", function (event) {
    if (event.key === "Escape" && !dialog.hidden) {
      event.preventDefault();
      closeDialog();
    }
  });

  // ------------------------------------------------------------------
  // forms: destructive / comment-writing actions ask first; forms inside the
  // dialog are posted without a page reload so the dialog stays open.
  // ------------------------------------------------------------------
  // Which submit button was pressed decides both the warning text and whether
  // the answer is rendered inside the dialog (SVN push) or as a page reload.
  let lastSubmitter = null;
  document.addEventListener("click", function (event) {
    const btn = event.target && event.target.closest
      ? event.target.closest("button[type=submit], input[type=submit]")
      : null;
    lastSubmitter = btn;
  }, true);

  function noteOf(data) {
    const summary = String(data.summary || "");
    const detail = String(data.detail || "");
    const text = detail && detail !== summary ? `${summary}\n${detail}` : summary;
    return text.slice(0, 900);
  }

  document.addEventListener("submit", function (event) {
    const form = event.target;
    const pressed = event.submitter || lastSubmitter;
    const tip = (pressed && pressed.dataset && pressed.dataset.ask) || form.dataset.confirm;
    lastSubmitter = null;
    if (tip && !window.confirm(tip)) {
      event.preventDefault();
      return;
    }
    if (!form.closest(".dialog-body")) return;
    event.preventDefault();
    const bugId = currentBug;
    const json = form.dataset.json === "1";
    mount("处理中…");
    dialogBody.innerHTML = json
      ? '<p class="muted">正在核对镜像与 SVN trunk 并按需提交，请稍候（可能要几十秒）…</p>'
      : '<p class="muted">正在执行，请稍候…</p>';
    const headers = json ? { "X-Requested-With": "fetch-dialog" } : {};
    fetch(form.action, { method: "post", body: new FormData(form), headers: headers })
      .then(function (resp) {
        if (json) {
          return resp.json().then(function (data) {
            if (bugId) loadBug(bugId, noteOf(data || {}), !data || !data.ok);
          });
        }
        if (form.dataset.reload) {
          // Follow the server's own target: the bind route points back at the
          // dialog only when the save was rejected.
          location.href = resp.url || location.pathname;
          return undefined;
        }
        if (bugId) {
          loadBug(bugId, "操作已提交，上方内容已刷新。");
          return undefined;
        }
        location.reload();
        return undefined;
      })
      .catch(function (err) {
        if (bugId) loadBug(bugId, `请求失败：${err.message}`, true);
        else location.reload();
      });
  });

  // ------------------------------------------------------------------
  // kanban bands: collapsed state is remembered per browser.
  // ------------------------------------------------------------------
  document.querySelectorAll("details.band[data-band]").forEach(function (band) {
    const key = `band:${band.dataset.band}`;
    const saved = window.localStorage ? window.localStorage.getItem(key) : null;
    if (saved !== null) band.open = saved === "1";
    band.addEventListener("toggle", function () {
      if (window.localStorage) window.localStorage.setItem(key, band.open ? "1" : "0");
    });
  });

  route();
})();
