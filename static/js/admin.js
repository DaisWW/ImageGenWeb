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
  const LOG_CATEGORY = { chat: "对话", generation: "生成", worker: "Worker", web: "Web" };
  const LOG_STATUS = { success: "成功", error: "失败" };
  const copy = (value) => JSON.parse(JSON.stringify(value));

  class AdminApp {
    constructor() {
      this.activeTab = "users";
      this.users = [];
      this.generationWorkspaces = null;
      this.spending = {};
      this.jobs = [];
      this.jobsInitialized = false;
      this.channelConfig = null;
      this.chatConfig = null;
      this.systemConfig = null;
      this.logMode = "runtime";
      this.logs = [];
      this.logOffsets = { runtime: 0, audit: 0 };
      this.logPageSize = 50;
      this.logTotal = 0;
      this.logsLoading = false;
      this.logsReloadPending = false;
      this.logsReloadNotify = false;
      this.lastLogPoll = 0;
      this.reducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)");
      this.cacheElements();
      this.bindEvents();
      window.requestAnimationFrame(() => this.updateTabIndicator(false));
      this.loadUsers();
      this.loadSettings(false);
      this.pollTimer = window.setInterval(() => {
        if (this.activeTab === "generations") this.loadJobs(false);
        if (this.activeTab === "logs" && this.logMode === "runtime" && Date.now() - this.lastLogPoll > 10000) {
          this.loadLogs(false);
        }
      }, 3500);
    }

    cacheElements() {
      const byId = (id) => document.getElementById(id);
      this.el = {
        tabs: [...document.querySelectorAll(".admin-tab")],
        tabIndicator: byId("adminTabIndicator"),
        views: [...document.querySelectorAll(".admin-view")],
        userTableBody: byId("userTableBody"),
        companyTodaySpending: byId("companyTodaySpending"),
        companyTotalSpending: byId("companyTotalSpending"),
        createUserButton: byId("createUserButton"),
        userDialog: byId("userDialog"),
        userForm: byId("userForm"),
        editUserDialog: byId("editUserDialog"),
        editUserDialogTitle: byId("editUserDialogTitle"),
        editUserForm: byId("editUserForm"),
        balanceDialog: byId("balanceDialog"),
        balanceForm: byId("balanceForm"),
        balanceDialogTitle: byId("balanceDialogTitle"),
        resetDialog: byId("resetDialog"),
        resetForm: byId("resetForm"),
        resetDialogTitle: byId("resetDialogTitle"),
        adminJobList: byId("adminJobList"),
        queueOverview: byId("queueOverview"),
        refreshJobsButton: byId("refreshJobsButton"),
        generationFilterForm: byId("generationFilterForm"),
        generationUserFilter: byId("generationUserFilter"),
        generationWorkspaceFilter: byId("generationWorkspaceFilter"),
        clearGenerationFilters: byId("clearGenerationFilters"),
        logModeButtons: [...document.querySelectorAll("[data-log-mode]")],
        logOverview: byId("logOverview"),
        refreshLogsButton: byId("refreshLogsButton"),
        runtimeLogFilterForm: byId("runtimeLogFilterForm"),
        auditLogFilterForm: byId("auditLogFilterForm"),
        runtimeLogUserFilter: byId("runtimeLogUserFilter"),
        auditLogUserFilter: byId("auditLogUserFilter"),
        runtimeLogTable: byId("runtimeLogTable"),
        auditLogTable: byId("auditLogTable"),
        runtimeLogTableBody: byId("runtimeLogTableBody"),
        auditLogTableBody: byId("auditLogTableBody"),
        logPageSummary: byId("logPageSummary"),
        previousLogPage: byId("previousLogPage"),
        nextLogPage: byId("nextLogPage"),
        logDetailDialog: byId("logDetailDialog"),
        logDetailKind: byId("logDetailKind"),
        logDetailTitle: byId("logDetailTitle"),
        logDetailMeta: byId("logDetailMeta"),
        logDetailData: byId("logDetailData"),
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
        workspacePromptsButton: byId("workspacePromptsButton"),
        workspacePromptsDialog: byId("workspacePromptsDialog"),
        workspacePromptsForm: byId("workspacePromptsForm"),
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
      this.el.createUserButton.addEventListener("click", () => this.openCreateUserDialog());
      this.el.userForm.addEventListener("submit", (event) => this.createUser(event));
      this.el.editUserForm.addEventListener("submit", (event) => this.updateUser(event));
      this.el.userTableBody.addEventListener("click", (event) => this.handleUserAction(event));
      this.el.balanceForm.addEventListener("submit", (event) => this.adjustBalance(event));
      this.el.resetForm.addEventListener("submit", (event) => this.resetPassword(event));
      this.el.refreshJobsButton.addEventListener("click", () => this.loadJobs(true));
      this.el.generationFilterForm.addEventListener("submit", (event) => this.applyGenerationFilters(event));
      this.el.generationUserFilter.addEventListener("change", () => this.renderGenerationWorkspaceFilter());
      this.el.clearGenerationFilters.addEventListener("click", () => this.clearGenerationFilters());
      this.el.logModeButtons.forEach((button) => button.addEventListener("click", () => this.selectLogMode(button.dataset.logMode)));
      this.el.refreshLogsButton.addEventListener("click", () => this.loadLogs(true));
      this.el.runtimeLogFilterForm.addEventListener("submit", (event) => this.applyLogFilters(event));
      this.el.auditLogFilterForm.addEventListener("submit", (event) => this.applyLogFilters(event));
      document.querySelectorAll("[data-clear-log-filters]").forEach((button) => {
        button.addEventListener("click", () => this.clearLogFilters(button.dataset.clearLogFilters));
      });
      this.el.runtimeLogTableBody.addEventListener("click", (event) => this.handleLogAction(event));
      this.el.auditLogTableBody.addEventListener("click", (event) => this.handleLogAction(event));
      this.el.previousLogPage.addEventListener("click", () => this.changeLogPage(-1));
      this.el.nextLogPage.addEventListener("click", () => this.changeLogPage(1));
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
      this.el.workspacePromptsButton.addEventListener("click", () => this.openWorkspacePromptsDialog());
      this.el.workspacePromptsForm.addEventListener("submit", (event) => this.saveWorkspacePrompts(event));
      this.el.contextSettingsButton.addEventListener("click", () => this.openContextDialog());
      this.el.createChatModelButton.addEventListener("click", () => this.openChatModelDialog());
      this.el.chatModelTableBody.addEventListener("click", (event) => this.handleChatModelAction(event));
      this.el.chatModelForm.addEventListener("submit", (event) => this.saveChatModel(event));
      this.el.contextForm.addEventListener("submit", (event) => this.saveContext(event));
      this.el.settingsForm.addEventListener("submit", (event) => this.saveSettings(event));
      window.addEventListener("resize", () => this.updateTabIndicator(false));
    }

    selectTab(name) {
      const previousIndex = this.el.tabs.findIndex((tab) => tab.dataset.tab === this.activeTab);
      const nextIndex = this.el.tabs.findIndex((tab) => tab.dataset.tab === name);
      if (nextIndex < 0) return;
      const changed = name !== this.activeTab;
      this.activeTab = name;
      this.el.tabs.forEach((tab) => tab.classList.toggle("active", tab.dataset.tab === name));
      this.el.views.forEach((view) => view.classList.toggle("active", view.dataset.view === name));
      this.updateTabIndicator();
      if (changed) this.animateAdminView(name, nextIndex >= previousIndex ? 1 : -1);
      if (name === "users") this.loadUsers();
      if (name === "generations") {
        this.loadGenerationFilters().then(() => this.loadJobs());
      }
      if (name === "channels") {
        this.loadChannels();
        this.loadChatModels();
      }
      if (name === "logs") this.loadLogs();
      if (name === "settings") this.loadSettings();
    }

    updateTabIndicator(animate = true) {
      const active = this.el.tabs.find((tab) => tab.dataset.tab === this.activeTab);
      if (!active || !this.el.tabIndicator) return;
      if (!animate) this.el.tabIndicator.classList.add("instant");
      this.el.tabIndicator.style.width = `${Math.max(0, active.offsetWidth - 28)}px`;
      this.el.tabIndicator.style.transform = `translateX(${active.offsetLeft + 14}px)`;
      if (!animate) {
        window.requestAnimationFrame(() => this.el.tabIndicator.classList.remove("instant"));
      }
    }

    animateAdminView(name, direction) {
      if (this.reducedMotion.matches) return;
      const view = this.el.views.find((item) => item.dataset.view === name);
      if (!view || typeof view.animate !== "function") return;
      view.getAnimations().forEach((animation) => animation.cancel());
      view.animate(
        [
          { opacity: 0, transform: `translateX(${direction * 8}px)` },
          { opacity: 1, transform: "translateX(0)" },
        ],
        { duration: 220, easing: "cubic-bezier(.22, 1, .36, 1)" },
      );
    }

    async loadUsers() {
      try {
        const data = await UI.api("/api/admin/users");
        this.users = data.users;
        this.spending = data.spending;
        this.renderUsers();
        this.renderLogUserFilters();
        if (this.generationWorkspaces !== null) this.renderGenerationWorkspaceFilter();
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
            <button class="icon-button" type="button" data-edit-user="${user.id}" title="编辑用户" aria-label="编辑用户"><i data-lucide="pencil"></i></button>
            <button class="icon-button" type="button" data-balance-user="${user.id}" title="调整余额" aria-label="调整余额"><i data-lucide="wallet-cards"></i></button>
            <button class="icon-button" type="button" data-reset-user="${user.id}" title="重置密码" aria-label="重置密码"><i data-lucide="key-round"></i></button>
            <button class="icon-button ${user.status === "active" ? "danger" : "accent"}" type="button" data-status-user="${user.id}" data-next-status="${user.status === "active" ? "disabled" : "active"}" title="${user.status === "active" ? "停用账户" : "启用账户"}" aria-label="${user.status === "active" ? "停用账户" : "启用账户"}"><i data-lucide="${user.status === "active" ? "user-x" : "user-check"}"></i></button>
          </div></td>
        </tr>`).join("");
      UI.icons(this.el.userTableBody);
    }

    renderLogUserFilters() {
      const runtimeValue = this.el.runtimeLogUserFilter.value;
      const auditValue = this.el.auditLogUserFilter.value;
      const options = this.users.map((user) => `<option value="${user.id}">${UI.escapeHtml(user.display_name || user.username)} · ${UI.escapeHtml(user.username)}</option>`).join("");
      const admins = this.users.filter((user) => user.role === "admin").map((user) => `<option value="${user.id}">${UI.escapeHtml(user.display_name || user.username)} · ${UI.escapeHtml(user.username)}</option>`).join("");
      this.el.runtimeLogUserFilter.innerHTML = `<option value="">全部用户</option>${options}`;
      this.el.auditLogUserFilter.innerHTML = `<option value="">全部管理员</option>${admins}`;
      this.el.runtimeLogUserFilter.value = runtimeValue;
      this.el.auditLogUserFilter.value = auditValue;
    }

    userById(id) {
      return this.users.find((user) => user.id === Number(id));
    }

    async handleUserAction(event) {
      const edit = event.target.closest("[data-edit-user]");
      if (edit) {
        const user = this.userById(edit.dataset.editUser);
        this.el.editUserDialogTitle.textContent = `编辑 ${user.display_name || user.username}`;
        this.el.editUserForm.elements.user_id.value = user.id;
        this.el.editUserForm.elements.display_name.value = user.display_name || "";
        this.el.editUserForm.elements.generation_concurrency.value = user.generation_concurrency;
        UI.openDialog(this.el.editUserDialog);
        return;
      }
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

    openCreateUserDialog() {
      this.el.userForm.reset();
      this.el.userForm.elements.generation_concurrency.value =
        this.systemConfig?.runtime?.default_user_concurrency ?? 2;
      UI.openDialog(this.el.userDialog);
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

    async updateUser(event) {
      event.preventDefault();
      const form = Object.fromEntries(new FormData(this.el.editUserForm));
      const submit = this.el.editUserForm.querySelector('[type="submit"]');
      submit.disabled = true;
      try {
        await UI.api(`/api/admin/users/${form.user_id}`, {
          method: "PUT",
          body: {
            display_name: form.display_name,
            generation_concurrency: Number(form.generation_concurrency),
          },
        });
        UI.closeDialog(this.el.editUserDialog);
        await this.loadUsers();
        UI.toast("用户设置已更新", "success");
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
      if (notify) {
        this.el.refreshJobsButton.disabled = true;
        this.el.refreshJobsButton.classList.add("is-loading");
        this.el.refreshJobsButton.setAttribute("aria-busy", "true");
      }
      try {
        const wasInitialized = this.jobsInitialized;
        const previous = new Map(this.jobs.map((job) => [String(job.id), this.jobMotionSignature(job)]));
        const params = new URLSearchParams({ limit: "100" });
        if (this.el.generationUserFilter.value) params.set("user_id", this.el.generationUserFilter.value);
        if (this.el.generationWorkspaceFilter.value) params.set("workspace_id", this.el.generationWorkspaceFilter.value);
        const data = await UI.api(`/api/admin/generations?${params.toString()}`);
        this.jobs = data.jobs;
        this.el.queueOverview.textContent = `生成中 ${data.running_images} 张 · 排队 ${data.queued_images} 张 · 共 ${data.jobs.length} 条记录`;
        this.renderJobs(previous, wasInitialized);
        this.jobsInitialized = true;
        if (notify) UI.toast("记录已刷新", "success");
      } catch (error) {
        UI.toast(error.message, "error");
      } finally {
        if (notify) {
          this.el.refreshJobsButton.disabled = false;
          this.el.refreshJobsButton.classList.remove("is-loading");
          this.el.refreshJobsButton.removeAttribute("aria-busy");
        }
      }
    }

    async loadGenerationFilters() {
      if (this.generationWorkspaces !== null) return;
      try {
        const data = await UI.api("/api/admin/generation-filters");
        this.generationWorkspaces = data.workspaces || [];
        this.el.generationUserFilter.innerHTML = [
          '<option value="">全部用户</option>',
          ...(data.users || []).map((user) => `<option value="${UI.escapeHtml(String(user.id))}">${UI.escapeHtml(user.display_name || user.username)} · ${UI.escapeHtml(user.username)}</option>`),
        ].join("");
        this.renderGenerationWorkspaceFilter();
      } catch (error) {
        UI.toast(error.message, "error");
      }
    }

    renderGenerationWorkspaceFilter() {
      const userId = this.el.generationUserFilter.value;
      const currentWorkspaceId = this.el.generationWorkspaceFilter.value;
      const availableWorkspaces = this.generationWorkspaces || [];
      const workspaces = userId
        ? availableWorkspaces.filter((workspace) => String(workspace.user_id) === userId)
        : availableWorkspaces;
      this.el.generationWorkspaceFilter.innerHTML = [
        '<option value="">全部工作站</option>',
        ...workspaces.map((workspace) => {
          const owner = this.users.find((user) => user.id === workspace.user_id);
          const ownerLabel = owner ? ` · ${owner.display_name || owner.username}` : "";
          return `<option value="${UI.escapeHtml(workspace.id)}">${UI.escapeHtml(workspace.name)}${UI.escapeHtml(ownerLabel)}</option>`;
        }),
      ].join("");
      const canKeepSelection = workspaces.some((workspace) => workspace.id === currentWorkspaceId);
      this.el.generationWorkspaceFilter.value = canKeepSelection ? currentWorkspaceId : "";
    }

    async applyGenerationFilters(event) {
      event.preventDefault();
      await this.loadJobs(true);
    }

    async clearGenerationFilters() {
      this.el.generationUserFilter.value = "";
      this.el.generationWorkspaceFilter.value = "";
      this.renderGenerationWorkspaceFilter();
      await this.loadJobs(true);
    }

    selectLogMode(mode) {
      if (!['runtime', 'audit'].includes(mode) || mode === this.logMode) return;
      this.logMode = mode;
      this.el.logModeButtons.forEach((button) => button.classList.toggle("active", button.dataset.logMode === mode));
      this.el.runtimeLogFilterForm.hidden = mode !== "runtime";
      this.el.auditLogFilterForm.hidden = mode !== "audit";
      this.el.runtimeLogTable.hidden = mode !== "runtime";
      this.el.auditLogTable.hidden = mode !== "audit";
      this.loadLogs();
    }

    async applyLogFilters(event) {
      event.preventDefault();
      this.logOffsets[this.logMode] = 0;
      await this.loadLogs(true);
    }

    async clearLogFilters(mode) {
      const form = mode === "audit" ? this.el.auditLogFilterForm : this.el.runtimeLogFilterForm;
      form.reset();
      this.logOffsets[mode] = 0;
      if (mode === this.logMode) await this.loadLogs(true);
    }

    async changeLogPage(direction) {
      const next = Math.max(0, this.logOffsets[this.logMode] + direction * this.logPageSize);
      if (next === this.logOffsets[this.logMode] || next >= this.logTotal && direction > 0) return;
      this.logOffsets[this.logMode] = next;
      await this.loadLogs(true);
    }

    async loadLogs(notify = true) {
      if (this.logsLoading) {
        this.logsReloadPending = true;
        this.logsReloadNotify = this.logsReloadNotify || notify;
        return;
      }
      this.logsLoading = true;
      this.el.refreshLogsButton.classList.add("is-loading");
      try {
        const mode = this.logMode;
        const form = mode === "audit" ? this.el.auditLogFilterForm : this.el.runtimeLogFilterForm;
        const params = new URLSearchParams({
          limit: String(this.logPageSize),
          offset: String(this.logOffsets[mode]),
        });
        for (const [name, value] of new FormData(form)) {
          if (String(value).trim()) params.set(name, String(value).trim());
        }
        const endpoint = mode === "audit" ? "audit-logs" : "runtime-logs";
        const data = await UI.api(`/api/admin/${endpoint}?${params.toString()}`);
        if (mode !== this.logMode || this.logsReloadPending) return;
        this.logs = data.logs || [];
        this.logTotal = Number(data.total || 0);
        this.lastLogPoll = Date.now();
        this.renderLogs();
      } catch (error) {
        if (notify) UI.toast(error.message, "error");
      } finally {
        this.logsLoading = false;
        this.el.refreshLogsButton.classList.remove("is-loading");
        if (this.logsReloadPending) {
          const pendingNotify = this.logsReloadNotify;
          this.logsReloadPending = false;
          this.logsReloadNotify = false;
          void this.loadLogs(pendingNotify);
        }
      }
    }

    renderLogs() {
      if (this.logMode === "audit") this.renderAuditLogs();
      else this.renderRuntimeLogs();
      const offset = this.logOffsets[this.logMode];
      const first = this.logTotal ? offset + 1 : 0;
      const last = Math.min(offset + this.logs.length, this.logTotal);
      this.el.logOverview.textContent = `${this.logMode === "audit" ? "操作审计" : "运行日志"} · ${this.logTotal} 条`;
      this.el.logPageSummary.textContent = `${first}-${last} / ${this.logTotal}`;
      this.el.previousLogPage.disabled = offset === 0;
      this.el.nextLogPage.disabled = offset + this.logs.length >= this.logTotal;
    }

    renderRuntimeLogs() {
      if (!this.logs.length) {
        this.el.runtimeLogTableBody.innerHTML = '<tr><td colspan="7" class="log-empty-cell">暂无运行日志</td></tr>';
        return;
      }
      this.el.runtimeLogTableBody.innerHTML = this.logs.map((log) => {
        const elapsed = log.elapsed_seconds == null ? "--" : `${Number(log.elapsed_seconds).toFixed(3)}s`;
        const user = log.user_label || (log.user_id ? `用户 #${log.user_id}` : "系统");
        const workspace = log.workspace_label || log.workspace_id || "--";
        const provider = log.provider_label || log.provider_id || "--";
        const model = log.model || "--";
        const error = log.error_code || (log.http_status ? `HTTP ${log.http_status}` : "--");
        return `<tr>
          <td><span class="log-primary">${UI.dateTime(log.created_at)}</span><small class="log-secondary">${UI.escapeHtml(log.source)}</small></td>
          <td><span class="status-badge ${log.status === "error" ? "failed" : "succeeded"}"><span></span>${LOG_STATUS[log.status] || UI.escapeHtml(log.status)}</span></td>
          <td><span class="log-primary">${UI.escapeHtml(LOG_CATEGORY[log.category] || log.category)}</span><small class="log-secondary">${UI.escapeHtml(log.event)}</small></td>
          <td><span class="log-primary">${UI.escapeHtml(user)}</span><small class="log-secondary">${UI.escapeHtml(workspace)}</small></td>
          <td><span class="log-primary">${UI.escapeHtml(provider)}</span><small class="log-secondary">${UI.escapeHtml(model)}</small></td>
          <td><span class="log-primary ${log.status === "error" ? "log-error" : ""}">${UI.escapeHtml(error)}</span><small class="log-secondary">${UI.escapeHtml(elapsed)}</small></td>
          <td class="actions-cell"><button class="icon-button" type="button" data-log-detail="${UI.escapeHtml(log.id)}" data-log-kind="runtime" title="查看详情" aria-label="查看详情"><i data-lucide="eye"></i></button></td>
        </tr>`;
      }).join("");
      UI.icons(this.el.runtimeLogTableBody);
    }

    renderAuditLogs() {
      if (!this.logs.length) {
        this.el.auditLogTableBody.innerHTML = '<tr><td colspan="5" class="log-empty-cell">暂无操作审计</td></tr>';
        return;
      }
      this.el.auditLogTableBody.innerHTML = this.logs.map((log) => `<tr>
        <td><span class="log-primary">${UI.dateTime(log.created_at)}</span><small class="log-secondary">#${log.id}</small></td>
        <td><span class="log-primary">${UI.escapeHtml(log.actor_label)}</span><small class="log-secondary">${log.actor_user_id ? `用户 #${log.actor_user_id}` : "系统"}</small></td>
        <td><span class="log-primary">${UI.escapeHtml(log.action)}</span></td>
        <td><span class="log-primary">${UI.escapeHtml(log.target_type)}</span><small class="log-secondary">${UI.escapeHtml(log.target_id)}</small></td>
        <td class="actions-cell"><button class="icon-button" type="button" data-log-detail="${log.id}" data-log-kind="audit" title="查看详情" aria-label="查看详情"><i data-lucide="eye"></i></button></td>
      </tr>`).join("");
      UI.icons(this.el.auditLogTableBody);
    }

    handleLogAction(event) {
      const button = event.target.closest("[data-log-detail]");
      if (button) this.openLogDetail(button.dataset.logKind, button.dataset.logDetail);
    }

    async openLogDetail(kind, id) {
      try {
        const endpoint = kind === "audit" ? "audit-logs" : "runtime-logs";
        const data = await UI.api(`/api/admin/${endpoint}/${encodeURIComponent(id)}`);
        const log = data.log;
        const fields = kind === "audit"
          ? [
              ["日志 ID", log.id], ["时间", UI.dateTime(log.created_at)], ["管理员", log.actor_label],
              ["操作", log.action], ["对象类型", log.target_type], ["对象 ID", log.target_id],
            ]
          : [
              ["日志 ID", log.id], ["时间", UI.dateTime(log.created_at)], ["状态", LOG_STATUS[log.status] || log.status],
              ["分类", LOG_CATEGORY[log.category] || log.category], ["事件", log.event], ["来源", log.source],
              ["用户", log.user_label || log.user_id], ["工作站", log.workspace_label || log.workspace_id],
              ["任务 ID", log.job_id], ["条目 ID", log.item_id], ["渠道", log.provider_label || log.provider_id],
              ["模型", log.model], ["错误码", log.error_code], ["HTTP 状态", log.http_status],
              ["上游请求 ID", log.upstream_request_id], ["耗时", log.elapsed_seconds == null ? "" : `${Number(log.elapsed_seconds).toFixed(3)}s`],
            ];
        this.el.logDetailKind.textContent = kind === "audit" ? "Audit Log" : "Runtime Log";
        this.el.logDetailTitle.textContent = log.message || log.action || "日志详情";
        this.el.logDetailMeta.innerHTML = fields.filter(([, value]) => value !== "" && value != null).map(([label, value]) => `<div><dt>${UI.escapeHtml(label)}</dt><dd>${UI.escapeHtml(value)}</dd></div>`).join("");
        this.el.logDetailData.textContent = JSON.stringify(log.details || {}, null, 2);
        UI.openDialog(this.el.logDetailDialog);
      } catch (error) {
        UI.toast(error.message, "error");
      }
    }

    jobMotionSignature(job) {
      return JSON.stringify([
        job.status,
        job.succeeded_count,
        job.charged_rmb,
        job.queue_position,
        job.items.map((item) => [item.id, item.status, item.thumbnail_url, item.image_url]),
      ]);
    }

    renderJobs(previous = new Map(), wasInitialized = false) {
      if (!this.jobs.length) {
        this.el.adminJobList.innerHTML = '<div class="admin-empty"><i data-lucide="activity"></i><span>暂无生成记录</span></div>';
        UI.icons(this.el.adminJobList);
        return;
      }
      this.el.adminJobList.innerHTML = this.jobs.map((job) => {
        const identifier = String(job.id);
        const previousSignature = previous.get(identifier);
        const motionClass = wasInitialized && previousSignature == null
          ? " data-enter"
          : previousSignature && previousSignature !== this.jobMotionSignature(job) ? " data-updated" : "";
        const outputs = job.items.map((item) => item.thumbnail_url
          ? `<a class="admin-output" href="${item.image_url}" target="_blank" title="查看原图"><img src="${item.thumbnail_url}" alt="生成结果"></a>`
          : `<span class="admin-output empty ${item.status}"><i data-lucide="${item.status === "failed" ? "circle-alert" : "loader-circle"}"></i></span>`).join("");
        const references = job.references.length
          ? `<div class="admin-references"><span>垫图</span>${job.references.map((asset) => `<a href="${asset.url}" target="_blank"><img src="${asset.url}" alt="${UI.escapeHtml(asset.name)}"></a>`).join("")}</div>`
          : "";
        return `<article class="admin-job-card ${job.status}${motionClass}" data-job-id="${UI.escapeHtml(identifier)}">
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
        UI.toast("任务已取消", "success");
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
      this.setChecks(form, "format", capabilities.formats || ["png"], ["png", "jpeg", "webp"]);
      form.elements.sizes.value = (capabilities.sizes || ["1024x1024"]).join(", ");
      form.elements.max_reference_images.value = capabilities.max_reference_images ?? 1;
      form.elements.max_reference_image_mb.value = capabilities.max_reference_image_mb ?? 10;
      form.elements.max_reference_total_mb.value = capabilities.max_reference_total_mb ?? 40;
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
          formats: this.readChecks(form, "format", ["png", "jpeg", "webp"]),
          sizes: form.elements.sizes.value.split(",").map((value) => value.trim()).filter(Boolean),
          max_reference_images: Number(form.elements.max_reference_images.value),
          max_reference_image_mb: Number(form.elements.max_reference_image_mb.value),
          max_reference_total_mb: Number(form.elements.max_reference_total_mb.value),
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
          <td><div class="capability-list"><span>超限直接截断较早内容</span><span>历史图片和生成结果优先</span></div></td>
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

    openWorkspacePromptsDialog() {
      const prompts = this.chatConfig?.workspace_prompts;
      const systemPrompts = this.chatConfig?.system_prompts;
      if (!prompts || !systemPrompts) return;
      this.el.workspacePromptsForm.elements.chat.value = systemPrompts.chat;
      this.el.workspacePromptsForm.elements.image.value = prompts.image;
      this.el.workspacePromptsForm.elements.animation.value = prompts.animation;
      UI.openDialog(this.el.workspacePromptsDialog);
    }

    async saveWorkspacePrompts(event) {
      event.preventDefault();
      const submit = this.el.workspacePromptsForm.querySelector('[type="submit"]');
      submit.disabled = true;
      try {
        const next = copy(this.chatConfig);
        next.system_prompts = {
          chat: this.el.workspacePromptsForm.elements.chat.value.trim(),
        };
        next.workspace_prompts = {
          image: this.el.workspacePromptsForm.elements.image.value.trim(),
          animation: this.el.workspacePromptsForm.elements.animation.value.trim(),
        };
        await this.persistChatModels(next, "提示词策略已更新");
        UI.closeDialog(this.el.workspacePromptsDialog);
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
        body: {
          revision: this.chatConfig.revision,
          system_prompts: config.system_prompts,
          workspace_prompts: config.workspace_prompts,
          context: config.context,
          models: config.models,
        },
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

    applySystemConfig(data) {
      this.systemConfig = data;
      this.el.siteTitleInput.value = data.site_title;
      this.el.versionInput.value = data.version;
      Object.entries(data.runtime || {}).forEach(([name, value]) => {
        const input = this.el.settingsForm.elements[name];
        if (!input) return;
        if (input.type === "checkbox") input.checked = value === true;
        else input.value = value;
      });
      if (!this.el.userDialog.open) {
        this.el.userForm.elements.generation_concurrency.value =
          data.runtime?.default_user_concurrency ?? 2;
      }
    }

    async loadSettings(notify = true) {
      try {
        const data = await UI.api("/api/admin/settings");
        this.applySystemConfig(data);
      } catch (error) {
        if (notify) UI.toast(error.message, "error");
      }
    }

    async saveSettings(event) {
      event.preventDefault();
      const submit = this.el.settingsForm.querySelector('[type="submit"]');
      submit.disabled = true;
      try {
        const runtime = {};
        Object.keys(this.systemConfig?.runtime || {}).forEach((name) => {
          const input = this.el.settingsForm.elements[name];
          runtime[name] = input.type === "checkbox" ? input.checked : Number(input.value);
        });
        const data = await UI.api("/api/admin/settings", {
          method: "PUT",
          body: {
            site_title: this.el.siteTitleInput.value,
            revision: this.systemConfig?.revision || "",
            runtime,
          },
        });
        this.applySystemConfig(data);
        document.querySelectorAll("[data-site-title]").forEach((node) => { node.textContent = data.site_title; });
        document.title = `管理后台 · ${data.site_title}`;
        UI.toast("系统设置已更新", "success");
      } catch (error) {
        UI.toast(error.message, "error");
      } finally {
        submit.disabled = false;
      }
    }
  }

  document.addEventListener("DOMContentLoaded", () => new AdminApp());
})();
