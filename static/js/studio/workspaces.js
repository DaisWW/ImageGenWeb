(() => {
  "use strict";

  const {
    StudioApp,
    UI,
    STATUS,
    setText,
    setHidden,
    setAttribute,
  } = window.ImageGenStudio;

  Object.assign(StudioApp.prototype, {
    renderWorkspaceList() {
      const signature = JSON.stringify([
        this.activeWorkspace?.id || "",
        this.maxWorkspaces,
        this.workspaceOrderSaving,
        ...this.workspaces.map((workspace) => {
          const operations = this.chatOperationList(workspace.id);
          const jobs = this.workspaceJobList(workspace.id);
          return [
            workspace.id,
            workspace.name,
            workspace.kind,
            operations
              .sort((left, right) => this.operationKey(left).localeCompare(this.operationKey(right)))
              .map((operation) => [
                this.operationKey(operation),
                operation.kind,
                operation.stage,
                operation.stage_label || operation.label,
              ]),
            ...jobs
              .sort((left, right) => String(left.id).localeCompare(String(right.id)))
              .map((job) => [
                job.id,
                job.status,
                job.progress_percent,
                job.queue_position,
                job.estimated_end_at,
              ]),
          ];
        }),
      ]);
      if (signature === this.workspaceListSignature) {
        this.updateWorkspaceJobDisplays();
        return;
      }
      this.workspaceListSignature = signature;
      this.el.workspaceList.replaceChildren(
        ...this.workspaces.map((workspace) => {
          const operations = this.chatOperationList(workspace.id);
          const hasOperation = operations.length > 0;
          const jobs = this.workspaceJobList(workspace.id);
          const item = document.createElement("div");
          item.className = `workspace-item${workspace.id === this.activeWorkspace?.id ? " active" : ""}${hasOperation ? " waiting" : ""}`;
          item.dataset.workspaceId = workspace.id;
          item.dataset.workspaceKind = workspace.kind;
          const drag = document.createElement("button");
          drag.type = "button";
          drag.className = "workspace-drag-handle";
          drag.dataset.dragWorkspace = workspace.id;
          drag.disabled = this.workspaceOrderSaving || this.workspaces.length < 2;
          drag.draggable = !drag.disabled;
          drag.tabIndex = -1;
          drag.title = `拖拽调整“${workspace.name}”顺序`;
          drag.setAttribute("aria-label", drag.title);
          drag.innerHTML = '<i data-lucide="grip-vertical"></i>';
          const select = document.createElement("button");
          select.type = "button";
          select.className = "workspace-select";
          select.dataset.selectWorkspace = workspace.id;
          select.setAttribute("aria-current", workspace.id === this.activeWorkspace?.id ? "true" : "false");
          const icon = document.createElement("span");
          icon.className = "workspace-icon";
          const workspaceIcon = workspace.kind === "image" ? "image" : "box";
          icon.innerHTML = `<i data-lucide="${hasOperation ? "loader-circle" : workspaceIcon}"></i>`;
          const copy = document.createElement("span");
          copy.className = "workspace-copy";
          const name = document.createElement("strong");
          name.textContent = workspace.name;
          const meta = document.createElement("small");
          meta.className = "workspace-meta";
          const progress = document.createElement("span");
          progress.className = "workspace-job-progress";
          progress.setAttribute("role", "progressbar");
          progress.setAttribute("aria-label", "生成进度");
          progress.append(document.createElement("i"));
          const timing = document.createElement("small");
          timing.className = "workspace-job-timing";
          const endLabel = document.createElement("span");
          const remainingLabel = document.createElement("span");
          timing.append(endLabel, remainingLabel);
          copy.append(name, meta, progress, timing);
          select.append(icon, copy);
          const actions = document.createElement("span");
          actions.className = "workspace-actions";
          const rename = document.createElement("button");
          rename.type = "button";
          rename.className = "workspace-action";
          rename.dataset.renameWorkspace = workspace.id;
          rename.title = `重命名“${workspace.name}”`;
          rename.setAttribute("aria-label", rename.title);
          rename.innerHTML = '<i data-lucide="pencil"></i>';
          const remove = document.createElement("button");
          remove.type = "button";
          remove.className = "workspace-action danger";
          remove.dataset.deleteWorkspace = workspace.id;
          remove.disabled = Boolean(
            hasOperation || jobs.length || this.workspaceHasGenerationSubmission(workspace.id),
          );
          remove.title = `删除“${workspace.name}”`;
          remove.setAttribute("aria-label", remove.title);
          remove.innerHTML = '<i data-lucide="trash-2"></i>';
          actions.append(rename, remove);
          item.append(drag, select, actions);
          this.workspaceElementCache.set(item, {
            meta,
            progress,
            progressFill: progress.firstElementChild,
            timing,
            endLabel,
            remainingLabel,
          });
          this.updateWorkspaceJobDisplay(item, workspace, operations);
          return item;
        }),
      );
      this.el.workspaceCount.textContent = `${this.workspaces.length} / ${this.maxWorkspaces}`;
      this.el.newWorkspaceButton.disabled = this.workspaces.length >= this.maxWorkspaces;
      UI.icons(this.el.workspaceList);
    },

    updateWorkspaceJobDisplays() {
      const workspaces = new Map(this.workspaces.map((workspace) => [workspace.id, workspace]));
      this.el.workspaceList.querySelectorAll(".workspace-item").forEach((item) => {
        const workspace = workspaces.get(item.dataset.workspaceId);
        if (workspace) {
          this.updateWorkspaceJobDisplay(item, workspace, this.chatOperationList(workspace.id));
        }
      });
      this.updateChatOperationDisplays();
    },

    updateWorkspaceJobDisplay(item, workspace, operations = this.chatOperationList(workspace.id)) {
      const operation = this.workspacePrimaryChatOperation(workspace.id);
      const chatOperations = operations.filter((item) => (
        ["reply", "prompt_draft"].includes(item.kind)
      ));
      const jobs = this.workspaceJobList(workspace.id);
      const statusPriority = { running: 0, reconnecting: 1, canceling: 2, queued: 3 };
      const job = jobs
        .slice()
        .sort((left, right) => (
          (statusPriority[left.status] ?? 99) - (statusPriority[right.status] ?? 99)
          || String(left.created_at || "").localeCompare(String(right.created_at || ""))
        ))[0];
      const elements = this.workspaceElementCache.get(item);
      if (!elements) return;
      const { meta, progress, progressFill, timing, endLabel, remainingLabel } = elements;
      ["queued", "running", "reconnecting", "canceling"].forEach((status) => {
        item.classList.toggle(`job-${status}`, job?.status === status);
      });
      if (operation) {
        const baseLabel = this.chatOperationAwaitingMessageAcceptance(operation, workspace.id)
          ? "正在发送消息"
          : operation.stage_label || operation.label;
        const operationCountLabel = operations.length > 1
          ? chatOperations.length === operations.length
            ? `${chatOperations.length} 个对话请求`
            : `${operations.length} 个活动请求`
          : "";
        const operationLabel = [
          baseLabel,
          operationCountLabel,
          jobs.length ? `${jobs.length} 个生成任务` : "",
        ].filter(Boolean).join(" · ");
        setHidden(progress, true);
        setHidden(timing, true);
        setText(meta, operationLabel);
        setAttribute(meta, "title", operationLabel);
        return;
      }
      if (!jobs.length) {
        setHidden(progress, true);
        setHidden(timing, true);
        const typeLabel = workspace.kind === "image" ? "图片" : "工作站";
        setText(meta, typeLabel);
        setAttribute(meta, "title", typeLabel);
        return;
      }
      if (jobs.length > 1) {
        const statusLabels = {
          running: "生成中",
          reconnecting: "重连中",
          canceling: "取消中",
          queued: "排队中",
        };
        const statusCounts = jobs.reduce((counts, current) => {
          counts[current.status] = (counts[current.status] || 0) + 1;
          return counts;
        }, {});
        const statusText = Object.entries(statusLabels)
          .filter(([status]) => statusCounts[status])
          .map(([status, label]) => `${statusCounts[status]} 个${label}`)
          .join("，");
        const jobsLabel = `${jobs.length} 个生成任务${statusText ? ` · ${statusText}` : ""}`;
        setHidden(progress, true);
        setHidden(timing, true);
        setText(meta, jobsLabel);
        setAttribute(meta, "title", jobsLabel);
        return;
      }
      if (job.status === "queued") {
        setHidden(progress, true);
        setHidden(timing, true);
        const position = Number(job.queue_position);
        const queueText = Number.isFinite(position)
          ? `排队中 · 前方 ${Math.max(0, position - 1)} 个`
          : "排队中 · 等待调度";
        setText(meta, queueText);
        setAttribute(meta, "title", queueText);
        return;
      }
      const percent = Math.min(100, Math.max(0, Number(job.progress_percent) || 0));
      const statusLabel = STATUS[job.status]?.[0] || "处理中";
      const metaText = `${statusLabel} ${percent}%`;
      setText(meta, metaText);
      setAttribute(meta, "title", metaText);
      setHidden(progress, false);
      setAttribute(progress, "aria-valuemin", "0");
      setAttribute(progress, "aria-valuemax", "100");
      setAttribute(progress, "aria-valuenow", percent);
      const progressWidth = `${percent}%`;
      if (progressFill.style.width !== progressWidth) progressFill.style.width = progressWidth;
      setHidden(timing, false);
      if (!job.estimated_end_at) {
        setText(endLabel, "正在估算结束时间");
        setHidden(remainingLabel, true);
      } else {
        const remaining = this.formatRemaining(job.estimated_end_at);
        setText(endLabel, `结束 ${UI.timeOnly(job.estimated_end_at)}`);
        setText(remainingLabel, remaining);
        setHidden(remainingLabel, false);
      }
      const timingTitle = [...timing.children]
        .filter((label) => !label.hidden)
        .map((label) => label.textContent)
        .join(" · ");
      setAttribute(timing, "title", timingTitle);
    },

    formatRemaining(value) {
      const milliseconds = new Date(value).getTime() - Date.now();
      if (!Number.isFinite(milliseconds) || milliseconds <= 0) return "仍在处理";
      const seconds = `${Math.ceil(milliseconds / 1000)}s`;
      return `剩余 ${seconds}`;
    },

    async handleWorkspaceListClick(event) {
      const rename = event.target.closest("[data-rename-workspace]");
      if (rename) {
        await this.renameWorkspace(rename.dataset.renameWorkspace);
        return;
      }
      const remove = event.target.closest("[data-delete-workspace]");
      if (remove) {
        await this.requestDeleteWorkspace(remove.dataset.deleteWorkspace);
        return;
      }
      const select = event.target.closest("[data-select-workspace]");
      if (select) await this.selectWorkspace(select.dataset.selectWorkspace);
    },

    handleWorkspaceDoubleClick(event) {
      const select = event.target.closest("[data-select-workspace]");
      if (select) this.renameWorkspace(select.dataset.selectWorkspace);
    },

    handleWorkspaceShortcut(event) {
      if (event.key !== "F2" || event.defaultPrevented || event.altKey
        || event.ctrlKey || event.metaKey || event.shiftKey || !this.activeWorkspace) return;
      const target = event.target;
      if (target instanceof HTMLElement
        && (target.isContentEditable || target.closest("input, textarea, select"))) return;
      if (document.querySelector("dialog[open]")) return;
      event.preventDefault();
      this.showWorkspaceDialog("rename");
    },

    async renameWorkspace(id) {
      await this.selectWorkspace(id);
      if (this.activeWorkspace?.id === id) this.showWorkspaceDialog("rename");
    },

    handleWorkspaceDragStart(event) {
      const handle = event.target.closest("[data-drag-workspace]");
      if (!handle || handle.disabled || this.workspaceOrderSaving) {
        event.preventDefault();
        return;
      }
      const item = handle.closest(".workspace-item");
      this.draggedWorkspaceId = handle.dataset.dragWorkspace;
      if (event.dataTransfer) {
        event.dataTransfer.effectAllowed = "move";
        event.dataTransfer.setData("text/plain", this.draggedWorkspaceId);
        event.dataTransfer.setDragImage(item, 18, Math.round(item.offsetHeight / 2));
      }
      window.requestAnimationFrame(() => item.classList.add("dragging"));
    },

    handleWorkspaceDragOver(event) {
      if (!this.draggedWorkspaceId) return;
      const item = event.target.closest(".workspace-item");
      this.clearWorkspaceDropIndicators();
      if (!item || item.dataset.workspaceId === this.draggedWorkspaceId) return;
      event.preventDefault();
      if (event.dataTransfer) event.dataTransfer.dropEffect = "move";
      const bounds = item.getBoundingClientRect();
      const horizontal = window.matchMedia("(max-width: 720px)").matches;
      const after = horizontal
        ? event.clientX > bounds.left + bounds.width / 2
        : event.clientY > bounds.top + bounds.height / 2;
      item.classList.add(after ? "drop-after" : "drop-before");
    },

    async handleWorkspaceDrop(event) {
      if (!this.draggedWorkspaceId) return;
      const item = event.target.closest(".workspace-item");
      if (!item || item.dataset.workspaceId === this.draggedWorkspaceId) {
        this.clearWorkspaceDragState();
        return;
      }
      event.preventDefault();
      const workspaceId = this.draggedWorkspaceId;
      const targetId = item.dataset.workspaceId;
      const placeAfter = item.classList.contains("drop-after");
      this.clearWorkspaceDragState();
      await this.moveWorkspace(workspaceId, targetId, placeAfter);
    },

    async moveWorkspace(workspaceId, targetId, placeAfter) {
      if (workspaceId === targetId || this.workspaceOrderSaving) return;
      const previous = [...this.workspaces];
      const ordered = [...this.workspaces];
      const fromIndex = ordered.findIndex((workspace) => workspace.id === workspaceId);
      if (fromIndex < 0) return;
      const [workspace] = ordered.splice(fromIndex, 1);
      let targetIndex = ordered.findIndex((item) => item.id === targetId);
      if (targetIndex < 0) return;
      if (placeAfter) targetIndex += 1;
      ordered.splice(targetIndex, 0, workspace);
      this.workspaceOrderSaving = true;
      this.workspaces = ordered;
      this.renderWorkspaceList();
      try {
        await UI.api("/api/workspaces/order", {
          method: "PUT",
          body: { workspace_ids: ordered.map((item) => item.id) },
        });
      } catch (error) {
        this.workspaces = previous;
        UI.toast(error.message, "error");
      } finally {
        this.workspaceOrderSaving = false;
        this.renderWorkspaceList();
      }
    },

    async refreshWorkspaces() {
      try {
        const data = await UI.api("/api/workspaces", { cache: "no-store" });
        const previousById = new Map(this.workspaces.map((workspace) => [workspace.id, workspace]));
        const activeWorkspaceId = this.activeWorkspace?.id || "";
        const currentWorkspaceSettings = this.activeWorkspace
          ? this.collectSettings?.()
          : null;
        const preserveLocalSettings = Boolean(
          this.saveTimer !== null || this.workspaceSettingSaves.has(activeWorkspaceId),
        );
        this.workspaces = data.workspaces.map((workspace) => {
          const current = previousById.get(workspace.id);
          if (current) Object.assign(current, workspace);
          return current || workspace;
        });
        this.maxWorkspaces = data.max_count;
        const activeWorkspace = this.workspaces.find((workspace) => workspace.id === activeWorkspaceId);
        if (activeWorkspace) {
          this.activeWorkspace = activeWorkspace;
          this.el.workspaceTitle.textContent = activeWorkspace.name;
          if (currentWorkspaceSettings && preserveLocalSettings) {
            activeWorkspace.settings = { ...activeWorkspace.settings, ...currentWorkspaceSettings };
          }
          this.renderWorkspaceList();
        } else if (this.workspaces[0]) {
          this.activeWorkspace = null;
          this.selectWorkspace(this.workspaces[0].id);
        } else {
          this.showEmptyWorkspace();
        }
      } catch (error) {
        if (error?.name !== "AbortError") UI.toast(error.message, "error");
      }
    },

    clearWorkspaceDropIndicators() {
      this.el.workspaceList.querySelectorAll(".drop-before, .drop-after").forEach((item) => {
        item.classList.remove("drop-before", "drop-after");
      });
    },

    clearWorkspaceDragState() {
      this.clearWorkspaceDropIndicators();
      this.el.workspaceList.querySelector(".dragging")?.classList.remove("dragging");
      this.draggedWorkspaceId = null;
    },

    selectWorkspace(id, { knownEmpty = false } = {}) {
      const workspace = this.workspaces.find((item) => item.id === id);
      if (!workspace || workspace === this.activeWorkspace) return;
      const selection = ++this.workspaceLoadSequence;
      if (this.activeWorkspace) {
        const outgoingWorkspaceId = this.activeWorkspace.id;
        this.chatDrafts.set(outgoingWorkspaceId, this.el.chatInput.value);
        void this.flushSettings(outgoingWorkspaceId);
      }
      this.activeWorkspace = workspace;
      this.saveLastWorkspaceId(workspace.id);
      this.jobs = [];
      this.messages = [];
      this.conversationContext = null;
      this.chatReferencePickerOpen = false;
      this.el.chatInput.value = this.chatDrafts.get(workspace.id) || "";
      this.setComposerMode("chat");
      this.renderWorkspaceList();
      this.el.workspaceTitle.textContent = workspace.name;
      this.applyWorkspaceSettings();
      this.renderReferences();
      this.renderChatReferences();
      this.renderJobs();
      this.renderMessages();
      this.animateWorkspaceIn();
      if (knownEmpty) return;
      void Promise.all([
        this.loadJobs(workspace.id),
        this.loadMessages(workspace.id),
      ]).then(() => {
        if (selection === this.workspaceLoadSequence
          && this.activeWorkspace?.id === workspace.id) this.scrollConversation(true);
      });
    },

    loadLastWorkspaceId() {
      try {
        return window.localStorage.getItem(`imagegen:last-workspace:${this.user.id}`);
      } catch {
        return null;
      }
    },

    saveLastWorkspaceId(workspaceId) {
      try {
        const key = `imagegen:last-workspace:${this.user.id}`;
        if (workspaceId) window.localStorage.setItem(key, workspaceId);
        else window.localStorage.removeItem(key);
      } catch {
        // The app remains usable when browser storage is unavailable.
      }
    },

    showEmptyWorkspace() {
      this.workspaceLoadSequence += 1;
      this.activeWorkspace = null;
      this.saveLastWorkspaceId(null);
      this.jobs = [];
      this.messages = [];
      this.conversationContext = null;
      this.el.workspaceTitle.textContent = "暂无工作站";
      this.renderWorkspaceList();
      this.setComposerMode("chat");
      this.renderMessages();
      this.updateMetrics();
    },

    animateWorkspaceIn() {
      this.workspaceTransition?.cancel();
      if (this.reducedMotion.matches || typeof this.el.conversationView.animate !== "function") {
        this.workspaceTransition = null;
        return;
      }
      const animation = this.el.conversationView.animate(
        [
          { opacity: 0.35, transform: "translateY(6px)" },
          { opacity: 1, transform: "translateY(0)" },
        ],
        { duration: 220, easing: "cubic-bezier(.22, 1, .36, 1)" },
      );
      this.workspaceTransition = animation;
      animation.finished.catch(() => {}).finally(() => {
        if (this.workspaceTransition === animation) this.workspaceTransition = null;
      });
    },

  });
})();
