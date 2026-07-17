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
          const operation = this.chatOperations.get(workspace.id);
          return [
            workspace.id,
            workspace.name,
            workspace.kind,
            operation?.kind || "",
            operation?.label || "",
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
          const operation = this.chatOperations.get(workspace.id);
          const item = document.createElement("div");
          item.className = `workspace-item${workspace.id === this.activeWorkspace?.id ? " active" : ""}${operation ? " waiting" : ""}`;
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
          const workspaceIcon = workspace.kind === "animation" ? "film" : "image";
          icon.innerHTML = `<i data-lucide="${operation ? "loader-circle" : workspaceIcon}"></i>`;
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
          remove.disabled = Boolean(operation);
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
          this.updateWorkspaceJobDisplay(item, workspace, operation);
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
          this.updateWorkspaceJobDisplay(item, workspace, this.chatOperations.get(workspace.id));
        }
      });
    },

    updateWorkspaceJobDisplay(item, workspace, operation) {
      const job = this.workspaceJobs.get(workspace.id);
      const elements = this.workspaceElementCache.get(item);
      if (!elements) return;
      const { meta, progress, progressFill, timing, endLabel, remainingLabel } = elements;
      ["queued", "running", "canceling"].forEach((status) => {
        item.classList.toggle(`job-${status}`, job?.status === status && !operation);
      });
      if (operation) {
        setHidden(progress, true);
        setHidden(timing, true);
        setText(meta, operation.label);
        setAttribute(meta, "title", operation.label);
        return;
      }
      if (!job) {
        setHidden(progress, true);
        setHidden(timing, true);
        const typeLabel = workspace.kind === "animation" ? "帧动画" : "图片";
        setText(meta, typeLabel);
        setAttribute(meta, "title", typeLabel);
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

    async selectWorkspace(id, { knownEmpty = false } = {}) {
      const workspace = this.workspaces.find((item) => item.id === id);
      if (!workspace || workspace === this.activeWorkspace) return;
      const selection = ++this.workspaceLoadSequence;
      if (this.activeWorkspace) {
        this.chatDrafts.set(this.activeWorkspace.id, this.el.chatInput.value);
        await this.flushSettings();
        if (selection !== this.workspaceLoadSequence) return;
        await this.animateWorkspaceOut();
        if (selection !== this.workspaceLoadSequence) return;
      }
      this.activeWorkspace = workspace;
      this.saveLastWorkspaceId(workspace.id);
      this.jobs = [];
      this.messages = [];
      this.conversationContext = null;
      this.chatReferencePickerOpen = false;
      this.setWorkspaceLoading(!knownEmpty, selection);
      this.el.chatInput.value = this.chatDrafts.get(workspace.id) || "";
      this.renderWorkspaceList();
      this.el.workspaceTitle.textContent = workspace.name;
      this.applyWorkspaceSettings();
      this.renderReferences();
      this.renderChatReferences();
      this.renderJobs();
      this.renderMessages();
      this.setComposerMode("chat");
      this.animateWorkspaceIn();
      if (!knownEmpty) await Promise.all([this.loadJobs(), this.loadMessages()]);
      if (selection !== this.workspaceLoadSequence) return;
      if (!knownEmpty) {
        this.setWorkspaceLoading(false, selection);
        this.scrollConversation(true);
      }
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
      const selection = ++this.workspaceLoadSequence;
      this.activeWorkspace = null;
      this.saveLastWorkspaceId(null);
      this.jobs = [];
      this.messages = [];
      this.conversationContext = null;
      this.el.workspaceTitle.textContent = "暂无工作站";
      this.renderWorkspaceList();
      this.setComposerMode("chat");
      this.setWorkspaceLoading(false, selection);
      this.updateMetrics();
    },

    setWorkspaceLoading(loading, selection) {
      window.clearTimeout(this.workspaceSkeletonTimer);
      this.workspaceLoading = loading;
      this.el.conversationLoading.hidden = true;
      this.el.conversationScroll.toggleAttribute("aria-busy", loading);
      this.el.messageList.hidden = loading;
      if (loading) {
        this.updateInteractionState();
        this.el.conversationEmpty.hidden = true;
        this.workspaceSkeletonTimer = window.setTimeout(() => {
          if (this.workspaceLoading && selection === this.workspaceLoadSequence) {
            this.el.conversationLoading.hidden = false;
          }
        }, 120);
        return;
      }
      this.renderMessages();
      if (!this.reducedMotion.matches) {
        const content = this.el.messageList.childElementCount
          ? this.el.messageList
          : this.el.conversationEmpty;
        content.animate?.(
          [
            { opacity: 0, transform: "translateY(5px)" },
            { opacity: 1, transform: "translateY(0)" },
          ],
          { duration: 180, easing: "cubic-bezier(.22, 1, .36, 1)" },
        );
      }
    },

    async animateWorkspaceOut() {
      if (this.reducedMotion.matches || typeof this.el.conversationView.animate !== "function") return;
      this.workspaceTransition?.cancel();
      const animation = this.el.conversationView.animate(
        [
          { opacity: 1, transform: "translateY(0)" },
          { opacity: 0.28, transform: "translateY(-4px)" },
        ],
        { duration: 90, easing: "ease-out", fill: "forwards" },
      );
      this.workspaceTransition = animation;
      await animation.finished.catch(() => {});
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
