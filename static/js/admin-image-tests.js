(() => {
  "use strict";

  const UI = window.ImageGen;
  const escape = UI.escapeHtml;
  const apiRoot = "/api/admin/image-tests";
  const statuses = {
    queued: "排队中", running: "生成中", reconnecting: "正在重连", canceling: "取消中",
    succeeded: "生成成功", failed: "生成失败", interrupted: "已中断", canceled: "已取消",
  };
  const qualities = { auto: "自动", low: "低", medium: "中", high: "高" };
  const verdicts = { "": "未标记", pass: "通过", fail: "不通过", review: "待复核" };
  const option = (value, label) => `<option value="${escape(value)}">${escape(label)}</option>`;
  const rmb = (amount) => `¥${Number(amount).toFixed(4)}`;

  class AdminImageTestSuites {
    constructor(app) {
      this.app = app;
      this.el = app.el;
      this.editor = null;
      this.copyable = false;
      this.pending = false;
      this.bind();
    }

    bind() {
      const el = this.el;
      el.Suite.addEventListener("change", () => {
        this.previewSuite();
        this.app.updateEstimate();
      });
      el.EditSuite.addEventListener("click", () => this.app.showPage("suites", this.selectedSuite()));
      el.EditSelect.addEventListener("change", () => this.editSuite(this.selectedSuite(el.EditSelect)));
      el.NewSuite.addEventListener("click", () => {
        this.editSuite();
        el.SuiteForm.elements.name.focus();
      });
      el.CopySuite.addEventListener("click", () => this.editSuite({
        name: el.SuiteForm.elements.name.value,
        questions: this.editorQuestions(),
      }, true));
      el.DeleteSuite.addEventListener("click", () => this.deleteSuite());
      el.AddQuestion.addEventListener("click", () => this.addQuestion({}, true));
      el.EditorQuestions.addEventListener("click", (event) => this.editQuestionOrder(event));
      el.SuiteForm.addEventListener("submit", (event) => this.saveSuite(event));
    }

    setPending(pending) {
      this.pending = pending;
      this.el.EditSelect.disabled = pending;
      this.el.NewSuite.disabled = pending;
      this.el.CopySuite.disabled = pending || !this.copyable;
      this.el.DeleteSuite.disabled = pending || !this.editor?.id || this.editor.builtin;
      [...this.el.SuiteForm.elements].forEach((field) => { field.disabled = pending; });
      this.updateQuestionOrder();
    }

    loadCatalog(catalog) {
      this.catalog = catalog;
      this.renderSuites();
      if (!this.editor) this.editSuite(this.selectedSuite());
    }

    renderSuites(selected = this.el.Suite.value) {
      const suites = this.catalog.suites;
      const options = suites.map((suite) => option(suite.id, suite.name)).join("");
      this.el.Suite.innerHTML = options;
      this.el.Suite.value = suites.some((suite) => suite.id === selected) ? selected : "default";
      this.el.Suite.disabled = false;
      this.el.EditSelect.innerHTML = option("", "新题组") + options;
      this.el.EditSelect.value = this.editor?.id || "";
      this.previewSuite();
    }

    selectedSuite(select = this.el.Suite) {
      return this.catalog?.suites.find((suite) => suite.id === select.value);
    }

    previewSuite() {
      const suite = this.selectedSuite();
      this.el.SuitePreview.innerHTML = suite ? `
        <ul>${suite.questions.map((question) => `<li><span>${escape(question.title)}</span>
          <small>${escape(question.size === "auto" ? "自动尺寸" : question.size)} · ${qualities[question.quality]}</small></li>`).join("")}</ul>
      ` : "";
      this.el.EditSuite.disabled = !suite;
    }

    editSuite(suite, copy = false) {
      if (!this.catalog) return;
      this.copyable = !!suite;
      this.editor = {
        id: copy ? "" : suite?.id || "",
        revision: suite?.revision,
        builtin: !copy && suite?.builtin,
      };
      this.el.EditSelect.value = this.editor.id;
      this.el.SuiteForm.elements.name.value = copy ? `${suite.name.slice(0, 75)} · 副本` : suite?.name || "";
      this.el.BuiltinHint.hidden = !this.editor.builtin;
      this.el.CopySuite.disabled = !suite;
      this.el.DeleteSuite.disabled = !this.editor.id || !!this.editor.builtin;
      this.el.EditorQuestions.replaceChildren();
      (suite?.questions || [{}]).forEach((question) => this.addQuestion(question));
    }

    editorQuestions() {
      return [...this.el.EditorQuestions.querySelectorAll(".image-test-question")].map((row) =>
        Object.fromEntries([...row.querySelectorAll("[name]")].map((field) => [field.name, field.value])),
      );
    }

    addQuestion(question, focus = false) {
      const list = this.el.EditorQuestions;
      if (list.children.length >= this.catalog.max_questions) return;
      list.insertAdjacentHTML("beforeend", `<fieldset class="image-test-question">
        <legend></legend>
        <div class="image-test-actions image-test-order">
          <button class="icon-button" type="button" data-question-action="up" aria-label="上移题目" title="上移题目"><i data-lucide="arrow-up"></i></button>
          <button class="icon-button" type="button" data-question-action="down" aria-label="下移题目" title="下移题目"><i data-lucide="arrow-down"></i></button>
          <button class="icon-button danger" type="button" data-question-action="remove" aria-label="删除题目" title="删除题目"><i data-lucide="trash-2"></i></button>
        </div>
        <div class="image-test-question-fields">
          <label class="field"><span>题目名称</span><input name="title" maxlength="80" required value="${escape(question.title)}"></label>
          <label class="field"><span>尺寸</span><input name="size" maxlength="20" placeholder="auto 或 1024x1024" value="${escape(question.size || "auto")}"></label>
          <label class="field"><span>质量</span><select name="quality">${Object.entries(qualities).map(([value, label]) => option(value, label)).join("")}</select></label>
        </div>
        <label class="field"><span>绘图提示词</span><textarea name="prompt" rows="3" maxlength="${this.catalog.max_prompt_characters}" required>${escape(question.prompt)}</textarea></label>
        <label class="field"><span>评审标准（选填）</span><textarea name="criteria" rows="2" maxlength="500">${escape(question.criteria)}</textarea></label>
      </fieldset>`);
      const row = list.lastElementChild;
      row.querySelector("[name=quality]").value = question.quality || "auto";
      this.updateQuestionOrder();
      UI.icons(row);
      if (focus) row.querySelector("[name=title]").focus();
    }

    updateQuestionOrder() {
      const rows = [...this.el.EditorQuestions.children];
      rows.forEach((row, index) => {
        row.querySelector("legend").textContent = `第 ${index + 1} 题`;
        row.querySelector("[data-question-action=up]").disabled = this.pending || index === 0;
        row.querySelector("[data-question-action=down]").disabled = this.pending || index === rows.length - 1;
        row.querySelector("[data-question-action=remove]").disabled = this.pending || rows.length === 1;
      });
      this.el.AddQuestion.disabled = this.pending || rows.length >= this.catalog.max_questions;
    }

    editQuestionOrder(event) {
      const button = event.target.closest("[data-question-action]");
      if (!button) return;
      const row = button.closest(".image-test-question");
      if (button.dataset.questionAction === "remove") row.remove();
      if (button.dataset.questionAction === "up") row.previousElementSibling?.before(row);
      if (button.dataset.questionAction === "down") row.nextElementSibling?.after(row);
      this.updateQuestionOrder();
    }

    async saveSuite(event) {
      event.preventDefault();
      if (this.pending) return;
      this.setPending(true);
      const button = event.submitter;
      this.app.setError("");
      const suiteId = this.editor.builtin ? "" : this.editor.id;
      try {
        const { suite } = await UI.api(`${apiRoot}/suites${suiteId ? `/${suiteId}` : ""}`, {
          method: suiteId ? "PUT" : "POST",
          body: {
            name: this.el.SuiteForm.elements.name.value,
            revision: this.editor.revision,
            questions: this.editorQuestions(),
          },
        });
        this.catalog.suites = [
          this.catalog.suites[0],
          suite,
          ...this.catalog.suites.slice(1).filter((item) => item.id !== suite.id),
        ];
        this.editSuite(suite);
        this.renderSuites(suite.id);
        this.app.updateEstimate();
        if (button?.value === "use") this.app.showPage("workbench");
        UI.toast("已保存题组", "success");
      } catch (error) {
        this.app.setError(error.message);
      } finally {
        this.setPending(false);
      }
    }

    async deleteSuite() {
      const { id, revision, builtin } = this.editor || {};
      if (!id || builtin || this.pending || !window.confirm("删除此题组？已生成的测评记录会保留。")) return;
      this.setPending(true);
      this.app.setError("");
      try {
        await UI.api(`${apiRoot}/suites/${id}`, { method: "DELETE", body: { revision } });
        this.catalog.suites = this.catalog.suites.filter((suite) => suite.id !== id);
        this.editSuite(this.catalog.suites[0]);
        this.renderSuites();
        this.app.updateEstimate();
        UI.toast("已删除题组", "success");
      } catch (error) {
        this.app.setError(error.message);
      } finally {
        this.setPending(false);
      }
    }
  }

  class AdminImageTestRuns {
    constructor(app) {
      this.app = app;
      this.el = app.el;
      this.currentId = "";
      this.historyId = "";
      this.offset = 0;
      this.detailVersion = 0;
      this.bind();
    }

    bind() {
      const el = this.el;
      el.HistoryRuns.addEventListener("click", (event) => {
        const batch = event.target.closest("[data-test-run]");
        if (batch) this.openHistoryRun(batch.dataset.testRun);
      });
      el.Previous.addEventListener("click", () => this.loadHistory(this.offset - this.history.limit));
      el.Next.addEventListener("click", () => this.loadHistory(this.offset + this.history.limit));
      el.CompareQuestion.addEventListener("change", () => this.renderComparison());
      el.Results.addEventListener("change", (event) => this.reviewResult(event));
      el.Cancel.addEventListener("click", () => {
        if (this.run?.active && window.confirm("取消批次中尚未完成的图片？")) {
          this.runAction(el.Cancel, `/runs/${this.run.id}/cancel`);
        }
      });
      el.Retry.addEventListener("click", () => {
        this.runAction(el.Retry, `/runs/${this.run.id}/retry`, {});
      });
    }

    async activate(page, refresh) {
      if (page === "history") {
        await this.loadHistory(this.offset);
      } else if (page === "workbench") {
        if (!this.currentId) {
          const { runs } = await UI.api(`${apiRoot}/runs`);
          if (!this.currentId) this.currentId = runs[0]?.id || "";
        }
        if (this.app.page === "workbench" && (refresh || this.run?.id !== this.currentId)) {
          await this.loadRun(this.currentId);
        }
      }
    }

    showPage(page) {
      const slot = page === "history" ? this.el.HistoryDetail : this.el.Current;
      slot.append(this.el.Run);
      if (page === "history") this.loadHistory(this.offset);
      else if (this.run?.id !== this.currentId) this.loadRun(this.currentId);
      else this.el.CurrentEmpty.hidden = !!this.run;
    }

    async loadHistory(offset) {
      const version = this.historyVersion = (this.historyVersion || 0) + 1;
      this.el.Previous.disabled = true;
      this.el.Next.disabled = true;
      try {
        const history = await UI.api(`${apiRoot}/runs?offset=${offset}`);
        if (this.app.page !== "history" || this.historyVersion !== version) return;
        this.history = history;
        this.offset = offset;
        const id = history.runs.some((run) => run.id === this.historyId) ? this.historyId : history.runs[0]?.id || "";
        this.el.HistoryTotal.textContent = `共 ${history.total} 批`;
        this.openHistoryRun(id);
      } catch (error) {
        this.app.setError(error.message);
      } finally {
        if (this.historyVersion === version && this.history) {
          this.el.Previous.disabled = !this.offset;
          this.el.Next.disabled = this.offset + this.history.limit >= this.history.total;
        }
      }
    }

    openHistoryRun(id) {
      this.historyId = id;
      this.renderHistory();
      this.loadRun(id);
    }

    renderHistory() {
      this.el.HistoryRuns.innerHTML = this.history.runs.map((entry) => {
        const run = this.run?.id === entry.id ? this.run : entry;
        return `<button class="image-test-history-entry" type="button" data-test-run="${run.id}"
          aria-pressed="${run.id === this.historyId}">
          <strong>${escape(run.suite_name)}${run.retry_of ? " · 重试" : ""}</strong>
          <small>${escape(UI.dateTime(run.created_at))} · ${run.finished}/${run.total} 张已处理</small>
        </button>`;
      }).join("") || '<p class="image-test-empty">暂无历史批次。</p>';
    }

    async loadRun(id, poll = false) {
      const version = ++this.detailVersion;
      const questions = poll ? this.run?.questions : null;
      if (this.run?.id !== id) this.showRun(null);
      if (!id) return;
      try {
        const { run } = await UI.api(`${apiRoot}/runs/${id}${questions ? "?progress=1" : ""}`);
        if (version === this.detailVersion && this.app.page !== "suites") {
          if (questions) run.questions = questions;
          this.showRun(run);
          this.updateHistoryProgress(run);
        }
      } catch (error) {
        if (version !== this.detailVersion) return;
        if (error.status === 404) this.showRun(null);
        if (!poll || error.status === 404) this.app.setError(error.message);
      }
    }

    async poll() {
      if (!this.run?.active || this.app.page === "suites" || this.polling || this.actionPending || document.hidden) return;
      this.polling = true;
      try {
        await this.loadRun(this.run.id, true);
      } finally {
        this.polling = false;
      }
    }

    async runAction(button, path, body) {
      if (this.actionPending) return;
      this.actionPending = true;
      button.disabled = true;
      ++this.detailVersion;
      this.app.setError("");
      try {
        if (body) {
          const signature = JSON.stringify([path, body]);
          if (this.pendingRequest?.signature !== signature) {
            const bytes = crypto.getRandomValues(new Uint8Array(16));
            const id = Array.from(bytes, (byte) => byte.toString(16).padStart(2, "0")).join("");
            this.pendingRequest = { signature, id };
          }
          body = { ...body, request_id: this.pendingRequest.id };
        }
        const { run } = await UI.api(apiRoot + path, { method: "POST", body });
        if (body) {
          this.pendingRequest = null;
          this.currentId = run.id;
          ++this.detailVersion;
          this.showRun(run);
          this.app.showPage("workbench");
          this.el.Run.scrollIntoView({ block: "start" });
        } else if (this.run?.id === run.id) {
          ++this.detailVersion;
          this.showRun(run);
        }
        this.updateHistoryProgress(run);
      } catch (error) {
        this.app.setError(error.message);
      } finally {
        this.actionPending = false;
        button.disabled = false;
        this.app.updateEstimate();
      }
    }

    updateHistoryProgress(run) {
      if (this.app.page !== "history" || !this.history) return;
      const entry = this.history.runs.find((item) => item.id === run.id);
      if (!entry || entry.finished === run.finished) return;
      entry.finished = run.finished;
      const label = this.el.HistoryRuns.querySelector(`[data-test-run="${CSS.escape(run.id)}"] small`);
      if (label) label.textContent = `${UI.dateTime(run.created_at)} · ${run.finished}/${run.total} 张已处理`;
    }

    showRun(run) {
      const el = this.el;
      const same = this.run?.id === run?.id;
      this.run = run;
      el.Run.hidden = !run;
      el.CurrentEmpty.hidden = this.app.page !== "workbench" || !!run;
      if (!run) return;
      el.RunTitle.textContent = run.suite_name + (run.retry_of ? " · 重试" : "");
      el.RunDate.textContent = `${UI.dateTime(run.created_at)} · 预计渠道价 ${rmb(run.estimated_rmb)}`;
      el.Progress.value = Math.round(100 * run.finished / run.total);
      el.ProgressLabel.textContent = `${run.finished} / ${run.total} 张已处理`;
      const count = run.counts;
      el.Metrics.innerHTML = [
        ["排队", count.queued || 0],
        ["执行中", (count.running || 0) + (count.reconnecting || 0) + (count.canceling || 0)],
        ["成功", count.succeeded || 0], ["失败", count.failed || 0], ["取消", count.canceled || 0],
        ...(run.expired ? [["过期", run.expired]] : []),
      ].map(([name, value]) => `<span>${name} <b>${value}</b></span>`).join("");
      el.RunNotice.textContent = run.active && !run.worker_ready ? "Worker 暂未就绪，任务已保存在队列中。" : "";
      el.RunNotice.hidden = !el.RunNotice.textContent;
      el.Cancel.hidden = !run.active;
      el.Retry.hidden = run.active || !(count.failed || count.interrupted);
      const indices = [...new Set(run.results.map((result) => result.question_index))];
      const question = same ? el.CompareQuestion.value : "";
      const options = indices.map((index) => option(index, run.questions[index].title)).join("");
      if (options !== this.questionOptions) {
        this.questionOptions = options;
        el.CompareQuestion.innerHTML = options;
      }
      el.CompareQuestion.value = indices.includes(Number(question)) && question !== "" ? question : String(indices[0] ?? "");
      el.CompareQuestion.disabled = !indices.length;
      this.renderComparison();
    }

    renderComparison() {
      const index = this.el.CompareQuestion.value;
      const question = index === "" ? null : this.run.questions[Number(index)];
      const results = this.run.results.filter((result) => String(result.question_index) === index);
      const version = JSON.stringify([this.run.id, index, results]);
      if (version === this.comparisonVersion) return;
      this.comparisonVersion = version;
      const questionKey = `${this.run.id}/${index}`;
      if (this.questionKey !== questionKey) {
        this.questionKey = questionKey;
        this.el.QuestionContext.innerHTML = question ? `
          <p class="subline">${escape(question.size === "auto" ? "自动尺寸" : question.size)} · 质量 ${qualities[question.quality]}</p>
          ${question.criteria ? `<p class="image-test-criteria">${escape(question.criteria)}</p>` : ""}
          <details><summary>绘图提示词</summary><p>${escape(question.prompt)}</p></details>
        ` : "";
      }
      this.el.Results.innerHTML = results.map((result) => this.resultCard(result)).join("")
        || '<p class="image-test-empty">此批次的图片已随生成记录过期。</p>';
      UI.icons(this.el.Results);
    }

    resultCard(result) {
      const status = statuses[result.status] || "";
      const elapsed = result.elapsed_seconds == null ? "" : `${result.elapsed_seconds.toFixed(1)} 秒`;
      const dimensions = result.width && result.height ? `${result.width} × ${result.height}` : "";
      const meta = [elapsed, dimensions, result.image_url ? result.format.toUpperCase() : ""].filter(Boolean);
      return `<article class="image-test-result">
        <div class="image-test-result-heading"><div>
          <strong>${escape(result.channel_label)}</strong><small>${escape(result.model)}</small>
        </div><span class="status-badge ${escape(result.status)}"><span></span>${status}</span></div>
        ${result.image_url ? `
          <button type="button" class="image-test-preview" data-image-preview
            data-image-preview-src="${escape(result.image_url)}" data-image-preview-title="${escape(result.channel_label + " · " + result.model)}"
            aria-label="查看 ${escape(result.channel_label)} ${escape(result.model)} 原图">
            <img src="${escape(result.thumbnail_url)}" alt="" loading="lazy">
          </button>` : `<div class="image-test-result-placeholder"><i data-lucide="${result.error ? "circle-alert" : "image"}"></i>
            ${result.error ? `<p>${escape(result.error)}</p>` : `<p>${status}</p>`}
            ${result.error_code ? `<small>${escape(result.error_code)}${result.upstream_status ? ` · HTTP ${result.upstream_status}` : ""}</small>` : ""}
          </div>`}
        <div class="image-test-result-footer">
          <span class="subline">${meta.join(" · ")}</span>
          ${result.image_url ? `<div class="image-test-actions">
            <select data-test-verdict="${result.id}" aria-label="复核标记">
              ${Object.entries(verdicts).map(([value, label]) => `<option value="${value}" ${value === result.verdict ? "selected" : ""}>${label}</option>`).join("")}
            </select>
            <a class="icon-button" href="${escape(result.image_url)}?download=1" title="下载原图" aria-label="下载原图"><i data-lucide="download"></i></a>
          </div>` : ""}
        </div>
      </article>`;
    }

    async reviewResult(event) {
      const select = event.target.closest("[data-test-verdict]");
      if (!select) return;
      const run = this.run;
      const result = run.results.find((item) => item.id === select.dataset.testVerdict);
      const value = select.value;
      select.disabled = true;
      try {
        await UI.api(`${apiRoot}/runs/${run.id}/results/${result.id}`, { method: "PUT", body: { verdict: value } });
        if (this.run?.id === run.id) await this.loadRun(run.id);
      } catch (error) {
        select.value = result.verdict;
        this.app.setError(error.message);
      } finally {
        select.disabled = false;
      }
    }
  }

  class AdminImageTests {
    constructor() {
      this.root = document.querySelector(".image-tests");
      this.el = Object.fromEntries(
        [...this.root.querySelectorAll("[id]")].map((el) => [el.id.replace("imageTest", ""), el]),
      );
      this.page = "workbench";
      this.suites = new AdminImageTestSuites(this);
      this.runs = new AdminImageTestRuns(this);
      this.bind();
    }

    bind() {
      const el = this.el;
      this.root.querySelectorAll("[data-test-page]").forEach((button) => {
        button.addEventListener("click", () => this.showPage(button.dataset.testPage));
      });
      el.Refresh.addEventListener("click", () => this.activate(true));
      el.Targets.addEventListener("change", (event) => {
        const group = event.target.closest("[data-test-channel]");
        if (group) {
          group.closest(".image-test-channel").querySelectorAll("[data-test-target]").forEach((box) => {
            box.checked = group.checked;
          });
        }
        this.updateEstimate();
      });
      el.SelectAll.addEventListener("click", () => this.selectTargets(true));
      el.SelectNone.addEventListener("click", () => this.selectTargets(false));
      el.Launch.addEventListener("submit", (event) => {
        event.preventDefault();
        const suite = this.suites.selectedSuite();
        if (!suite || el.Start.disabled) return;
        this.runs.runAction(el.Start, "/runs", {
          suite_id: suite.id, suite_revision: suite.revision, targets: this.selectedTargets(),
        });
      });

    }

    async activate(refresh = false) {
      if (this.loadingCatalog) return;
      this.loadingCatalog = true;
      this.setError("");
      try {
        const selected = this.selectedTargets();
        const hadAll = !this.catalog || selected.length === this.targetBoxes().length;
        this.catalog = await UI.api(apiRoot);
        this.suites.loadCatalog(this.catalog);
        this.renderTargets(hadAll, selected);
        this.updateEstimate();
        this.el.HistoryRetention.textContent = `测评记录和图片随生成记录保留 ${this.catalog.history_retention_days} 天。`;
        await this.runs.activate(this.page, refresh);
      } catch (error) {
        this.setError(error.message);
      } finally {
        this.loadingCatalog = false;
      }
    }

    showPage(page, suite) {
      this.setError("");
      this.page = page;
      this.root.querySelectorAll("[data-test-page]").forEach((button) => {
        const active = button.dataset.testPage === page;
        button.classList.toggle("active", active);
        button.setAttribute("aria-pressed", String(active));
      });
      this.root.querySelectorAll("[data-test-pane]").forEach((pane) => {
        pane.hidden = pane.dataset.testPane !== page;
      });
      if (page === "suites") {
        if (suite) this.suites.editSuite(suite);
        return;
      }
      this.runs.showPage(page);
    }

    setError(message) {
      this.el.Error.textContent = message;
      this.el.Error.hidden = !message;
    }

    poll() {
      return this.runs.poll();
    }

    targetBoxes() {
      return [...this.el.Targets.querySelectorAll("[data-test-target]")];
    }

    selectedTargets() {
      return this.targetBoxes().filter((box) => box.checked).map((box) => ({
        channel_id: box.dataset.channelId, model: box.value,
      }));
    }

    renderTargets(all, selected) {
      this.el.Targets.innerHTML = this.catalog.channels.map((channel) => {
        const unavailable = !channel.configured ? "待配置 API Key"
          : !channel.capabilities.modes.includes("text2img") ? "未启用文生图" : "";
        const header = `<strong>${escape(channel.label)}</strong><small>${rmb(channel.price_rmb)} / 张</small>`;
        const models = channel.models.map((model) => {
          const checked = all || selected.some((target) => target.channel_id === channel.id && target.model === model.id);
          return `<label class="check-line"><input type="checkbox" data-test-target
            data-channel-id="${escape(channel.id)}" data-price="${escape(channel.price_rmb)}"
            value="${escape(model.id)}" ${checked ? "checked" : ""}>
            <span>${escape(model.label)}${model.label !== model.id ? `<small>${escape(model.id)}</small>` : ""}</span></label>`;
        }).join("");
        return `<div class="image-test-channel">
          ${unavailable
            ? `<div class="image-test-channel-heading">${header}<span class="subline">${unavailable}</span></div>`
            : `<label class="image-test-channel-heading check-line"><input type="checkbox" data-test-channel>${header}</label>
               <div class="image-test-models">${models}</div>`}
          </div>`;
      }).join("") || '<p class="image-test-empty">暂无启用的生图渠道。</p>';
    }

    selectTargets(checked) {
      this.targetBoxes().forEach((box) => { box.checked = checked; });
      this.updateEstimate();
    }

    updateEstimate() {
      if (!this.catalog) return;
      const boxes = this.targetBoxes();
      const selected = boxes.filter((box) => box.checked);
      this.el.Targets.querySelectorAll("[data-test-channel]").forEach((group) => {
        const models = [...group.closest(".image-test-channel").querySelectorAll("[data-test-target]")];
        group.checked = models.every((model) => model.checked);
        group.indeterminate = !group.checked && models.some((model) => model.checked);
      });
      const questions = this.suites.selectedSuite()?.questions.length || 0;
      const images = questions * selected.length;
      const price = questions * selected.reduce((sum, box) => sum + Number(box.dataset.price), 0);
      this.el.TargetCount.textContent = `已选 ${selected.length} / ${boxes.length} 个模型`;
      this.el.SelectAll.disabled = selected.length === boxes.length;
      this.el.SelectNone.disabled = !selected.length;
      this.el.Start.disabled = this.runs.actionPending || !images || images > this.catalog.max_images;
      this.el.Estimate.textContent = images > this.catalog.max_images
        ? `已选 ${images} 张，单批最多 ${this.catalog.max_images} 张，请减少模型或题目。`
        : `${questions} 道题 · ${images} 张图 · 预计渠道价 ${rmb(price)}`;
    }


  }

  window.AdminImageTests = AdminImageTests;
})();
