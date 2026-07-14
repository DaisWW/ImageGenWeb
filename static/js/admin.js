(() => {
  "use strict";

  const UI = window.ImageGen;
  const STATUS = {
    queued: "排队中",
    running: "生成中",
    canceling: "取消中",
    succeeded: "已完成",
    partial: "部分完成",
    failed: "失败",
    canceled: "已取消",
  };
  const copy = (value) => JSON.parse(JSON.stringify(value));

  class AdminApp {
    constructor() {
      this.activeTab = "users";
      this.users = [];
      this.spending = {};
      this.jobs = [];
      this.channelConfig = null;
      this.chatConfig = null;
      this.cacheElements();
      this.bindEvents();
      this.loadUsers();
      this.pollTimer = window.setInterval(() => {
        if (this.activeTab === "generations") this.loadJobs(false);
      }, 3500);
    }

    cacheElements() {
      const byId = (id) => document.getElementById(id);
      this.el = {
        tabs: [...document.querySelectorAll(".admin-tab")],
        views: [...document.querySelectorAll(".admin-view")],
        userTableBody: byId("userTableBody"),
        companyTodaySpending: byId("companyTodaySpending"),
        companyTotalSpending: byId("companyTotalSpending"),
        createUserButton: byId("createUserButton"),
        userDialog: byId("userDialog"),
        userForm: byId("userForm"),
        balanceDialog: byId("balanceDialog"),
        balanceForm: byId("balanceForm"),
        balanceDialogTitle: byId("balanceDialogTitle"),
        resetDialog: byId("resetDialog"),
        resetForm: byId("resetForm"),
        resetDialogTitle: byId("resetDialogTitle"),
        adminJobList: byId("adminJobList"),
        queueOverview: byId("queueOverview"),
        refreshJobsButton: byId("refreshJobsButton"),
        channelTableBody: byId("channelTableBody"),
        configVersion: byId("configVersion"),
        configError: byId("configError"),
        queueSettingsButton: byId("queueSettingsButton"),
        createChannelButton: byId("createChannelButton"),
        channelDialog: byId("channelDialog"),
        channelDialogTitle: byId("channelDialogTitle"),
        channelForm: byId("channelForm"),
        channelModelList: byId("channelModelList"),
        addChannelModelButton: byId("addChannelModelButton"),
        queueDialog: byId("queueDialog"),
        queueForm: byId("queueForm"),
        chatModelTableBody: byId("chatModelTableBody"),
        chatConfigVersion: byId("chatConfigVersion"),
        chatConfigError: byId("chatConfigError"),
        contextSettingsButton: byId("contextSettingsButton"),
        createChatModelButton: byId("createChatModelButton"),
        chatModelDialog: byId("chatModelDialog"),
        chatModelDialogTitle: byId("chatModelDialogTitle"),
        chatModelForm: byId("chatModelForm"),
        contextDialog: byId("contextDialog"),
        contextForm: byId("contextForm"),
        settingsForm: byId("settingsForm"),
        siteTitleInput: byId("siteTitleInput"),
        versionInput: byId("versionInput"),
      };
    }

    bindEvents() {
      this.el.tabs.forEach((tab) => tab.addEventListener("click", () => this.selectTab(tab.dataset.tab)));
      this.el.createUserButton.addEventListener("click", () => UI.openDialog(this.el.userDialog));
      this.el.userForm.addEventListener("submit", (event) => this.createUser(event));
      this.el.userTableBody.addEventListener("click", (event) => this.handleUserAction(event));
      this.el.balanceForm.addEventListener("submit", (event) => this.adjustBalance(event));
      this.el.resetForm.addEventListener("submit", (event) => this.resetPassword(event));
      this.el.refreshJobsButton.addEventListener("click", () => this.loadJobs(true));
      this.el.adminJobList.addEventListener("click", (event) => this.handleJobAction(event));
      this.el.queueSettingsButton.addEventListener("click", () => this.openQueueDialog());
      this.el.createChannelButton.addEventListener("click", () => this.openChannelDialog());
      this.el.channelTableBody.addEventListener("click", (event) => this.handleChannelAction(event));
      this.el.channelForm.addEventListener("submit", (event) => this.saveChannel(event));
      this.el.addChannelModelButton.addEventListener("click", () => this.addChannelModelRow());
      this.el.channelModelList.addEventListener("click", (event) => {
        const button = event.target.closest("[data-remove-channel-model]");
        if (button) button.closest(".config-model-row").remove();
      });
      this.el.queueForm.addEventListener("submit", (event) => this.saveQueue(event));
      this.el.contextSettingsButton.addEventListener("click", () => this.openContextDialog());
      this.el.createChatModelButton.addEventListener("click", () => this.openChatModelDialog());
      this.el.chatModelTableBody.addEventListener("click", (event) => this.handleChatModelAction(event));
      this.el.chatModelForm.addEventListener("submit", (event) => this.saveChatModel(event));
      this.el.contextForm.addEventListener("submit", (event) => this.saveContext(event));
      this.el.settingsForm.addEventListener("submit", (event) => this.saveSettings(event));
    }

    selectTab(name) {
      this.activeTab = name;
      this.el.tabs.forEach((tab) => tab.classList.toggle("active", tab.dataset.tab === name));
      this.el.views.forEach((view) => view.classList.toggle("active", view.dataset.view === name));
      if (name === "users") this.loadUsers();
      if (name === "generations") this.loadJobs();
      if (name === "channels") {
        this.loadChannels();
        this.loadChatModels();
      }
      if (name === "settings") this.loadSettings();
    }

    async loadUsers() {
      try {
        const data = await UI.api("/api/admin/users");
        this.users = data.users;
        this.spending = data.spending;
        this.renderUsers();
      } catch (error) {
        UI.toast(error.message, "error");
      }
    }

    renderUsers() {
      this.el.companyTodaySpending.textContent = UI.money(this.spending.today_rmb);
      this.el.companyTotalSpending.textContent = UI.money(this.spending.total_rmb);
      this.el.userTableBody.innerHTML = this.users.map((user) => `
        <tr>
          <td><div class="table-user"><span>${UI.escapeHtml((user.display_name || user.username).slice(0, 1).toUpperCase())}</span><div><strong>${UI.escapeHtml(user.display_name || user.username)}</strong><small>${UI.escapeHtml(user.username)} · ${user.role === "admin" ? "管理员" : "用户"}</small></div></div></td>
          <td><span class="status-badge ${user.status === "active" ? "succeeded" : "failed"}"><span></span>${user.status === "active" ? "启用" : "停用"}</span></td>
          <td class="money-cell">${UI.money(user.balance_rmb)}</td>
          <td class="spending-cell"><strong>${UI.money(user.spending?.total_rmb)}</strong><small>今日 ${UI.money(user.spending?.today_rmb)}</small></td>
          <td>${UI.money(user.reserved_rmb)}</td>
          <td>${user.generation_concurrency}</td>
          <td>${UI.dateTime(user.last_login_at)}</td>
          <td class="actions-cell"><div class="row-actions">
            <button class="icon-button" type="button" data-balance-user="${user.id}" title="调整余额" aria-label="调整余额"><i data-lucide="wallet-cards"></i></button>
            <button class="icon-button" type="button" data-reset-user="${user.id}" title="重置密码" aria-label="重置密码"><i data-lucide="key-round"></i></button>
            <button class="icon-button ${user.status === "active" ? "danger" : "accent"}" type="button" data-status-user="${user.id}" data-next-status="${user.status === "active" ? "disabled" : "active"}" title="${user.status === "active" ? "停用账户" : "启用账户"}" aria-label="${user.status === "active" ? "停用账户" : "启用账户"}"><i data-lucide="${user.status === "active" ? "user-x" : "user-check"}"></i></button>
          </div></td>
        </tr>`).join("");
      UI.icons(this.el.userTableBody);
    }

    userById(id) {
      return this.users.find((user) => user.id === Number(id));
    }

    async handleUserAction(event) {
      const balance = event.target.closest("[data-balance-user]");
      if (balance) {
        const user = this.userById(balance.dataset.balanceUser);
        this.el.balanceDialogTitle.textContent = `调整 ${user.display_name || user.username} 的余额`;
        this.el.balanceForm.elements.user_id.value = user.id;
        this.el.balanceForm.elements.amount_rmb.value = "";
        this.el.balanceForm.elements.note.value = "";
        UI.openDialog(this.el.balanceDialog);
        return;
      }
      const reset = event.target.closest("[data-reset-user]");
      if (reset) {
        const user = this.userById(reset.dataset.resetUser);
        this.el.resetDialogTitle.textContent = `重置 ${user.display_name || user.username} 的密码`;
        this.el.resetForm.elements.user_id.value = user.id;
        this.el.resetForm.elements.password.value = "";
        UI.openDialog(this.el.resetDialog);
        return;
      }
      const status = event.target.closest("[data-status-user]");
      if (!status) return;
      const user = this.userById(status.dataset.statusUser);
      const action = status.dataset.nextStatus === "active" ? "启用" : "停用";
      if (!window.confirm(`${action}账户“${user.display_name || user.username}”？`)) return;
      status.disabled = true;
      try {
        await UI.api(`/api/admin/users/${user.id}/status`, {
          method: "POST", body: { status: status.dataset.nextStatus },
        });
        await this.loadUsers();
        UI.toast(`账户已${action}`, "success");
      } catch (error) {
        status.disabled = false;
        UI.toast(error.message, "error");
      }
    }

    async createUser(event) {
      event.preventDefault();
      const submit = this.el.userForm.querySelector('[type="submit"]');
      submit.disabled = true;
      try {
        const form = Object.fromEntries(new FormData(this.el.userForm));
        form.generation_concurrency = Number(form.generation_concurrency);
        await UI.api("/api/admin/users", { method: "POST", body: form });
        this.el.userForm.reset();
        UI.closeDialog(this.el.userDialog);
        await this.loadUsers();
        UI.toast("用户已创建", "success");
      } catch (error) {
        UI.toast(error.message, "error");
      } finally {
        submit.disabled = false;
      }
    }

    async adjustBalance(event) {
      event.preventDefault();
      const form = Object.fromEntries(new FormData(this.el.balanceForm));
      const submit = this.el.balanceForm.querySelector('[type="submit"]');
      submit.disabled = true;
      try {
        await UI.api(`/api/admin/users/${form.user_id}/balance`, {
          method: "POST",
          body: { operation: form.operation, amount_rmb: form.amount_rmb, note: form.note },
        });
        UI.closeDialog(this.el.balanceDialog);
        await this.loadUsers();
        UI.toast("余额已更新并记录流水", "success");
      } catch (error) {
        UI.toast(error.message, "error");
      } finally {
        submit.disabled = false;
      }
    }

    async resetPassword(event) {
      event.preventDefault();
      const form = Object.fromEntries(new FormData(this.el.resetForm));
      const submit = this.el.resetForm.querySelector('[type="submit"]');
      submit.disabled = true;
      try {
        await UI.api(`/api/admin/users/${form.user_id}/password`, {
          method: "POST", body: { password: form.password },
        });
        this.el.resetForm.reset();
        UI.closeDialog(this.el.resetDialog);
        UI.toast("密码已重置", "success");
      } catch (error) {
        UI.toast(error.message, "error");
      } finally {
        submit.disabled = false;
      }
    }

    async loadJobs(notify = false) {
      try {
        const data = await UI.api("/api/admin/generations?limit=100");
        this.jobs = data.jobs;
        this.el.queueOverview.textContent = `生成中 ${data.running_images} 张 · 排队 ${data.queued_images} 张 · 共 ${data.jobs.length} 条记录`;
        this.renderJobs();
        if (notify) UI.toast("记录已刷新", "success");
      } catch (error) {
        UI.toast(error.message, "error");
      }
    }

    renderJobs() {
      if (!this.jobs.length) {
        this.el.adminJobList.innerHTML = '<div class="admin-empty"><i data-lucide="activity"></i><span>暂无生成记录</span></div>';
        UI.icons(this.el.adminJobList);
        return;
      }
      this.el.adminJobList.innerHTML = this.jobs.map((job) => {
        const outputs = job.items.map((item) => item.thumbnail_url
          ? `<a class="admin-output" href="${item.image_url}" target="_blank" title="查看原图"><img src="${item.thumbnail_url}" alt="生成结果"></a>`
          : `<span class="admin-output empty ${item.status}"><i data-lucide="${item.status === "failed" ? "circle-alert" : "loader-circle"}"></i></span>`).join("");
        const references = job.references.length
          ? `<div class="admin-references"><span>垫图</span>${job.references.map((asset) => `<a href="${asset.url}" target="_blank"><img src="${asset.url}" alt="${UI.escapeHtml(asset.name)}"></a>`).join("")}</div>`
          : "";
        return `<article class="admin-job-card ${job.status}">
          <div class="admin-job-top">
            <div class="admin-job-owner"><span class="user-avatar small">${UI.escapeHtml((job.user.display_name || job.user.username).slice(0, 1).toUpperCase())}</span><div><strong>${UI.escapeHtml(job.user.display_name || job.user.username)}</strong><small>${UI.dateTime(job.created_at)} · ${UI.escapeHtml(job.channel)} · ${UI.escapeHtml(job.model)}</small></div></div>
            <div class="admin-job-state"><span class="status-badge ${job.status}"><span></span>${STATUS[job.status] || job.status}</span>${job.can_cancel ? `<button class="button danger small" data-admin-cancel="${job.id}"><i data-lucide="square"></i>取消</button>` : ""}</div>
          </div>
          <p class="admin-job-prompt">${UI.escapeHtml(job.prompt)}</p>
          <div class="admin-job-detail"><span>${job.size}</span><span>${job.quality}</span><span>${job.succeeded_count}/${job.requested_count} 张</span><span>${UI.money(job.charged_rmb)} 已扣</span>${job.queue_position ? `<span>队列 ${job.queue_position}/${job.queue_total}</span>` : ""}</div>
          ${references}<div class="admin-output-row">${outputs}</div>
        </article>`;
      }).join("");
      UI.icons(this.el.adminJobList);
    }

    async handleJobAction(event) {
      const button = event.target.closest("[data-admin-cancel]");
      if (!button) return;
      button.disabled = true;
      try {
        await UI.api(`/api/admin/generations/${button.dataset.adminCancel}/cancel`, { method: "POST" });
        await this.loadJobs();
        UI.toast("取消请求已提交", "success");
      } catch (error) {
        button.disabled = false;
        UI.toast(error.message, "error");
      }
    }

    async loadChannels() {
      try {
        const data = await UI.api("/api/admin/channels");
        this.channelConfig = data.config;
        this.renderChannels();
      } catch (error) {
        UI.toast(error.message, "error");
      }
    }

    renderChannels() {
      const config = this.channelConfig;
      if (!config) return;
      const origin = config.managed ? "数据库管理" : "启动默认";
      this.el.configVersion.textContent = `${origin} · 版本 ${config.version} · 全局并发 ${config.queue.global_concurrency}`;
      this.el.configError.hidden = !config.last_error;
      this.el.configError.textContent = config.last_error || "";
      this.el.channelTableBody.innerHTML = config.channels.map((channel) => `
        <tr>
          <td><strong>${UI.escapeHtml(channel.label)}</strong><small class="subline">${UI.escapeHtml(channel.id)}</small></td>
          <td><span class="status-badge ${channel.configured ? "succeeded" : "failed"}"><span></span>${channel.configured ? "可用" : channel.enabled ? "缺少 Key" : "停用"}</span></td>
          <td class="money-cell">${UI.money(channel.price_rmb)}<small class="subline">每张</small></td>
          <td><div class="tag-list">${channel.models.filter((model) => model.enabled).map((model) => `<span>${UI.escapeHtml(model.label)}<small>${UI.escapeHtml(model.id)}</small></span>`).join("")}</div></td>
          <td>${channel.limits.max_concurrency}</td>
          <td><div class="capability-list"><span>${channel.capabilities.modes.includes("img2img") ? `最多 ${channel.capabilities.max_reference_images} 张垫图` : "仅文生图"}</span><span>${channel.capabilities.sizes.join(" / ")}</span></div></td>
          <td class="actions-cell"><div class="row-actions">
            <button class="icon-button" type="button" data-edit-channel="${UI.escapeHtml(channel.id)}" title="编辑渠道" aria-label="编辑渠道"><i data-lucide="pencil"></i></button>
            <button class="icon-button danger" type="button" data-delete-channel="${UI.escapeHtml(channel.id)}" title="删除渠道" aria-label="删除渠道"><i data-lucide="trash-2"></i></button>
          </div></td>
        </tr>`).join("");
      UI.icons(this.el.channelTableBody);
    }

    openChannelDialog(identifier = "") {
      const channel = this.channelConfig?.channels.find((item) => item.id === identifier) || null;
      const form = this.el.channelForm;
      form.reset();
      form.elements.original_id.value = channel?.id || "";
      form.elements.id.value = channel?.id || "";
      form.elements.id.readOnly = Boolean(channel);
      form.elements.label.value = channel?.label || "";
      form.elements.enabled.checked = channel ? channel.enabled : true;
      form.elements.base_url.value = channel?.base_url || "";
      form.elements.api_key.value = "";
      form.elements.api_key.placeholder = channel?.has_api_key ? "已配置，留空保持" : "";
      form.elements.price_rmb.value = UI.amount(channel?.price_rmb);
      form.elements.clear_api_key.checked = false;
      form.querySelector("[data-clear-key-row]").hidden = !channel?.has_api_key;
      this.renderChannelModels(channel?.models || [{ id: "", label: "", enabled: true }]);

      const capabilities = channel?.capabilities || {};
      this.setChecks(form, "mode", capabilities.modes || ["text2img", "img2img"], ["text2img", "img2img"]);
      this.setChecks(form, "quality", capabilities.qualities || ["medium"], ["auto", "low", "medium", "high"]);
      this.setChecks(form, "format", capabilities.formats || ["png"], ["png", "jpeg", "webp"]);
      form.elements.sizes.value = (capabilities.sizes || ["1024x1024"]).join(", ");
      form.elements.max_reference_images.value = capabilities.max_reference_images ?? 1;
      form.elements.max_reference_image_mb.value = capabilities.max_reference_image_mb ?? 10;
      form.elements.max_reference_total_mb.value = capabilities.max_reference_total_mb ?? 40;
      form.elements.reference_field.value = capabilities.reference_field || "image";
      form.elements.max_concurrency.value = channel?.limits.max_concurrency ?? 2;
      form.elements.timeout_seconds.value = channel?.limits.timeout_seconds ?? 600;
      form.elements.estimated_seconds.value = channel?.limits.estimated_seconds ?? 180;
      this.el.channelDialogTitle.textContent = channel ? `编辑 ${channel.label}` : "新增生图渠道";
      UI.openDialog(this.el.channelDialog);
    }

    renderChannelModels(models) {
      this.el.channelModelList.replaceChildren();
      models.forEach((model) => this.addChannelModelRow(model));
    }

    addChannelModelRow(model = { id: "", label: "", enabled: true }) {
      const row = document.createElement("div");
      row.className = "config-model-row";
      row.innerHTML = `
        <label class="field"><span>模型 ID</span><input data-model-id maxlength="100" required></label>
        <label class="field"><span>显示名称</span><input data-model-label maxlength="100" required></label>
        <label class="check-line"><input type="checkbox" data-model-enabled><span>启用</span></label>
        <button class="icon-button danger" type="button" data-remove-channel-model title="移除模型" aria-label="移除模型"><i data-lucide="trash-2"></i></button>`;
      row.querySelector("[data-model-id]").value = model.id || "";
      row.querySelector("[data-model-label]").value = model.label || "";
      row.querySelector("[data-model-enabled]").checked = model.enabled !== false;
      this.el.channelModelList.append(row);
      UI.icons(row);
    }

    async handleChannelAction(event) {
      const edit = event.target.closest("[data-edit-channel]");
      if (edit) {
        this.openChannelDialog(edit.dataset.editChannel);
        return;
      }
      const remove = event.target.closest("[data-delete-channel]");
      if (!remove) return;
      const channel = this.channelConfig.channels.find((item) => item.id === remove.dataset.deleteChannel);
      if (!window.confirm(`删除生图渠道“${channel.label}”？`)) return;
      const next = copy(this.channelConfig);
      next.channels = next.channels.filter((item) => item.id !== channel.id);
      try {
        await this.persistChannels(next, "渠道已删除");
      } catch (error) {
        UI.toast(error.message, "error");
      }
    }

    async saveChannel(event) {
      event.preventDefault();
      const submit = this.el.channelForm.querySelector('[type="submit"]');
      submit.disabled = true;
      try {
        const channel = this.channelFromForm();
        const next = copy(this.channelConfig);
        const originalId = this.el.channelForm.elements.original_id.value;
        const index = next.channels.findIndex((item) => item.id === originalId);
        if (index >= 0) next.channels[index] = channel;
        else next.channels.push(channel);
        await this.persistChannels(next, originalId ? "渠道已更新" : "渠道已创建");
        UI.closeDialog(this.el.channelDialog);
      } catch (error) {
        UI.toast(error.message, "error");
      } finally {
        submit.disabled = false;
      }
    }

    channelFromForm() {
      const form = this.el.channelForm;
      const models = [...this.el.channelModelList.querySelectorAll(".config-model-row")].map((row) => ({
        id: row.querySelector("[data-model-id]").value.trim(),
        label: row.querySelector("[data-model-label]").value.trim(),
        enabled: row.querySelector("[data-model-enabled]").checked,
      }));
      return {
        id: form.elements.id.value.trim(),
        label: form.elements.label.value.trim(),
        enabled: form.elements.enabled.checked,
        base_url: form.elements.base_url.value.trim(),
        api_key: form.elements.api_key.value.trim(),
        clear_api_key: form.elements.clear_api_key.checked,
        price_rmb: form.elements.price_rmb.value,
        models,
        capabilities: {
          modes: this.readChecks(form, "mode", ["text2img", "img2img"]),
          qualities: this.readChecks(form, "quality", ["auto", "low", "medium", "high"]),
          formats: this.readChecks(form, "format", ["png", "jpeg", "webp"]),
          sizes: form.elements.sizes.value.split(",").map((value) => value.trim()).filter(Boolean),
          max_reference_images: Number(form.elements.max_reference_images.value),
          max_reference_image_mb: Number(form.elements.max_reference_image_mb.value),
          max_reference_total_mb: Number(form.elements.max_reference_total_mb.value),
          reference_field: form.elements.reference_field.value,
        },
        limits: {
          max_concurrency: Number(form.elements.max_concurrency.value),
          timeout_seconds: Number(form.elements.timeout_seconds.value),
          estimated_seconds: Number(form.elements.estimated_seconds.value),
        },
      };
    }

    openQueueDialog() {
      const form = this.el.queueForm;
      Object.entries(this.channelConfig.queue).forEach(([name, value]) => {
        if (form.elements[name]) form.elements[name].value = value;
      });
      UI.openDialog(this.el.queueDialog);
    }

    async saveQueue(event) {
      event.preventDefault();
      const submit = this.el.queueForm.querySelector('[type="submit"]');
      submit.disabled = true;
      try {
        const next = copy(this.channelConfig);
        Object.keys(next.queue).forEach((name) => {
          next.queue[name] = Number(this.el.queueForm.elements[name].value);
        });
        await this.persistChannels(next, "队列设置已更新");
        UI.closeDialog(this.el.queueDialog);
      } catch (error) {
        UI.toast(error.message, "error");
      } finally {
        submit.disabled = false;
      }
    }

    async persistChannels(config, message) {
      const data = await UI.api("/api/admin/channels", {
        method: "PUT",
        body: { revision: this.channelConfig.revision, queue: config.queue, channels: config.channels },
      });
      this.channelConfig = data.config;
      this.renderChannels();
      UI.toast(message, "success");
    }

    async loadChatModels() {
      try {
        const data = await UI.api("/api/admin/chat-models");
        this.chatConfig = data.config;
        this.renderChatModels();
      } catch (error) {
        UI.toast(error.message, "error");
      }
    }

    renderChatModels() {
      const config = this.chatConfig;
      if (!config) return;
      const context = config.context;
      const origin = config.managed ? "数据库管理" : "启动默认";
      this.el.chatConfigVersion.textContent = `${origin} · 版本 ${config.version} · 最大上下文 ${context.max_context_tokens.toLocaleString()} tokens`;
      this.el.chatConfigError.hidden = !config.last_error;
      this.el.chatConfigError.textContent = config.last_error || "";
      this.el.chatModelTableBody.innerHTML = config.models.map((model) => `
        <tr>
          <td><strong>${UI.escapeHtml(model.label)}</strong><small class="subline">${UI.escapeHtml(model.id)}</small></td>
          <td><span class="status-badge ${model.configured ? "succeeded" : "failed"}"><span></span>${model.configured ? "可用" : model.enabled ? "缺少 Key" : "停用"}</span></td>
          <td>${UI.escapeHtml(model.model)}${model.reasoning_effort ? `<small class="subline">推理 ${UI.escapeHtml(model.reasoning_effort)}</small>` : ""}</td>
          <td><div class="capability-list"><span>${context.compact_at_tokens.toLocaleString()} tokens 开始压缩</span><span>保留最近 ${context.keep_recent_messages} 条消息</span></div></td>
          <td class="actions-cell"><div class="row-actions">
            <button class="icon-button" type="button" data-edit-chat-model="${UI.escapeHtml(model.id)}" title="编辑模型" aria-label="编辑模型"><i data-lucide="pencil"></i></button>
            <button class="icon-button danger" type="button" data-delete-chat-model="${UI.escapeHtml(model.id)}" title="删除模型" aria-label="删除模型"><i data-lucide="trash-2"></i></button>
          </div></td>
        </tr>`).join("");
      UI.icons(this.el.chatModelTableBody);
    }

    openChatModelDialog(identifier = "") {
      const model = this.chatConfig?.models.find((item) => item.id === identifier) || null;
      const form = this.el.chatModelForm;
      form.reset();
      form.elements.original_id.value = model?.id || "";
      form.elements.id.value = model?.id || "";
      form.elements.id.readOnly = Boolean(model);
      form.elements.label.value = model?.label || "";
      form.elements.enabled.checked = model ? model.enabled : true;
      form.elements.base_url.value = model?.base_url || "";
      form.elements.api_key.value = "";
      form.elements.api_key.placeholder = model?.has_api_key ? "已配置，留空保持" : "";
      form.elements.clear_api_key.checked = false;
      form.querySelector("[data-clear-chat-key-row]").hidden = !model?.has_api_key;
      form.elements.model.value = model?.model || "";
      form.elements.reasoning_effort.value = model?.reasoning_effort || "";
      form.elements.timeout_seconds.value = model?.timeout_seconds ?? 180;
      form.elements.max_output_tokens.value = model?.max_output_tokens ?? 2000;
      this.el.chatModelDialogTitle.textContent = model ? `编辑 ${model.label}` : "新增对话模型";
      UI.openDialog(this.el.chatModelDialog);
    }

    async handleChatModelAction(event) {
      const edit = event.target.closest("[data-edit-chat-model]");
      if (edit) {
        this.openChatModelDialog(edit.dataset.editChatModel);
        return;
      }
      const remove = event.target.closest("[data-delete-chat-model]");
      if (!remove) return;
      const model = this.chatConfig.models.find((item) => item.id === remove.dataset.deleteChatModel);
      if (!window.confirm(`删除对话模型“${model.label}”？`)) return;
      const next = copy(this.chatConfig);
      next.models = next.models.filter((item) => item.id !== model.id);
      try {
        await this.persistChatModels(next, "对话模型已删除");
      } catch (error) {
        UI.toast(error.message, "error");
      }
    }

    async saveChatModel(event) {
      event.preventDefault();
      const submit = this.el.chatModelForm.querySelector('[type="submit"]');
      submit.disabled = true;
      try {
        const form = this.el.chatModelForm;
        const model = {
          id: form.elements.id.value.trim(),
          label: form.elements.label.value.trim(),
          enabled: form.elements.enabled.checked,
          base_url: form.elements.base_url.value.trim(),
          api_key: form.elements.api_key.value.trim(),
          clear_api_key: form.elements.clear_api_key.checked,
          model: form.elements.model.value.trim(),
          reasoning_effort: form.elements.reasoning_effort.value,
          timeout_seconds: Number(form.elements.timeout_seconds.value),
          max_output_tokens: Number(form.elements.max_output_tokens.value),
        };
        const next = copy(this.chatConfig);
        const originalId = form.elements.original_id.value;
        const index = next.models.findIndex((item) => item.id === originalId);
        if (index >= 0) next.models[index] = model;
        else next.models.push(model);
        await this.persistChatModels(next, originalId ? "对话模型已更新" : "对话模型已创建");
        UI.closeDialog(this.el.chatModelDialog);
      } catch (error) {
        UI.toast(error.message, "error");
      } finally {
        submit.disabled = false;
      }
    }

    openContextDialog() {
      Object.entries(this.chatConfig.context).forEach(([name, value]) => {
        this.el.contextForm.elements[name].value = value;
      });
      UI.openDialog(this.el.contextDialog);
    }

    async saveContext(event) {
      event.preventDefault();
      const submit = this.el.contextForm.querySelector('[type="submit"]');
      submit.disabled = true;
      try {
        const next = copy(this.chatConfig);
        Object.keys(next.context).forEach((name) => {
          next.context[name] = Number(this.el.contextForm.elements[name].value);
        });
        await this.persistChatModels(next, "上下文策略已更新");
        UI.closeDialog(this.el.contextDialog);
      } catch (error) {
        UI.toast(error.message, "error");
      } finally {
        submit.disabled = false;
      }
    }

    async persistChatModels(config, message) {
      const data = await UI.api("/api/admin/chat-models", {
        method: "PUT",
        body: { revision: this.chatConfig.revision, context: config.context, models: config.models },
      });
      this.chatConfig = data.config;
      this.renderChatModels();
      UI.toast(message, "success");
    }

    setChecks(form, prefix, selected, values) {
      values.forEach((value) => {
        form.elements[`${prefix}_${value}`].checked = selected.includes(value);
      });
    }

    readChecks(form, prefix, values) {
      return values.filter((value) => form.elements[`${prefix}_${value}`].checked);
    }

    async loadSettings() {
      try {
        const data = await UI.api("/api/admin/settings");
        this.el.siteTitleInput.value = data.site_title;
        this.el.versionInput.value = data.version;
      } catch (error) {
        UI.toast(error.message, "error");
      }
    }

    async saveSettings(event) {
      event.preventDefault();
      const submit = this.el.settingsForm.querySelector('[type="submit"]');
      submit.disabled = true;
      try {
        const data = await UI.api("/api/admin/settings", {
          method: "PUT", body: { site_title: this.el.siteTitleInput.value },
        });
        document.querySelectorAll("[data-site-title]").forEach((node) => { node.textContent = data.site_title; });
        document.title = `管理后台 · ${data.site_title}`;
        UI.toast("系统 Title 已更新", "success");
      } catch (error) {
        UI.toast(error.message, "error");
      } finally {
        submit.disabled = false;
      }
    }
  }

  document.addEventListener("DOMContentLoaded", () => new AdminApp());
})();
