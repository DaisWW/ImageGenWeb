(() => {
  "use strict";

  const UI = window.ImageGen;
  const STATUS = {
    queued: ["排队中", "queued"],
    running: ["生成中", "running"],
    canceling: ["取消中", "canceling"],
    succeeded: ["已完成", "succeeded"],
    partial: ["部分完成", "partial"],
    failed: ["失败", "failed"],
    interrupted: ["已中断", "failed"],
    canceled: ["已取消", "canceled"],
  };
  const TERMINAL = new Set(["succeeded", "partial", "failed", "canceled"]);
  const ACTIVE_POLL_INTERVAL = 2200;
  const IDLE_POLL_INTERVAL = 8000;
  const IMAGE_SIZE_PATTERN = /^([1-9]\d{1,4})x([1-9]\d{1,4})$/;
  const IMAGE_DIMENSION_MIN = 64;
  const IMAGE_DIMENSION_MAX = 8192;
  const REFERENCE_IMAGE_TYPES = new Set(["image/png", "image/jpeg", "image/webp"]);
  const REFERENCE_IMAGE_EXTENSION = /\.(?:png|jpe?g|webp)$/i;
  const JOB_ELEMENT_SELECTOR = [
    "[data-job-status]",
    "[data-job-status-label]",
    "[data-job-queue]",
    "[data-job-time]",
    "[data-job-eta]",
    "[data-job-retry]",
    "[data-job-cancel]",
    "[data-job-progress]",
    "[data-job-prompt]",
    "[data-job-channel]",
    "[data-job-model]",
    "[data-job-size]",
    "[data-job-quality]",
    "[data-job-count]",
    "[data-job-charge]",
    "[data-animation-result]",
    "[data-animation-image]",
    "[data-animation-meta]",
    "[data-animation-download]",
    ".output-grid",
  ].join(",");

  const setText = (element, value) => {
    const next = String(value ?? "");
    if (element.textContent !== next) element.textContent = next;
  };
  const setHidden = (element, hidden) => {
    const next = Boolean(hidden);
    if (element.hidden !== next) element.hidden = next;
  };
  const setDisabled = (element, disabled) => {
    const next = Boolean(disabled);
    if (element.disabled !== next) element.disabled = next;
  };
  const setAttribute = (element, name, value) => {
    const next = String(value);
    if (element.getAttribute(name) !== next) element.setAttribute(name, next);
  };

  class StudioApp {
    constructor() {
      this.bootstrap = JSON.parse(document.getElementById("bootstrapData").textContent);
      this.brandMarkUrl = document.getElementById("studioApp").dataset.brandMarkUrl;
      this.user = this.bootstrap.user;
      this.workspaces = this.bootstrap.workspaces;
      this.limits = this.bootstrap.runtime_settings || {
        max_workspaces_per_user: this.bootstrap.max_workspaces,
        max_assets_per_workspace: 8,
        max_message_characters: 12000,
        max_chat_attachments: 8,
        max_attachment_mb: 10,
        max_attachment_total_mb: 40,
        max_prompt_characters: 8000,
        max_batch_images: 20,
        max_animation_frames: 20,
        max_animation_fps: 24,
      };
      this.maxWorkspaces = this.limits.max_workspaces_per_user;
      this.historyRetentionDays = this.bootstrap.history_retention_days;
      this.channels = this.bootstrap.channels;
      this.chatModels = this.bootstrap.chat_models || [];
      this.activeWorkspace = null;
      this.jobs = [];
      this.messages = [];
      this.conversationContext = null;
      this.referenceSelections = new Map();
      this.chatReferenceSelections = new Map();
      this.chatDrafts = new Map();
      this.chatOperations = new Map();
      this.pendingUserMessages = new Map();
      this.saveTimer = null;
      this.promptCounterTimer = null;
      this.workspaceSkeletonTimer = null;
      this.workspaceLoadSequence = 0;
      this.workspaceLoading = false;
      this.workspaceTransition = null;
      this.reducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)");
      this.loadingJobWorkspaces = new Map();
      this.loadingMessageWorkspaces = new Map();
      this.workspaceJobs = new Map();
      this.loadingWorkspaceJobs = null;
      this.referenceUploads = new Map();
      this.referenceUploadQueues = new Map();
      this.referenceUploadSequence = 0;
      this.dialogMode = "create";
      this.dialogWorkspaceKind = "image";
      this.workspaceDeleteId = null;
      this.draggedWorkspaceId = null;
      this.workspaceOrderSaving = false;
      this.uploadTarget = "generation";
      this.chatReferencePickerOpen = false;
      this.detailItemId = null;
      this.channelVersion = "";
      this.chatModelVersion = "";
      this.runtimeRevision = "";
      this.workspaceListSignature = "";
      this.workspaceElementCache = new WeakMap();
      this.jobElementCache = new WeakMap();
      this.polling = false;
      this.pollTimer = null;
      this.scrollFrame = null;
      this.workspaces.forEach((workspace) => {
        if (workspace.conversation_operation?.busy) {
          this.chatOperations.set(workspace.id, {
            ...workspace.conversation_operation,
            local: false,
          });
        }
      });
      this.cacheElements();
      this.applyRuntimeSettings(this.limits, this.historyRetentionDays);
      this.bindEvents();
      this.renderWorkspaceList();
      const lastWorkspaceId = this.loadLastWorkspaceId();
      const initialWorkspace = this.workspaces.find((workspace) => workspace.id === lastWorkspaceId)
        || this.workspaces[0];
      this.selectWorkspace(initialWorkspace?.id);
      this.loadWorkspaceJobs().finally(() => this.schedulePoll());
      this.loadChannels(false);
      this.loadChatModels(false);
      this.countdownTimer = window.setInterval(() => {
        if (document.hidden) return;
        this.updateWorkspaceJobDisplays();
        this.updateEtaMetric();
      }, 1000);
      this.channelTimer = window.setInterval(() => {
        if (document.hidden) return;
        this.loadChannels(false);
        this.loadChatModels(false);
        this.loadRuntimeSettings(false);
      }, 15000);
    }

    cacheElements() {
      const byId = (id) => document.getElementById(id);
      this.el = {
        workspaceList: byId("workspaceList"),
        workspaceCount: byId("workspaceCount"),
        retentionSummary: byId("retentionSummary"),
        workspaceTitle: byId("workspaceTitle"),
        workspaceStatus: byId("workspaceStatus"),
        workspaceStateDot: byId("workspaceStateDot"),
        runningMetric: byId("runningMetric"),
        queueMetric: byId("queueMetric"),
        etaMetric: byId("etaMetric"),
        etaRemainingMetric: byId("etaRemainingMetric"),
        newWorkspaceButton: byId("newWorkspaceButton"),
        clearWorkspaceButton: byId("clearWorkspaceButton"),
        workspaceDialog: byId("workspaceDialog"),
        workspaceForm: byId("workspaceForm"),
        workspaceDialogTitle: byId("workspaceDialogTitle"),
        workspaceNameInput: byId("workspaceNameInput"),
        workspaceKindControl: byId("workspaceKindControl"),
        workspaceKindSwitch: byId("workspaceKindSwitch"),
        workspaceClearDialog: byId("workspaceClearDialog"),
        workspaceClearForm: byId("workspaceClearForm"),
        workspaceClearName: byId("workspaceClearName"),
        workspaceDeleteDialog: byId("workspaceDeleteDialog"),
        workspaceDeleteForm: byId("workspaceDeleteForm"),
        workspaceDeleteName: byId("workspaceDeleteName"),
        conversationView: byId("conversationView"),
        conversationScroll: byId("conversationScroll"),
        conversationLoading: byId("conversationLoading"),
        conversationEmpty: byId("conversationEmpty"),
        messageList: byId("messageList"),
        chatForm: byId("chatForm"),
        chatModelSelect: byId("chatModelSelect"),
        translatePrompt: byId("translatePrompt"),
        contextStatus: byId("contextStatus"),
        draftPromptButton: byId("draftPromptButton"),
        chatReferenceStrip: byId("chatReferenceStrip"),
        chatReferenceList: byId("chatReferenceList"),
        chatReferenceButton: byId("chatReferenceButton"),
        chatReferenceCount: byId("chatReferenceCount"),
        chatInput: byId("chatInput"),
        chatSendButton: byId("chatSendButton"),
        generationForm: byId("generationForm"),
        generationBackButton: byId("generationBackButton"),
        generationHeadingTitle: byId("generationHeadingTitle"),
        generationHeadingSubtitle: byId("generationHeadingSubtitle"),
        modeSwitch: byId("modeSwitch"),
        channelSelect: byId("channelSelect"),
        modelSelect: byId("modelSelect"),
        sizeInput: byId("sizeInput"),
        sizeOptions: byId("sizeOptions"),
        qualitySelect: byId("qualitySelect"),
        formatSelect: byId("formatSelect"),
        transparentBackground: byId("transparentBackground"),
        transparentBackgroundControl: byId("transparentBackgroundControl"),
        frameFormatLabel: byId("frameFormatLabel"),
        imageCountControl: byId("imageCountControl"),
        batchCount: byId("batchCount"),
        animationControls: [...document.querySelectorAll(".animation-control")],
        animationFrameCount: byId("animationFrameCount"),
        animationFps: byId("animationFps"),
        animationFormat: byId("animationFormat"),
        animationLoop: byId("animationLoop"),
        referenceStrip: byId("referenceStrip"),
        referenceInput: byId("referenceInput"),
        referenceAdd: byId("referenceAdd"),
        referenceAddLabel: byId("referenceAddLabel"),
        referenceList: byId("referenceList"),
        referenceLimit: byId("referenceLimit"),
        promptInput: byId("promptInput"),
        promptCounter: byId("promptCounter"),
        priceEstimateLabel: byId("priceEstimateLabel"),
        priceEstimate: byId("priceEstimate"),
        saveState: byId("saveState"),
        generateButton: byId("generateButton"),
        generateButtonLabel: byId("generateButtonLabel"),
        imageDialog: byId("imageDialog"),
        detailImage: byId("detailImage"),
        detailList: byId("detailList"),
        detailPrompt: byId("detailPrompt"),
        detailReferences: byId("detailReferences"),
        detailReuse: byId("detailReuse"),
        detailReuseLabel: byId("detailReuseLabel"),
        detailDownload: byId("detailDownload"),
      };
    }

    applyRuntimeSettings(settings, historyRetentionDays) {
      this.limits = { ...this.limits, ...(settings || {}) };
      this.maxWorkspaces = this.limits.max_workspaces_per_user;
      if (Number.isFinite(Number(historyRetentionDays))) {
        this.historyRetentionDays = Number(historyRetentionDays);
      }
      this.el.retentionSummary.textContent = `生成记录保留 ${this.historyRetentionDays} 天`;
      this.el.chatInput.maxLength = this.limits.max_message_characters;
      this.el.promptInput.maxLength = this.limits.max_prompt_characters;
      this.el.batchCount.max = this.limits.max_batch_images;
      this.el.animationFrameCount.max = this.limits.max_animation_frames;
      this.el.animationFps.max = this.limits.max_animation_fps;
      for (const selection of this.chatReferenceSelections.values()) {
        this.trimReferenceSelection(selection, this.limits.max_chat_attachments);
      }
      this.renderWorkspaceList();
      if (this.activeWorkspace) {
        this.renderReferences();
        this.renderChatReferences();
        this.updateInteractionState();
      }
    }

    async loadRuntimeSettings(notify = true) {
      try {
        const data = await UI.api("/api/runtime-settings");
        const revision = data.revision || "";
        if (revision && revision === this.runtimeRevision) return;
        this.runtimeRevision = revision;
        this.applyRuntimeSettings(data.settings, data.history_retention_days);
      } catch (error) {
        if (notify) UI.toast(error.message, "error");
      }
    }

    bindEvents() {
      this.el.newWorkspaceButton.addEventListener("click", () => this.showWorkspaceDialog("create"));
      this.el.clearWorkspaceButton.addEventListener("click", () => this.requestClearWorkspace());
      this.el.workspaceForm.addEventListener("submit", (event) => this.saveWorkspaceName(event));
      this.el.workspaceClearForm.addEventListener("submit", (event) => this.clearWorkspace(event));
      this.el.workspaceDeleteForm.addEventListener("submit", (event) => this.deleteWorkspace(event));
      this.el.workspaceDeleteDialog.addEventListener("close", () => {
        this.workspaceDeleteId = null;
      });
      this.el.workspaceKindSwitch.addEventListener("click", (event) => {
        const button = event.target.closest("[data-workspace-kind]");
        if (button) this.setDialogWorkspaceKind(button.dataset.workspaceKind);
      });
      this.el.workspaceList.addEventListener("click", (event) => this.handleWorkspaceListClick(event));
      this.el.workspaceList.addEventListener("dblclick", (event) => this.handleWorkspaceDoubleClick(event));
      this.el.workspaceList.addEventListener("dragstart", (event) => this.handleWorkspaceDragStart(event));
      this.el.workspaceList.addEventListener("dragover", (event) => this.handleWorkspaceDragOver(event));
      this.el.workspaceList.addEventListener("drop", (event) => this.handleWorkspaceDrop(event));
      this.el.workspaceList.addEventListener("dragend", () => this.clearWorkspaceDragState());
      document.addEventListener("keydown", (event) => this.handleWorkspaceShortcut(event));
      this.el.chatForm.addEventListener("submit", (event) => this.sendChatMessage(event));
      this.el.chatInput.addEventListener("keydown", (event) => {
        if (event.key === "Enter" && !event.shiftKey && !event.isComposing) {
          event.preventDefault();
          this.el.chatForm.requestSubmit();
        }
      });
      this.el.chatInput.addEventListener("paste", (event) => this.handleChatPaste(event));
      this.el.chatForm.addEventListener("dragenter", (event) => this.handleChatDrag(event));
      this.el.chatForm.addEventListener("dragover", (event) => this.handleChatDrag(event));
      this.el.chatForm.addEventListener("dragleave", (event) => this.handleChatDragLeave(event));
      this.el.chatForm.addEventListener("drop", (event) => this.handleChatDrop(event));
      this.el.chatModelSelect.addEventListener("change", () => this.settingChanged());
      this.el.translatePrompt.addEventListener("change", () => this.settingChanged());
      this.el.draftPromptButton.addEventListener("click", () => this.createPromptDraft());
      this.el.chatReferenceButton.addEventListener("click", () => this.toggleChatReferences());
      this.el.chatReferenceList.addEventListener("click", (event) => this.handleChatReferenceClick(event));
      this.el.messageList.addEventListener("click", (event) => {
        const retryButton = event.target.closest("[data-retry-message]");
        if (retryButton) {
          this.retryChatMessage(retryButton.dataset.retryMessage);
          return;
        }
        const button = event.target.closest("[data-use-prompt-draft]");
        if (button) {
          this.applyPromptDraft(button.dataset.usePromptDraft);
          return;
        }
        this.handleJobClick(event);
      });
      this.el.generationBackButton.addEventListener("click", () => this.setComposerMode("chat"));
      this.el.modeSwitch.addEventListener("click", (event) => {
        const button = event.target.closest("[data-mode]");
        if (button && !button.disabled) this.setMode(button.dataset.mode, true);
      });
      this.el.channelSelect.addEventListener("change", () => {
        this.applyChannel(null, true);
      });
      [this.el.modelSelect, this.el.qualitySelect].forEach((field) => {
        field.addEventListener("change", () => this.settingChanged());
      });
      this.el.formatSelect.addEventListener("change", () => {
        this.updateTransparentBackgroundState();
        this.settingChanged();
      });
      this.el.transparentBackground.addEventListener("change", () => this.settingChanged());
      this.el.sizeInput.addEventListener("input", () => this.el.sizeInput.setCustomValidity(""));
      this.el.sizeInput.addEventListener("change", () => {
        if (this.validateSizeInput(true)) this.settingChanged();
      });
      this.el.batchCount.addEventListener("input", () => {
        this.updatePrice();
        this.settingChanged();
      });
      [this.el.animationFrameCount, this.el.animationFps].forEach((field) => {
        field.addEventListener("input", () => {
          this.updatePrice();
          this.settingChanged();
        });
      });
      [this.el.animationFormat, this.el.animationLoop].forEach((field) => {
        field.addEventListener("change", () => this.settingChanged());
      });
      this.el.promptInput.addEventListener("input", () => {
        window.clearTimeout(this.promptCounterTimer);
        this.promptCounterTimer = window.setTimeout(() => {
          this.el.promptCounter.textContent = `${this.el.promptInput.value.length} / ${this.limits.max_prompt_characters}`;
        }, 120);
        this.settingChanged();
      });
      this.el.referenceAdd.addEventListener("click", () => this.openReferencePicker("generation"));
      this.el.referenceInput.addEventListener("change", () => {
        this.uploadReferences([...this.el.referenceInput.files], this.uploadTarget);
      });
      this.el.referenceList.addEventListener("click", (event) => this.handleReferenceClick(event));
      this.el.generationForm.addEventListener("submit", (event) => this.submitGeneration(event));
      this.el.detailReuse.addEventListener("click", () => this.reuseDetailImage());
      document.addEventListener("visibilitychange", () => {
        if (document.hidden) return;
        this.updateWorkspaceJobDisplays();
        this.updateEtaMetric();
        this.schedulePoll(0);
        this.loadChannels(false);
        this.loadChatModels(false);
        this.loadRuntimeSettings(false);
      });
    }

    currentChannel() {
      return this.channels.find((channel) => channel.id === this.el.channelSelect.value) || null;
    }

    referenceSelectionLimit(target, workspace = this.activeWorkspace) {
      if (target === "chat") return this.limits.max_chat_attachments;
      const channelId = workspace?.id === this.activeWorkspace?.id
        ? this.el.channelSelect.value
        : workspace?.settings?.channel_id;
      const channel = this.channels.find((item) => item.id === channelId);
      const limit = channel?.capabilities.max_reference_images || 0;
      return workspace?.kind === "animation" ? Math.min(1, limit) : limit;
    }

    trimReferenceSelection(selection, limit) {
      const removed = [...selection].slice(Math.max(0, limit));
      removed.forEach((id) => selection.delete(id));
      return removed.length;
    }

    generationReferenceLimit() {
      return this.referenceSelectionLimit("generation");
    }

    currentSelection(workspaceId = this.activeWorkspace?.id) {
      if (!workspaceId) return new Set();
      if (!this.referenceSelections.has(workspaceId)) {
        this.referenceSelections.set(workspaceId, new Set());
      }
      return this.referenceSelections.get(workspaceId);
    }

    currentChatSelection(workspaceId = this.activeWorkspace?.id) {
      if (!workspaceId) return new Set();
      if (!this.chatReferenceSelections.has(workspaceId)) {
        this.chatReferenceSelections.set(workspaceId, new Set());
      }
      return this.chatReferenceSelections.get(workspaceId);
    }

    pendingReferenceUploads(workspaceId = this.activeWorkspace?.id) {
      if (!workspaceId) return [];
      return [...this.referenceUploads.values()].filter((upload) => (
        upload.workspaceId === workspaceId
      ));
    }

    get referenceUploadPending() {
      return this.pendingReferenceUploads().length > 0;
    }

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
    }

    updateWorkspaceJobDisplays() {
      const workspaces = new Map(this.workspaces.map((workspace) => [workspace.id, workspace]));
      this.el.workspaceList.querySelectorAll(".workspace-item").forEach((item) => {
        const workspace = workspaces.get(item.dataset.workspaceId);
        if (workspace) {
          this.updateWorkspaceJobDisplay(item, workspace, this.chatOperations.get(workspace.id));
        }
      });
    }

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
    }

    formatRemaining(value) {
      const milliseconds = new Date(value).getTime() - Date.now();
      if (!Number.isFinite(milliseconds) || milliseconds <= 0) return "仍在处理";
      const seconds = `${Math.ceil(milliseconds / 1000)}s`;
      return `剩余 ${seconds}`;
    }

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
    }

    handleWorkspaceDoubleClick(event) {
      const select = event.target.closest("[data-select-workspace]");
      if (select) this.renameWorkspace(select.dataset.selectWorkspace);
    }

    handleWorkspaceShortcut(event) {
      if (event.key !== "F2" || event.defaultPrevented || event.altKey
        || event.ctrlKey || event.metaKey || event.shiftKey || !this.activeWorkspace) return;
      const target = event.target;
      if (target instanceof HTMLElement
        && (target.isContentEditable || target.closest("input, textarea, select"))) return;
      if (document.querySelector("dialog[open]")) return;
      event.preventDefault();
      this.showWorkspaceDialog("rename");
    }

    async renameWorkspace(id) {
      await this.selectWorkspace(id);
      if (this.activeWorkspace?.id === id) this.showWorkspaceDialog("rename");
    }

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
    }

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
    }

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
    }

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
    }

    clearWorkspaceDropIndicators() {
      this.el.workspaceList.querySelectorAll(".drop-before, .drop-after").forEach((item) => {
        item.classList.remove("drop-before", "drop-after");
      });
    }

    clearWorkspaceDragState() {
      this.clearWorkspaceDropIndicators();
      this.el.workspaceList.querySelector(".dragging")?.classList.remove("dragging");
      this.draggedWorkspaceId = null;
    }

    async selectWorkspace(id) {
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
      this.setWorkspaceLoading(true, selection);
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
      await Promise.all([this.loadJobs(), this.loadMessages()]);
      if (selection !== this.workspaceLoadSequence) return;
      this.setWorkspaceLoading(false, selection);
    }

    loadLastWorkspaceId() {
      try {
        return window.localStorage.getItem(`imagegen:last-workspace:${this.user.id}`);
      } catch {
        return null;
      }
    }

    saveLastWorkspaceId(workspaceId) {
      try {
        window.localStorage.setItem(`imagegen:last-workspace:${this.user.id}`, workspaceId);
      } catch {
        // The app remains usable when browser storage is unavailable.
      }
    }

    setWorkspaceLoading(loading, selection) {
      window.clearTimeout(this.workspaceSkeletonTimer);
      this.workspaceLoading = loading;
      this.el.conversationLoading.hidden = true;
      this.el.conversationScroll.toggleAttribute("aria-busy", loading);
      this.el.messageList.hidden = loading;
      this.updateInteractionState();
      if (loading) {
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
    }

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
    }

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
    }

    setComposerMode(mode) {
      const generation = mode === "generation";
      this.el.chatForm.hidden = generation;
      this.el.generationForm.hidden = !generation;
      this.updateInteractionState();
    }

    applyWorkspaceSettings() {
      const settings = this.activeWorkspace?.settings || {};
      this.updateWorkspaceKindUI();
      this.renderChatModelOptions(settings.chat_model_id);
      this.el.translatePrompt.checked = settings.translate_prompt === true;
      this.el.transparentBackground.checked = settings.transparent_background === true;
      this.el.promptInput.value = settings.prompt || "";
      this.el.promptCounter.textContent = `${this.el.promptInput.value.length} / ${this.limits.max_prompt_characters}`;
      this.el.batchCount.value = Math.min(
        this.limits.max_batch_images, Math.max(1, Number(settings.batch_count || 1)),
      );
      this.el.animationFrameCount.value = Math.min(
        this.limits.max_animation_frames, Math.max(2, Number(settings.animation_frame_count || 8)),
      );
      this.el.animationFps.value = Math.min(
        this.limits.max_animation_fps, Math.max(1, Number(settings.animation_fps || 8)),
      );
      this.el.animationFormat.value = ["webp", "gif"].includes(settings.animation_format)
        ? settings.animation_format
        : "webp";
      this.el.animationLoop.checked = settings.animation_loop !== false;
      const preferred = this.channels.find((channel) => channel.id === settings.channel_id && channel.configured)
        || this.channels.find((channel) => channel.configured)
        || this.channels[0];
      this.renderChannelOptions(preferred?.id);
      this.applyChannel(settings, false);
      const mode = this.isAnimationWorkspace()
        ? (this.currentSelection().size ? "img2img" : "text2img")
        : (settings.mode || "text2img");
      this.setMode(mode, false);
      this.el.saveState.textContent = "参数已保存";
    }

    isAnimationWorkspace() {
      return this.activeWorkspace?.kind === "animation";
    }

    animationNeedsMaster() {
      return this.isAnimationWorkspace() && this.currentSelection().size === 0;
    }

    updateWorkspaceKindUI() {
      const animation = this.isAnimationWorkspace();
      const needsMaster = this.animationNeedsMaster();
      this.el.modeSwitch.hidden = animation;
      this.el.imageCountControl.hidden = animation;
      this.el.animationControls.forEach((control) => {
        control.hidden = !animation || needsMaster;
      });
      this.el.generationForm.classList.toggle("animation-workflow", animation);
      this.el.generationHeadingTitle.textContent = needsMaster
        ? "生成并确认母图"
        : animation ? "确认帧动画参数" : "确认生图参数";
      this.el.generationHeadingSubtitle.textContent = needsMaster
        ? "先生成 1 张母图，确认后再制作帧动画"
        : animation ? "帧序列将按顺序生成并合成为动画"
        : "提示词和参数会保存在当前工作站";
      this.el.frameFormatLabel.textContent = needsMaster
        ? "母图格式"
        : animation ? "帧格式" : "格式";
      this.el.generateButtonLabel.textContent = needsMaster
        ? "生成母图"
        : animation ? "开始生成帧" : "开始生成";
      this.el.promptInput.placeholder = needsMaster
        ? "输入角色造型、画面与动作基准描述..."
        : animation ? "输入画面与完整动作描述..." : "输入画面描述...";
      this.el.referenceAddLabel.textContent = animation ? "添加母图" : "添加垫图";
      this.el.referenceStrip.hidden = !animation && this.el.modeSwitch.dataset.mode !== "img2img";
    }

    renderChatModelOptions(selectedId = this.el.chatModelSelect.value) {
      const available = this.chatModels.filter((model) => model.enabled && model.configured);
      const options = available.map((model) => {
        const option = document.createElement("option");
        option.value = model.id;
        const reasoning = model.reasoning_effort ? ` · 推理 ${model.reasoning_effort}` : "";
        option.textContent = model.label;
        option.title = `${model.model}${reasoning}`;
        return option;
      });
      if (!options.length) {
        const option = document.createElement("option");
        option.value = "";
        option.textContent = "管理员尚未配置";
        options.push(option);
      }
      this.el.chatModelSelect.replaceChildren(...options);
      this.el.chatModelSelect.value = available.some((model) => model.id === selectedId)
        ? selectedId
        : (available[0]?.id || "");
      this.updateInteractionState();
    }

    renderChannelOptions(selectedId = this.el.channelSelect.value) {
      const options = this.channels.map((channel) => {
        const option = document.createElement("option");
        option.value = channel.id;
        option.textContent = `${channel.label}${channel.configured ? "" : " · 未配置"}`;
        option.disabled = !channel.configured;
        return option;
      });
      this.el.channelSelect.replaceChildren(...options);
      const selected = this.channels.find((channel) => channel.id === selectedId && channel.configured)
        || this.channels.find((channel) => channel.configured);
      this.el.channelSelect.value = selected?.id || "";
      this.el.channelSelect.disabled = !selected;
    }

    applyChannel(saved = null, shouldSave = false) {
      const channel = this.currentChannel();
      if (!channel) {
        [this.el.modelSelect, this.el.qualitySelect, this.el.formatSelect].forEach((field) => {
          field.replaceChildren();
          field.disabled = true;
        });
        this.el.sizeOptions.replaceChildren();
        this.el.sizeInput.value = "";
        this.el.sizeInput.disabled = true;
        this.el.sizeInput.setCustomValidity("");
        this.updateTransparentBackgroundState();
        this.el.generateButton.disabled = true;
        this.updatePrice();
        return;
      }
      const settings = saved || this.collectSettings();
      this.fillSelect(this.el.modelSelect, channel.models, settings.model, "id", "label");
      this.fillSizeSuggestions(channel.capabilities.sizes, settings.size);
      this.fillSelect(this.el.qualitySelect, channel.capabilities.qualities, settings.quality, null, null, {
        auto: "自动", low: "低", medium: "中", high: "高",
      });
      this.fillSelect(this.el.formatSelect, channel.capabilities.formats, settings.output_format, null, null, {
        png: "PNG", jpeg: "JPEG", webp: "WebP",
      });
      this.updateTransparentBackgroundState();
      document.querySelectorAll("[data-mode]").forEach((button) => {
        button.disabled = !channel.capabilities.modes.includes(button.dataset.mode);
      });
      if (!channel.capabilities.modes.includes(this.el.modeSwitch.dataset.mode)) {
        this.setMode(channel.capabilities.modes[0], false);
      }
      const selection = this.currentSelection();
      this.trimReferenceSelection(selection, this.generationReferenceLimit());
      this.el.generateButton.disabled = false;
      this.renderReferences();
      this.updatePrice();
      this.updateInteractionState();
      if (shouldSave) this.settingChanged();
    }

    fillSelect(select, values, preferred, idKey = null, labelKey = null, labels = {}) {
      const normalized = values || [];
      const options = normalized.map((value) => {
        const id = idKey ? value[idKey] : value;
        const label = labelKey ? value[labelKey] : (labels[value] || value);
        const option = document.createElement("option");
        option.value = id;
        option.textContent = label;
        return option;
      });
      select.replaceChildren(...options);
      const ids = normalized.map((value) => idKey ? value[idKey] : value);
      select.value = ids.includes(preferred) ? preferred : (ids[0] || "");
      select.disabled = !ids.length;
    }

    fillSizeSuggestions(values, preferred) {
      const suggestions = (values || []).map((value) => this.normalizeSize(value)).filter(Boolean);
      this.el.sizeOptions.replaceChildren(
        ...suggestions.map((value) => {
          const option = document.createElement("option");
          option.value = value;
          return option;
        }),
      );
      this.el.sizeInput.value = this.normalizeSize(preferred) || suggestions[0] || "1024x1024";
      this.el.sizeInput.disabled = false;
      this.el.sizeInput.setCustomValidity("");
    }

    normalizeSize(value) {
      return String(value || "").trim().toLowerCase().replaceAll("×", "x");
    }

    validateSizeInput(report = false) {
      if (this.el.sizeInput.disabled) return true;
      const value = this.normalizeSize(this.el.sizeInput.value);
      const match = IMAGE_SIZE_PATTERN.exec(value);
      const valid = Boolean(match)
        && [Number(match[1]), Number(match[2])]
          .every((dimension) => dimension >= IMAGE_DIMENSION_MIN && dimension <= IMAGE_DIMENSION_MAX);
      this.el.sizeInput.setCustomValidity(valid ? "" : "尺寸格式应为宽x高，单边范围 64–8192 像素");
      if (valid) this.el.sizeInput.value = value;
      else if (report) this.el.sizeInput.reportValidity();
      return valid;
    }

    updateTransparentBackgroundState() {
      const available = ["png", "webp"].includes(this.el.formatSelect.value);
      this.el.transparentBackground.disabled = !available;
      if (!available) this.el.transparentBackground.checked = false;
      this.el.transparentBackgroundControl.classList.toggle("is-disabled", !available);
      this.el.transparentBackgroundControl.title = available
        ? "生成包含 Alpha 通道的透明背景图片"
        : "透明背景仅支持 PNG 或 WebP";
    }

    setMode(mode, shouldSave) {
      const channel = this.currentChannel();
      if (channel && !channel.capabilities.modes.includes(mode)) return;
      this.el.modeSwitch.dataset.mode = mode;
      this.el.modeSwitch.querySelectorAll("[data-mode]").forEach((button) => {
        const active = button.dataset.mode === mode;
        button.classList.toggle("active", active);
        button.setAttribute("aria-pressed", String(active));
      });
      this.el.referenceStrip.hidden = !this.isAnimationWorkspace() && mode !== "img2img";
      this.updateWorkspaceKindUI();
      this.updatePrice();
      if (shouldSave) this.settingChanged();
    }

    collectSettings() {
      return {
        mode: this.el.modeSwitch.dataset.mode,
        prompt: this.el.promptInput.value,
        channel_id: this.el.channelSelect.value,
        model: this.el.modelSelect.value,
        size: this.normalizeSize(this.el.sizeInput.value)
          || this.activeWorkspace?.settings?.size
          || "1024x1024",
        quality: this.el.qualitySelect.value,
        output_format: this.el.formatSelect.value,
        compression: 90,
        transparent_background: this.el.transparentBackground.checked,
        batch_count: Math.min(
          this.limits.max_batch_images, Math.max(1, Number(this.el.batchCount.value || 1)),
        ),
        animation_frame_count: Math.min(
          this.limits.max_animation_frames,
          Math.max(2, Number(this.el.animationFrameCount.value || 8)),
        ),
        animation_fps: Math.min(
          this.limits.max_animation_fps, Math.max(1, Number(this.el.animationFps.value || 8)),
        ),
        animation_loop: this.el.animationLoop.checked,
        animation_format: this.el.animationFormat.value,
        chat_model_id: this.el.chatModelSelect.value,
        translate_prompt: this.el.translatePrompt.checked,
      };
    }

    settingChanged() {
      if (!this.activeWorkspace) return;
      if (this.el.saveState.textContent !== "正在保存...") {
        this.el.saveState.textContent = "正在保存...";
      }
      window.clearTimeout(this.saveTimer);
      this.saveTimer = window.setTimeout(() => {
        this.saveTimer = null;
        this.saveSettings();
      }, 550);
    }

    async flushSettings() {
      if (this.saveTimer === null) return;
      window.clearTimeout(this.saveTimer);
      this.saveTimer = null;
      await this.saveSettings();
    }

    async saveSettings() {
      const workspace = this.activeWorkspace;
      if (!workspace) return;
      if (!this.validateSizeInput(false)) {
        this.el.saveState.textContent = "尺寸无效";
        return;
      }
      const settings = this.collectSettings();
      try {
        const data = await UI.api(`/api/workspaces/${workspace.id}`, {
          method: "PATCH",
          body: { settings },
        });
        workspace.settings = data.workspace.settings;
        if (workspace === this.activeWorkspace) this.el.saveState.textContent = "参数已保存";
      } catch (error) {
        if (workspace === this.activeWorkspace) this.el.saveState.textContent = "保存失败";
        UI.toast(error.message, "error");
      }
    }

    showWorkspaceDialog(mode) {
      this.dialogMode = mode;
      this.el.workspaceDialogTitle.textContent = mode === "create" ? "新建工作站" : "重命名工作站";
      this.el.workspaceNameInput.value = mode === "rename"
        ? this.activeWorkspace?.name || ""
        : this.nextWorkspaceName();
      this.el.workspaceKindControl.hidden = mode !== "create";
      if (mode === "create") this.setDialogWorkspaceKind("image");
      UI.openDialog(this.el.workspaceDialog);
      this.el.workspaceNameInput.focus();
      if (mode === "create") this.el.workspaceNameInput.select();
    }

    nextWorkspaceName() {
      const now = new Date();
      const pad = (value) => String(value).padStart(2, "0");
      const base = `工作站-${now.getFullYear()}-${pad(now.getMonth() + 1)}-${pad(now.getDate())}`;
      const names = new Set(this.workspaces.map((workspace) => workspace.name));
      if (!names.has(base)) return base;
      let index = 2;
      while (names.has(`${base} ${index}`)) index += 1;
      return `${base} ${index}`;
    }

    setDialogWorkspaceKind(kind) {
      this.dialogWorkspaceKind = kind === "animation" ? "animation" : "image";
      this.el.workspaceKindSwitch.dataset.kind = this.dialogWorkspaceKind;
      this.el.workspaceKindSwitch.querySelectorAll("[data-workspace-kind]").forEach((button) => {
        const active = button.dataset.workspaceKind === this.dialogWorkspaceKind;
        button.classList.toggle("active", active);
        button.setAttribute("aria-pressed", String(active));
      });
    }

    async saveWorkspaceName(event) {
      event.preventDefault();
      const name = this.el.workspaceNameInput.value.trim();
      const submit = this.el.workspaceForm.querySelector('[type="submit"]');
      submit.disabled = true;
      try {
        if (this.dialogMode === "create") {
          const data = await UI.api("/api/workspaces", {
            method: "POST",
            body: { name, kind: this.dialogWorkspaceKind },
          });
          this.workspaces.unshift(data.workspace);
          UI.closeDialog(this.el.workspaceDialog);
          this.activeWorkspace = null;
          await this.selectWorkspace(data.workspace.id);
          UI.toast("工作站已创建", "success");
        } else {
          const data = await UI.api(`/api/workspaces/${this.activeWorkspace.id}`, {
            method: "PATCH", body: { name },
          });
          Object.assign(this.activeWorkspace, data.workspace);
          this.el.workspaceTitle.textContent = data.workspace.name;
          this.renderWorkspaceList();
          UI.closeDialog(this.el.workspaceDialog);
          UI.toast("工作站已重命名", "success");
        }
      } catch (error) {
        UI.toast(error.message, "error");
      } finally {
        submit.disabled = false;
      }
    }

    async requestDeleteWorkspace(id) {
      const workspace = this.workspaces.find((item) => item.id === id);
      if (!workspace) return;
      await this.selectWorkspace(id);
      if (this.activeWorkspace?.id !== id) return;
      if (this.referenceUploadPending) {
        UI.toast("请先等待图片上传完成或取消上传", "error");
        return;
      }
      if (this.workspaceHasActiveJob() || this.workspaceChatBusy(id)) {
        UI.toast("请等待当前任务完成后再删除工作站", "error");
        return;
      }
      this.workspaceDeleteId = id;
      this.el.workspaceDeleteName.textContent = workspace.name;
      UI.openDialog(this.el.workspaceDeleteDialog);
    }

    async deleteWorkspace(event) {
      event.preventDefault();
      const workspaceId = this.workspaceDeleteId;
      const workspace = this.workspaces.find((item) => item.id === workspaceId);
      if (!workspace) {
        UI.closeDialog(this.el.workspaceDeleteDialog);
        return;
      }
      const submit = this.el.workspaceDeleteForm.querySelector('[type="submit"]');
      submit.disabled = true;
      try {
        await UI.api(`/api/workspaces/${workspaceId}`, { method: "DELETE" });
        const index = this.workspaces.findIndex((item) => item.id === workspaceId);
        this.referenceSelections.delete(workspaceId);
        this.chatReferenceSelections.delete(workspaceId);
        this.chatDrafts.delete(workspaceId);
        this.chatOperations.delete(workspaceId);
        this.workspaceJobs.delete(workspaceId);
        this.pendingUserMessages.delete(workspaceId);
        this.workspaces.splice(index, 1);
        this.activeWorkspace = null;
        if (!this.workspaces.length) {
          const data = await UI.api("/api/workspaces", { method: "POST", body: { name: "默认工作站" } });
          this.workspaces.push(data.workspace);
        }
        UI.closeDialog(this.el.workspaceDeleteDialog);
        await this.selectWorkspace(this.workspaces[Math.max(0, index - 1)]?.id || this.workspaces[0].id);
        UI.toast("工作站已删除", "success");
      } catch (error) {
        UI.toast(error.message, "error");
      } finally {
        submit.disabled = false;
      }
    }

    requestClearWorkspace() {
      if (!this.activeWorkspace || this.workspaceHasActiveJob()
        || this.workspaceChatBusy() || this.referenceUploadPending) {
        UI.toast("当前任务完成前不能清空会话", "error");
        return;
      }
      this.el.workspaceClearName.textContent = this.activeWorkspace.name;
      UI.openDialog(this.el.workspaceClearDialog);
    }

    async clearWorkspace(event) {
      event.preventDefault();
      const workspace = this.activeWorkspace;
      if (!workspace || this.workspaceHasActiveJob()
        || this.workspaceChatBusy() || this.referenceUploadPending) {
        UI.closeDialog(this.el.workspaceClearDialog);
        UI.toast("当前任务完成前不能清空会话", "error");
        return;
      }
      const submit = this.el.workspaceClearForm.querySelector('[type="submit"]');
      submit.disabled = true;
      try {
        const data = await UI.api(`/api/workspaces/${workspace.id}/clear`, {
          method: "POST",
        });
        Object.assign(workspace, data.workspace);
        this.messages = [];
        this.jobs = [];
        this.conversationContext = null;
        this.referenceSelections.set(workspace.id, new Set());
        this.chatReferenceSelections.set(workspace.id, new Set());
        this.chatDrafts.set(workspace.id, "");
        this.pendingUserMessages.delete(workspace.id);
        this.chatReferencePickerOpen = false;
        this.applyWorkspaceSettings();
        this.renderReferences();
        this.renderChatReferences();
        this.renderWorkspaceList();
        this.renderMessages();
        this.updateMetrics();
        this.setComposerMode("chat");
        UI.closeDialog(this.el.workspaceClearDialog);
        UI.toast("当前会话已清空", "success");
      } catch (error) {
        UI.toast(error.message, "error");
      } finally {
        submit.disabled = false;
        this.updateInteractionState();
      }
    }

    async loadMessages(workspaceId = this.activeWorkspace?.id) {
      if (!workspaceId) return;
      const existing = this.loadingMessageWorkspaces.get(workspaceId);
      if (existing) return existing;
      const request = (async () => {
        try {
          const data = await UI.api(`/api/workspaces/${workspaceId}/messages?limit=200`);
          const operationChanged = this.syncServerChatOperation(
            workspaceId,
            data.conversation_operation,
          );
          if (operationChanged) this.renderWorkspaceList();
          if (this.activeWorkspace?.id === workspaceId) {
            const nextMessages = data.messages || [];
            const messagesChanged = this.messages.length !== nextMessages.length
              || this.messages[0]?.id !== nextMessages[0]?.id
              || this.messages.at(-1)?.id !== nextMessages.at(-1)?.id;
            const contextChanged = JSON.stringify(this.conversationContext)
              !== JSON.stringify(data.context);
            if (messagesChanged) this.messages = nextMessages;
            if (contextChanged) this.conversationContext = data.context;
            if (messagesChanged || contextChanged || operationChanged) this.renderMessages();
          }
        } catch (error) {
          UI.toast(error.message, "error");
        }
      })();
      this.loadingMessageWorkspaces.set(workspaceId, request);
      try {
        return await request;
      } finally {
        if (this.loadingMessageWorkspaces.get(workspaceId) === request) {
          this.loadingMessageWorkspaces.delete(workspaceId);
        }
      }
    }

    renderMessages() {
      const scrollGap = this.el.conversationScroll.scrollHeight
        - this.el.conversationScroll.scrollTop
        - this.el.conversationScroll.clientHeight;
      const keepAtBottom = scrollGap < 120;
      const timeline = [
        ...this.messages.map((message) => ({
          type: "message",
          createdAt: message.created_at,
          id: message.id,
          value: message,
        })),
        ...this.jobs.map((job) => ({
          type: "job",
          createdAt: job.created_at,
          id: job.id,
          value: job,
        })),
      ].sort((left, right) => {
        const time = new Date(left.createdAt || 0).getTime() - new Date(right.createdAt || 0).getTime();
        return time || String(left.id).localeCompare(String(right.id));
      });
      const workspaceId = this.activeWorkspace?.id;
      const pendingUserMessage = this.pendingUserMessages.get(workspaceId);
      const operation = this.chatOperations.get(workspaceId);
      if (pendingUserMessage) {
        timeline.push({
          type: "message",
          createdAt: pendingUserMessage.created_at,
          id: `pending-user-${workspaceId}`,
          value: pendingUserMessage,
        });
      }
      if (operation) {
        timeline.push({
          type: "message",
          createdAt: null,
          id: `pending-assistant-${workspaceId}`,
          value: {
            role: "assistant",
            kind: "pending",
            content: operation.label,
            created_at: operation.started_at || null,
          },
        });
      }
      const timelineChanged = this.reconcileTimeline(timeline);
      setHidden(this.el.conversationEmpty, this.workspaceLoading || timeline.length > 0);
      this.renderContextStatus();
      this.updateInteractionState();
      if (!this.workspaceLoading && keepAtBottom && timelineChanged) this.scrollConversation();
    }

    reconcileTimeline(timeline) {
      const existing = new Map(
        [...this.el.messageList.children].map((node) => [node.dataset.timelineKey, node]),
      );
      const desiredKeys = new Set();
      let layoutChanged = false;
      const entranceStart = Math.max(0, timeline.length - 24);
      const newEntryCount = timeline.reduce((count, entry) => (
        count + (existing.has(`${entry.type}:${entry.id}`) ? 0 : 1)
      ), 0);
      if (timeline.length && (existing.size === 0 || newEntryCount > 48)) {
        const fragment = document.createDocumentFragment();
        timeline.forEach((entry, index) => {
          const key = `${entry.type}:${entry.id}`;
          const node = entry.type === "job"
            ? this.jobCard(entry.value)
            : this.messageCard(entry.value);
          node.dataset.timelineKey = key;
          if (!this.reducedMotion.matches && index >= entranceStart) {
            node.classList.add("timeline-enter");
            node.addEventListener("animationend", () => node.classList.remove("timeline-enter"), {
              once: true,
            });
          }
          if (entry.type !== "job") UI.icons(node);
          fragment.append(node);
        });
        this.el.messageList.replaceChildren(fragment);
        return true;
      }
      timeline.forEach((entry, index) => {
        const key = `${entry.type}:${entry.id}`;
        desiredKeys.add(key);
        const isNew = !existing.has(key);
        let node = existing.get(key);
        if (entry.type === "job") {
          const previousStatus = node?.dataset.jobStatus;
          const unchangedTerminal = node
            && node.dataset.jobStatus === entry.value.status
            && TERMINAL.has(entry.value.status);
          if (!unchangedTerminal) {
            node = node ? this.updateJobCard(node, entry.value) : this.jobCard(entry.value);
            if (previousStatus !== entry.value.status) layoutChanged = true;
          }
        } else if (!node) {
          node = this.messageCard(entry.value);
        }
        if (node.dataset.timelineKey !== key) node.dataset.timelineKey = key;
        const current = this.el.messageList.children[index];
        if (current !== node) {
          this.el.messageList.insertBefore(node, current || null);
          layoutChanged = true;
        }
        if (isNew) {
          layoutChanged = true;
          if (!this.reducedMotion.matches && index >= entranceStart) {
            node.classList.add("timeline-enter");
            const clearEntrance = (event) => {
              if (event.target !== node) return;
              node.classList.remove("timeline-enter");
              node.removeEventListener("animationend", clearEntrance);
            };
            node.addEventListener("animationend", clearEntrance);
          }
          if (entry.type !== "job") UI.icons(node);
        }
      });
      [...this.el.messageList.children].forEach((node) => {
        if (!desiredKeys.has(node.dataset.timelineKey)) {
          node.remove();
          layoutChanged = true;
        }
      });
      return layoutChanged;
    }

    prepareImageReveal(image) {
      image.classList.add("media-reveal");
      image.classList.remove("is-loaded");
      let revealed = false;
      const reveal = () => {
        if (revealed) return;
        revealed = true;
        window.requestAnimationFrame(() => image.classList.add("is-loaded"));
      };
      image.addEventListener("error", reveal, { once: true });
      if (image.complete && image.naturalWidth > 0) {
        if (typeof image.decode === "function") image.decode().then(reveal, reveal);
        else reveal();
      } else {
        image.addEventListener("load", reveal, { once: true });
      }
    }

    messageCard(message) {
      const row = document.createElement("article");
      row.className = `message-row ${message.role} ${message.kind || "message"}`;
      if (message.id) row.dataset.messageId = message.id;

      const avatar = document.createElement("span");
      avatar.className = "message-avatar";
      if (message.role === "user") {
        avatar.innerHTML = '<i data-lucide="user-round"></i>';
      } else {
        const brand = document.createElement("img");
        brand.src = this.brandMarkUrl;
        brand.alt = "";
        avatar.append(brand);
      }

      const card = document.createElement("div");
      card.className = "message-card";
      const meta = document.createElement("header");
      meta.className = "message-meta";
      const author = document.createElement("strong");
      author.textContent = message.role === "user" ? "你" : (message.provider_label || "AI 助手");
      const timing = document.createElement("span");
      const timingParts = [];
      if (message.created_at) timingParts.push(UI.dateTime(message.created_at));
      if (message.role === "assistant" && Number.isFinite(Number(message.elapsed_seconds))) {
        timingParts.push(`响应 ${this.formatElapsed(message.elapsed_seconds)}`);
      }
      timing.textContent = timingParts.join(" · ") || "正在处理";
      meta.append(author, timing);
      card.append(meta);

      if (message.kind === "pending") {
        const pending = document.createElement("div");
        pending.className = "message-pending";
        pending.innerHTML = '<span class="message-pending-dots" aria-hidden="true"><i></i><i></i><i></i></span><span></span>';
        pending.lastElementChild.textContent = message.content || "正在等待 AI 回复";
        card.append(pending);
      } else if (message.kind === "prompt_draft") {
        card.append(this.promptDraftContent(message));
      } else {
        const content = document.createElement("p");
        content.className = "message-content";
        content.textContent = message.content || "";
        card.append(content);
        if (message.kind === "error" && message.payload?.retry_user_message_id) {
          const retry = document.createElement("button");
          retry.type = "button";
          retry.className = "button ghost small";
          retry.dataset.retryMessage = message.id;
          retry.innerHTML = '<i data-lucide="refresh-cw"></i>重新发送';
          card.append(retry);
        }
      }

      if (message.attachments?.length) {
        const attachments = document.createElement("div");
        attachments.className = "message-attachments";
        message.attachments.forEach((asset) => {
          const link = document.createElement("a");
          link.href = asset.url;
          link.target = "_blank";
          link.rel = "noopener";
          link.title = asset.name;
          const image = document.createElement("img");
          image.src = asset.url;
          image.alt = asset.name;
          image.loading = "lazy";
          image.decoding = "async";
          this.prepareImageReveal(image);
          link.append(image);
          attachments.append(link);
        });
        card.append(attachments);
      }

      row.append(avatar, card);
      return row;
    }

    promptDraftContent(message) {
      const payload = message.payload || {};
      const wrap = document.createElement("div");
      wrap.className = "prompt-draft-content";
      const summaryLabel = document.createElement("span");
      summaryLabel.className = "message-section-label";
      summaryLabel.textContent = "需求确认";
      const summary = document.createElement("p");
      summary.textContent = payload.summary_zh || "";
      const promptLabel = document.createElement("span");
      promptLabel.className = "message-section-label";
      promptLabel.textContent = payload.language === "en"
        ? "English prompt"
        : this.isAnimationWorkspace() ? "帧动画提示词" : "生图提示词";
      const prompt = document.createElement("p");
      prompt.className = "prompt-draft-text";
      prompt.textContent = payload.prompt || "";
      const action = document.createElement("button");
      action.type = "button";
      action.className = "button primary small";
      action.dataset.usePromptDraft = message.id;
      action.innerHTML = '<i data-lucide="image-plus"></i>使用此提示词生图';
      wrap.append(summaryLabel, summary, promptLabel, prompt, action);
      return wrap;
    }

    formatElapsed(value) {
      const seconds = Number(value);
      if (!Number.isFinite(seconds)) return "--";
      if (seconds < 60) return `${seconds.toFixed(seconds < 10 ? 1 : 0)} 秒`;
      const minutes = Math.floor(seconds / 60);
      return `${minutes} 分 ${Math.round(seconds % 60)} 秒`;
    }

    async sendChatMessage(event) {
      event.preventDefault();
      if (this.workspaceLoading || !this.activeWorkspace
        || this.workspaceChatBusy() || this.workspaceHasActiveJob()) return;
      if (this.referenceUploadPending) {
        UI.toast("请等待图片上传完成或取消上传", "info");
        return;
      }
      const workspace = this.activeWorkspace;
      const workspaceId = workspace.id;
      const modelId = this.el.chatModelSelect.value;
      const draft = this.el.chatInput.value;
      const content = draft.trim();
      await this.loadRuntimeSettings(false);
      if (this.activeWorkspace?.id !== workspaceId) return;
      const selection = this.currentChatSelection();
      const omitted = this.trimReferenceSelection(
        selection,
        this.referenceSelectionLimit("chat", workspace),
      );
      if (omitted) {
        this.renderChatReferences();
        UI.toast(`附件上限已更新，已取消 ${omitted} 张超限图片`, "info");
      }
      const attachmentIds = [...selection];
      if (!content && !attachmentIds.length) {
        UI.toast("请输入消息或添加参考图", "error");
        this.el.chatInput.focus();
        return;
      }
      if (!modelId) {
        UI.toast("管理员尚未配置可用的对话模型", "error");
        return;
      }
      this.startLocalChatOperation(workspaceId, "reply", "正在等待 AI 回复");
      let failure = null;
      try {
        await this.flushSettings();
        if (this.activeWorkspace?.id !== workspaceId) return;
        const selectedIds = new Set(attachmentIds);
        this.pendingUserMessages.set(workspaceId, {
          role: "user",
          kind: "message",
          content,
          attachments: workspace.assets.filter((asset) => selectedIds.has(asset.id)),
          created_at: new Date().toISOString(),
        });
        this.el.chatInput.value = "";
        this.chatDrafts.set(workspaceId, "");
        selection.clear();
        this.chatReferencePickerOpen = false;
        this.renderChatReferences();
        this.renderMessages();
        const data = await UI.api(`/api/workspaces/${workspaceId}/messages`, {
          method: "POST",
          body: {
            model_id: modelId,
            content,
            attachment_ids: attachmentIds,
          },
        });
        this.pendingUserMessages.delete(workspaceId);
        if (this.activeWorkspace?.id === workspaceId) {
          this.messages.push(...data.messages);
          this.conversationContext = data.context;
        }
        if (data.workspace) {
          Object.assign(workspace, data.workspace);
          if (this.activeWorkspace?.id === workspaceId) {
            this.el.workspaceTitle.textContent = workspace.name;
          }
          this.renderWorkspaceList();
        }
      } catch (error) {
        this.pendingUserMessages.delete(workspaceId);
        this.chatDrafts.set(workspaceId, draft);
        attachmentIds.forEach((id) => selection.add(id));
        if (this.activeWorkspace?.id === workspaceId) {
          this.el.chatInput.value = draft;
          this.chatReferencePickerOpen = attachmentIds.length > 0;
          this.renderChatReferences();
        }
        failure = error;
      } finally {
        await this.finishLocalChatOperation(workspaceId, failure);
      }
    }

    async retryChatMessage(errorMessageId) {
      if (this.workspaceLoading || !this.activeWorkspace
        || this.workspaceChatBusy() || this.workspaceHasActiveJob()) return;
      const workspaceId = this.activeWorkspace.id;
      const modelId = this.el.chatModelSelect.value;
      if (!modelId) {
        UI.toast("管理员尚未配置可用的对话模型", "error");
        return;
      }
      this.startLocalChatOperation(workspaceId, "reply", "正在重新发送消息");
      let failure = null;
      try {
        await this.flushSettings();
        if (this.activeWorkspace?.id !== workspaceId) return;
        const data = await UI.api(
          `/api/workspaces/${workspaceId}/messages/${errorMessageId}/retry`,
          { method: "POST", body: { model_id: modelId } },
        );
        if (this.activeWorkspace?.id === workspaceId) {
          this.messages.push(data.message);
          this.conversationContext = data.context;
        }
      } catch (error) {
        failure = error;
      } finally {
        await this.finishLocalChatOperation(workspaceId, failure);
      }
    }

    async createPromptDraft() {
      if (this.workspaceLoading || !this.activeWorkspace
        || this.workspaceChatBusy() || this.workspaceHasActiveJob()
        || this.referenceUploadPending) return;
      if (!this.messages.some((message) => message.role === "user")) {
        UI.toast(
          this.isAnimationWorkspace() ? "请先描述需要制作的帧动画" : "请先描述需要生成的图片",
          "error",
        );
        this.el.chatInput.focus();
        return;
      }
      const workspace = this.activeWorkspace;
      const workspaceId = workspace.id;
      const modelId = this.el.chatModelSelect.value;
      const translateToEnglish = this.el.translatePrompt.checked;
      const generationMode = this.el.modeSwitch.dataset.mode || "text2img";
      const generationReferences = generationMode === "img2img"
        ? [...this.currentSelection(workspaceId)]
        : [];
      this.startLocalChatOperation(
        workspaceId,
        "prompt_draft",
        this.isAnimationWorkspace() ? "正在检查并总结帧动画需求" : "正在检查并总结生图需求",
      );
      let failure = null;
      try {
        await this.flushSettings();
        if (this.activeWorkspace?.id !== workspaceId) return;
        const data = await UI.api(`/api/workspaces/${workspaceId}/prompt-drafts`, {
          method: "POST",
          body: {
            model_id: modelId,
            translate_to_english: translateToEnglish,
            mode: generationMode,
            reference_ids: generationReferences,
          },
        });
        if (this.activeWorkspace?.id === workspaceId) {
          this.messages.push(data.message);
          this.conversationContext = data.context;
          if (data.message?.payload?.status === "needs_clarification") {
            const references = this.currentChatSelection(workspaceId);
            (data.message.payload.reference_ids || []).forEach((id) => references.add(id));
            this.trimReferenceSelection(
              references,
              this.referenceSelectionLimit("chat", workspace),
            );
            this.chatReferencePickerOpen = references.size > 0;
            this.renderChatReferences();
            UI.toast("还需补充关键信息", "info");
            this.el.chatInput.focus();
          }
        }
        workspace.settings.translate_prompt = translateToEnglish;
      } catch (error) {
        failure = error;
      } finally {
        await this.finishLocalChatOperation(workspaceId, failure);
      }
    }

    applyPromptDraft(messageId) {
      const message = this.messages.find((item) => item.id === messageId);
      const prompt = message?.payload?.prompt;
      if (!prompt) return;
      this.el.promptInput.value = prompt;
      this.el.promptCounter.textContent = `${prompt.length} / ${this.limits.max_prompt_characters}`;
      const activeIds = new Set((this.activeWorkspace?.assets || []).map((asset) => asset.id));
      const requested = [...new Set(message.payload.reference_ids || [])];
      const available = requested.filter((id) => activeIds.has(id));
      const max = this.generationReferenceLimit();
      const references = new Set(available.slice(0, max));
      this.referenceSelections.set(this.activeWorkspace.id, references);
      this.setMode(references.size ? "img2img" : "text2img", false);
      this.renderReferences();
      this.setComposerMode("generation");
      this.settingChanged();
      const omitted = requested.length - references.size;
      if (omitted > 0) {
        UI.toast(`当前渠道最多使用 ${max} 张垫图，已忽略 ${omitted} 张超限或已删除的参考图`);
      }
      this.el.promptInput.focus();
    }

    workspaceChatBusy(workspaceId = this.activeWorkspace?.id) {
      return Boolean(workspaceId && this.chatOperations.has(workspaceId));
    }

    startLocalChatOperation(workspaceId, kind, label) {
      this.chatOperations.set(workspaceId, {
        busy: true,
        kind,
        label,
        started_at: new Date().toISOString(),
        local: true,
      });
      this.renderWorkspaceList();
      if (this.activeWorkspace?.id === workspaceId) this.renderMessages();
    }

    async finishLocalChatOperation(workspaceId, failure = null) {
      if (this.chatOperations.get(workspaceId)?.local) {
        this.chatOperations.delete(workspaceId);
      }
      this.renderWorkspaceList();
      if (this.activeWorkspace?.id === workspaceId) this.renderMessages();
      if (!failure) return;
      UI.toast(failure.message, "error");
      await this.loadMessages(workspaceId);
    }

    syncServerChatOperation(workspaceId, operation) {
      const previous = this.chatOperations.get(workspaceId);
      if (previous?.local) return false;
      const next = operation?.busy ? { ...operation, local: false } : null;
      const unchanged = Boolean(previous) === Boolean(next)
        && (!next || (
          previous.kind === next.kind
          && previous.label === next.label
          && previous.started_at === next.started_at
        ));
      if (unchanged) return false;
      if (operation?.busy) {
        this.chatOperations.set(workspaceId, next);
      } else {
        this.chatOperations.delete(workspaceId);
      }
      return true;
    }

    workspaceHasActiveJob() {
      return this.jobs.some((job) => !TERMINAL.has(job.status));
    }

    updateInteractionState() {
      const generationBusy = this.workspaceHasActiveJob();
      const operation = this.chatOperations.get(this.activeWorkspace?.id);
      const chatBusy = Boolean(operation);
      const locked = this.workspaceLoading || generationBusy || chatBusy;
      const referenceUploading = this.referenceUploadPending;
      const hasModel = Boolean(this.el.chatModelSelect.value);
      setDisabled(this.el.chatInput, locked);
      setDisabled(this.el.chatSendButton, locked || referenceUploading || !hasModel);
      const sendTitle = referenceUploading ? "等待图片上传完成" : "发送消息";
      setAttribute(this.el.chatSendButton, "title", sendTitle);
      setAttribute(this.el.chatSendButton, "aria-label", sendTitle);
      setDisabled(this.el.chatModelSelect, locked || !hasModel);
      setDisabled(this.el.translatePrompt, locked);
      setDisabled(this.el.chatReferenceButton, locked || referenceUploading);
      setDisabled(
        this.el.draftPromptButton,
        locked || referenceUploading || !hasModel
          || !this.messages.some((message) => message.role === "user"),
      );
      setDisabled(this.el.generateButton, locked || referenceUploading || !this.currentChannel());
      setDisabled(this.el.generationBackButton, this.workspaceLoading || generationBusy);
      setDisabled(
        this.el.referenceAdd,
        this.workspaceLoading || referenceUploading
          || (this.activeWorkspace?.assets.length || 0) >= this.limits.max_assets_per_workspace,
      );
      setDisabled(this.el.clearWorkspaceButton, locked || referenceUploading);
      this.el.workspaceList.querySelectorAll("[data-delete-workspace]").forEach((button) => {
        const workspaceId = button.dataset.deleteWorkspace;
        const activeLocked = workspaceId === this.activeWorkspace?.id
          && (locked || referenceUploading);
        setDisabled(button, this.chatOperations.has(workspaceId) || activeLocked);
      });
      this.el.messageList.querySelectorAll("[data-retry-message]").forEach((button) => {
        setDisabled(button, locked || !hasModel);
      });
      this.el.messageList.querySelectorAll("[data-retry-job]").forEach((button) => {
        setDisabled(button, locked);
      });
      const placeholder = this.workspaceLoading
        ? "正在加载工作站..."
        : generationBusy
        ? "当前生成完成前不能继续对话，可在生成记录中取消任务"
        : chatBusy ? `${operation.label}，可切换到其他工作站继续`
        : this.isAnimationWorkspace() ? "描述画面、动作和循环方式..." : "描述你想生成的画面...";
      if (this.el.chatInput.placeholder !== placeholder) this.el.chatInput.placeholder = placeholder;
    }

    renderContextStatus() {
      const used = Number(this.conversationContext?.estimated_context_tokens || 0);
      const maximum = Number(this.conversationContext?.max_context_tokens || 0);
      const percent = maximum > 0 ? Math.min(100, Math.round(used / maximum * 100)) : 0;
      const compacted = this.conversationContext?.compacted ? " · 已压缩早期对话" : "";
      setText(this.el.contextStatus.querySelector("span"), `上下文 ${percent}%${compacted}`);
      const title = maximum
        ? `约 ${used.toLocaleString()} / ${maximum.toLocaleString()} tokens`
        : "当前会话上下文";
      setAttribute(this.el.contextStatus, "title", title);
    }

    scrollConversation() {
      if (this.scrollFrame !== null) window.cancelAnimationFrame(this.scrollFrame);
      this.scrollFrame = window.requestAnimationFrame(() => {
        this.el.conversationScroll.scrollTop = this.el.conversationScroll.scrollHeight;
        this.scrollFrame = null;
      });
    }

    toggleChatReferences() {
      if (this.workspaceLoading || this.workspaceChatBusy() || this.workspaceHasActiveJob()) return;
      this.chatReferencePickerOpen = !this.chatReferencePickerOpen;
      this.renderChatReferences();
    }

    openReferencePicker(target) {
      if (this.workspaceLoading || this.referenceUploadPending) return;
      this.uploadTarget = target;
      this.el.referenceInput.click();
    }

    chatCanAcceptImages() {
      return Boolean(this.activeWorkspace)
        && !this.workspaceLoading
        && !this.workspaceChatBusy()
        && !this.workspaceHasActiveJob()
        && !this.referenceUploadPending;
    }

    handleChatDrag(event) {
      if (![...(event.dataTransfer?.types || [])].includes("Files")) return;
      event.preventDefault();
      if (!this.chatCanAcceptImages()) {
        event.dataTransfer.dropEffect = "none";
        return;
      }
      event.dataTransfer.dropEffect = "copy";
      this.el.chatForm.classList.add("is-image-dragover");
    }

    handleChatDragLeave(event) {
      if (this.el.chatForm.contains(event.relatedTarget)) return;
      this.el.chatForm.classList.remove("is-image-dragover");
    }

    handleChatDrop(event) {
      if (![...(event.dataTransfer?.types || [])].includes("Files")) return;
      event.preventDefault();
      this.el.chatForm.classList.remove("is-image-dragover");
      if (!this.chatCanAcceptImages()) return;
      this.uploadReferences([...event.dataTransfer.files], "chat");
    }

    handleChatPaste(event) {
      const items = [...(event.clipboardData?.items || [])];
      const itemFiles = items
        .filter((item) => item.kind === "file")
        .map((item) => item.getAsFile())
        .filter(Boolean);
      const files = itemFiles.length
        ? itemFiles
        : [...(event.clipboardData?.files || [])];
      if (!files.length) return;
      event.preventDefault();
      if (!this.chatCanAcceptImages()) return;
      this.uploadReferences(files, "chat");
    }

    renderChatReferences() {
      const assets = this.activeWorkspace?.assets || [];
      const uploads = this.pendingReferenceUploads();
      const selection = this.currentChatSelection();
      const pickerOpen = this.chatReferencePickerOpen;
      const visibleAssets = pickerOpen
        ? assets
        : assets.filter((asset) => selection.has(asset.id));
      this.el.chatReferenceCount.textContent = selection.size;
      this.el.chatReferenceCount.hidden = selection.size === 0;
      this.el.chatReferenceStrip.hidden = !pickerOpen
        && selection.size === 0 && uploads.length === 0;
      this.el.chatReferenceStrip.classList.toggle("is-picker-open", pickerOpen);
      this.el.chatReferenceStrip.firstElementChild.textContent = pickerOpen
        ? "选择工作站图片"
        : "随消息发送";

      const upload = document.createElement("button");
      upload.type = "button";
      upload.className = "chat-reference-add";
      upload.dataset.uploadChatReference = "true";
      upload.disabled = assets.length + uploads.length >= this.limits.max_assets_per_workspace
        || this.referenceUploadPending;
      upload.title = this.referenceUploadPending
        ? "图片上传中"
        : assets.length + uploads.length >= this.limits.max_assets_per_workspace
        ? `工作站最多保留 ${this.limits.max_assets_per_workspace} 张参考图`
        : "上传参考图";
      upload.innerHTML = '<i data-lucide="image-plus"></i>';

      const cards = visibleAssets.map((asset) => {
        const card = document.createElement("span");
        card.className = "chat-reference-item";
        const toggle = document.createElement("button");
        toggle.type = "button";
        toggle.className = `chat-reference-card${selection.has(asset.id) ? " selected" : ""}`;
        toggle.dataset.chatReferenceToggle = asset.id;
        toggle.title = selection.has(asset.id) ? `取消 ${asset.name}` : `随消息发送 ${asset.name}`;
        const image = document.createElement("img");
        image.src = asset.url;
        image.alt = asset.name;
        image.decoding = "async";
        const check = document.createElement("span");
        check.innerHTML = '<i data-lucide="check"></i>';
        toggle.append(image, check);
        const remove = document.createElement("button");
        remove.type = "button";
        remove.className = "reference-remove chat-reference-remove";
        remove.dataset.referenceRemove = asset.id;
        remove.title = `删除 ${asset.name}`;
        remove.setAttribute("aria-label", `删除 ${asset.name}`);
        remove.innerHTML = '<i data-lucide="x"></i>';
        card.append(toggle, remove);
        return card;
      });
      const uploadCards = uploads.map((pending) => {
        const card = document.createElement("span");
        card.className = "chat-reference-item reference-upload-card";
        const preview = document.createElement("span");
        preview.className = `chat-reference-card reference-upload-preview is-${pending.state}`;
        const image = document.createElement("img");
        image.src = pending.previewUrl;
        image.alt = pending.file.name || "待上传图片";
        image.decoding = "async";
        const status = document.createElement("span");
        status.className = "reference-upload-status";
        status.title = pending.state === "canceling" ? "正在取消" : "正在上传";
        status.innerHTML = '<i data-lucide="loader-circle"></i>';
        const cancel = document.createElement("button");
        cancel.type = "button";
        cancel.className = "reference-remove chat-reference-remove";
        cancel.dataset.cancelReferenceUpload = pending.id;
        cancel.disabled = pending.state === "canceling";
        cancel.title = pending.state === "canceling" ? "正在取消" : "取消上传";
        cancel.setAttribute("aria-label", cancel.title);
        cancel.innerHTML = '<i data-lucide="x"></i>';
        preview.append(image, status);
        card.append(preview, cancel);
        return card;
      });
      this.el.chatReferenceList.replaceChildren(
        ...(pickerOpen ? [upload] : []),
        ...cards,
        ...uploadCards,
      );
      UI.icons(this.el.chatReferenceList);
    }

    async handleChatReferenceClick(event) {
      const cancelUpload = event.target.closest("[data-cancel-reference-upload]");
      if (cancelUpload) {
        this.cancelReferenceUpload(cancelUpload.dataset.cancelReferenceUpload);
        return;
      }
      if (this.workspaceLoading || this.workspaceChatBusy()
        || this.workspaceHasActiveJob() || this.referenceUploadPending) return;
      const remove = event.target.closest("[data-reference-remove]");
      if (remove) {
        await this.removeReference(remove.dataset.referenceRemove);
        return;
      }
      const upload = event.target.closest("[data-upload-chat-reference]");
      if (upload) {
        this.openReferencePicker("chat");
        return;
      }
      const toggle = event.target.closest("[data-chat-reference-toggle]");
      if (!toggle) return;
      const selection = this.currentChatSelection();
      const id = toggle.dataset.chatReferenceToggle;
      if (selection.has(id)) selection.delete(id);
      else if (selection.size < this.limits.max_chat_attachments) selection.add(id);
      this.renderChatReferences();
    }

    renderReferences() {
      const assets = this.activeWorkspace?.assets || [];
      const uploads = this.pendingReferenceUploads();
      const selected = this.currentSelection();
      const max = this.generationReferenceLimit();
      this.el.referenceLimit.textContent = `${selected.size} / ${max}`;
      this.el.referenceAdd.disabled = assets.length + uploads.length >= this.limits.max_assets_per_workspace
        || this.referenceUploadPending;
      this.el.referenceList.replaceChildren(
        ...assets.map((asset) => {
          const card = document.createElement("div");
          card.className = `reference-card${selected.has(asset.id) ? " selected" : ""}`;
          card.dataset.assetId = asset.id;
          const toggle = document.createElement("button");
          toggle.type = "button";
          toggle.className = "reference-toggle";
          toggle.dataset.referenceToggle = asset.id;
          toggle.title = selected.has(asset.id) ? "取消选择" : "选择为垫图";
          const image = document.createElement("img");
          image.src = asset.url;
          image.alt = asset.name;
          image.decoding = "async";
          const check = document.createElement("span");
          check.innerHTML = '<i data-lucide="check"></i>';
          toggle.append(image, check);
          const remove = document.createElement("button");
          remove.type = "button";
          remove.className = "reference-remove";
          remove.dataset.referenceRemove = asset.id;
          remove.title = "删除垫图";
          remove.setAttribute("aria-label", "删除垫图");
          remove.innerHTML = '<i data-lucide="x"></i>';
          card.append(toggle, remove);
          return card;
        }),
        ...uploads.map((pending) => {
          const card = document.createElement("div");
          card.className = "reference-card reference-upload-card";
          const preview = document.createElement("div");
          preview.className = `reference-toggle reference-upload-preview is-${pending.state}`;
          const image = document.createElement("img");
          image.src = pending.previewUrl;
          image.alt = pending.file.name || "待上传图片";
          image.decoding = "async";
          const status = document.createElement("span");
          status.className = "reference-upload-status";
          status.title = pending.state === "canceling" ? "正在取消" : "正在上传";
          status.innerHTML = '<i data-lucide="loader-circle"></i>';
          const cancel = document.createElement("button");
          cancel.type = "button";
          cancel.className = "reference-remove";
          cancel.dataset.cancelReferenceUpload = pending.id;
          cancel.disabled = pending.state === "canceling";
          cancel.title = pending.state === "canceling" ? "正在取消" : "取消上传";
          cancel.setAttribute("aria-label", cancel.title);
          cancel.innerHTML = '<i data-lucide="x"></i>';
          preview.append(image, status);
          card.append(preview, cancel);
          return card;
        }),
      );
      UI.icons(this.el.referenceList);
      if (this.isAnimationWorkspace()) {
        const mode = selected.size ? "img2img" : "text2img";
        if (this.el.modeSwitch.dataset.mode !== mode) this.setMode(mode, false);
      }
      this.updateWorkspaceKindUI();
      this.updatePrice();
    }

    uploadReferences(files, target) {
      this.el.referenceInput.value = "";
      const workspace = this.activeWorkspace;
      if (!files.length || !workspace || this.workspaceLoading || this.referenceUploadPending) return;
      const images = files.filter((file) => (
        REFERENCE_IMAGE_TYPES.has(file.type.toLowerCase())
        || REFERENCE_IMAGE_EXTENSION.test(file.name)
      ));
      if (!images.length) {
        UI.toast("仅支持 PNG、JPEG 和 WebP 图片", "error");
        return;
      }
      if (workspace.assets.length + this.pendingReferenceUploads(workspace.id).length + images.length
        > this.limits.max_assets_per_workspace) {
        UI.toast(`每个工作站最多保留 ${this.limits.max_assets_per_workspace} 张垫图`, "error");
        return;
      }
      const maxBytes = this.limits.max_attachment_mb * 1024 * 1024;
      if (images.some((file) => file.size > maxBytes)) {
        UI.toast(`单张垫图不能超过 ${this.limits.max_attachment_mb} MiB`, "error");
        return;
      }
      const maxTotalBytes = this.limits.max_attachment_total_mb * 1024 * 1024;
      if (images.reduce((total, file) => total + file.size, 0) > maxTotalBytes) {
        UI.toast(`垫图合计不能超过 ${this.limits.max_attachment_total_mb} MiB`, "error");
        return;
      }
      const normalizedTarget = target === "chat" ? "chat" : "generation";
      images.forEach((file) => {
        const id = `reference-upload-${++this.referenceUploadSequence}`;
        this.referenceUploads.set(id, {
          id,
          workspaceId: workspace.id,
          target: normalizedTarget,
          file,
          previewUrl: URL.createObjectURL(file),
          state: "queued",
          cancelRequested: false,
        });
        this.enqueueReferenceUpload(id);
      });
      if (files.length > images.length) {
        UI.toast(`已忽略 ${files.length - images.length} 个不支持的文件`, "info");
      }
      this.refreshReferenceUploadUI(workspace.id);
    }

    enqueueReferenceUpload(id) {
      const upload = this.referenceUploads.get(id);
      if (!upload) return;
      const previous = this.referenceUploadQueues.get(upload.workspaceId) || Promise.resolve();
      const task = previous.catch(() => {}).then(() => this.processReferenceUpload(id));
      this.referenceUploadQueues.set(upload.workspaceId, task);
      const clearQueue = () => {
        if (this.referenceUploadQueues.get(upload.workspaceId) === task) {
          this.referenceUploadQueues.delete(upload.workspaceId);
        }
      };
      task.then(clearQueue, clearQueue);
    }

    async processReferenceUpload(id) {
      const upload = this.referenceUploads.get(id);
      if (!upload) return;
      const workspace = this.workspaces.find((item) => item.id === upload.workspaceId);
      if (!workspace) {
        this.finishReferenceUpload(id);
        return;
      }
      upload.state = "uploading";
      this.refreshReferenceUploadUI(workspace.id);
      const data = new FormData();
      data.append("references", upload.file, upload.file.name);
      try {
        const payload = await UI.api(`/api/workspaces/${workspace.id}/assets`, {
          method: "POST", body: data,
        });
        const asset = payload.assets?.[0];
        if (!asset) throw new Error("图片上传结果无效");
        if (upload.cancelRequested) {
          try {
            await UI.api(`/api/workspaces/${workspace.id}/assets/${asset.id}`, { method: "DELETE" });
          } catch (error) {
            this.commitReferenceUpload(workspace, upload, asset);
            UI.toast(`${upload.file.name || "图片"}取消失败，已保留上传结果`, "error");
          }
        } else {
          this.commitReferenceUpload(workspace, upload, asset);
        }
      } catch (error) {
        if (!upload.cancelRequested) {
          UI.toast(`${upload.file.name || "图片"}：${error.message}`, "error");
        }
      } finally {
        this.finishReferenceUpload(id);
      }
    }

    commitReferenceUpload(workspace, upload, asset) {
      if (!workspace.assets.some((item) => item.id === asset.id)) workspace.assets.push(asset);
      const selection = upload.target === "chat"
        ? this.currentChatSelection(workspace.id)
        : this.currentSelection(workspace.id);
      const limit = this.referenceSelectionLimit(upload.target, workspace);
      this.trimReferenceSelection(selection, limit);
      if (selection.size < limit) selection.add(asset.id);
      if (workspace.kind === "animation" && upload.target === "generation"
        && this.activeWorkspace?.id === workspace.id) {
        this.setMode("img2img", true);
      }
      this.renderWorkspaceList();
    }

    cancelReferenceUpload(id) {
      const upload = this.referenceUploads.get(id);
      if (!upload || upload.workspaceId !== this.activeWorkspace?.id || upload.cancelRequested) return;
      upload.cancelRequested = true;
      if (upload.state === "queued") {
        this.finishReferenceUpload(id);
        return;
      }
      upload.state = "canceling";
      this.refreshReferenceUploadUI(upload.workspaceId);
    }

    finishReferenceUpload(id) {
      const upload = this.referenceUploads.get(id);
      if (!upload) return;
      this.referenceUploads.delete(id);
      URL.revokeObjectURL(upload.previewUrl);
      this.refreshReferenceUploadUI(upload.workspaceId);
    }

    refreshReferenceUploadUI(workspaceId) {
      if (this.activeWorkspace?.id !== workspaceId) return;
      this.renderReferences();
      this.renderChatReferences();
      this.updateInteractionState();
    }

    async handleReferenceClick(event) {
      const cancelUpload = event.target.closest("[data-cancel-reference-upload]");
      if (cancelUpload) {
        this.cancelReferenceUpload(cancelUpload.dataset.cancelReferenceUpload);
        return;
      }
      if (this.workspaceLoading || this.referenceUploadPending) return;
      const remove = event.target.closest("[data-reference-remove]");
      if (remove) {
        await this.removeReference(remove.dataset.referenceRemove);
        return;
      }
      const toggle = event.target.closest("[data-reference-toggle]");
      if (!toggle) return;
      const id = toggle.dataset.referenceToggle;
      const selection = this.currentSelection();
      if (selection.has(id)) selection.delete(id);
      else {
        const max = this.generationReferenceLimit();
        if (selection.size >= max) {
          UI.toast(`当前渠道最多选择 ${max} 张垫图`, "error");
          return;
        }
        selection.add(id);
      }
      if (this.isAnimationWorkspace()) {
        this.setMode(selection.size ? "img2img" : "text2img", true);
      }
      this.renderReferences();
    }

    async removeReference(id) {
      if (this.workspaceLoading || !this.activeWorkspace
        || this.workspaceChatBusy() || this.workspaceHasActiveJob()) return;
      if (!window.confirm("从工作站删除这张垫图？历史消息和任务中的引用仍会保留。")) return;
      const workspace = this.activeWorkspace;
      try {
        await UI.api(`/api/workspaces/${workspace.id}/assets/${id}`, { method: "DELETE" });
        workspace.assets = workspace.assets.filter((asset) => asset.id !== id);
        this.referenceSelections.get(workspace.id)?.delete(id);
        this.chatReferenceSelections.get(workspace.id)?.delete(id);
        if (this.activeWorkspace?.id === workspace.id) {
          if (workspace.kind === "animation") {
            this.setMode(this.currentSelection(workspace.id).size ? "img2img" : "text2img", true);
          }
          this.renderReferences();
          this.renderChatReferences();
        }
        this.renderWorkspaceList();
        UI.toast("参考图已删除", "success");
      } catch (error) {
        UI.toast(error.message, "error");
      }
    }

    updatePrice() {
      const count = this.animationNeedsMaster()
        ? 1
        : this.isAnimationWorkspace()
        ? Math.min(
          this.limits.max_animation_frames,
          Math.max(2, Number(this.el.animationFrameCount.value || 8)),
        )
        : Math.min(
          this.limits.max_batch_images, Math.max(1, Number(this.el.batchCount.value || 1)),
        );
      const needsMaster = this.animationNeedsMaster();
      const unit = !needsMaster && this.isAnimationWorkspace() ? "帧" : "张";
      const price = Number(this.currentChannel()?.price_rmb || 0);
      this.el.priceEstimateLabel.textContent = needsMaster ? "母图预计总价" : `${count} ${unit}预计总价`;
      this.el.priceEstimate.textContent = UI.money(price * count);
      this.channels.forEach((channel, index) => {
        const option = this.el.channelSelect.options[index];
        if (option) {
          const unitPrice = UI.money(channel.price_rmb);
          option.textContent = `${channel.label} · ${unitPrice}/${unit}${channel.configured ? "" : " · 未配置"}`;
        }
      });
    }

    async submitGeneration(event) {
      event.preventDefault();
      if (this.workspaceLoading) return;
      if (this.referenceUploadPending) {
        UI.toast("请等待图片上传完成或取消上传", "info");
        return;
      }
      if (!this.activeWorkspace) {
        UI.toast("暂无可用渠道", "error");
        return;
      }
      if (this.workspaceChatBusy()) {
        UI.toast("请等待当前 AI 回复完成后再开始生成", "error");
        return;
      }
      const workspace = this.activeWorkspace;
      const button = this.el.generateButton;
      if (button.classList.contains("loading")) return;
      button.disabled = true;
      button.classList.add("loading");
      try {
        await Promise.all([this.loadChannels(false), this.loadRuntimeSettings(false)]);
        if (this.activeWorkspace?.id !== workspace.id) return;
        if (!this.currentChannel()) {
          UI.toast("暂无可用渠道", "error");
          return;
        }
        if (!this.validateSizeInput(true)) return;
        const settings = this.collectSettings();
        const selection = this.currentSelection(workspace.id);
        const omitted = this.trimReferenceSelection(selection, this.generationReferenceLimit());
        if (omitted) {
          this.renderReferences();
          UI.toast(`渠道垫图上限已更新，已取消 ${omitted} 张超限图片`, "info");
        }
        const referenceIds = [...selection];
        const masterOnly = this.animationNeedsMaster();
        if (this.isAnimationWorkspace()) {
          settings.mode = masterOnly ? "text2img" : "img2img";
        }
        if (!settings.prompt.trim()) {
          UI.toast("请输入提示词", "error");
          this.el.promptInput.focus();
          return;
        }
        if (settings.mode === "img2img" && !referenceIds.length) {
          UI.toast("垫图生图至少选择一张垫图", "error");
          return;
        }
        const data = await UI.api("/api/generations", {
          method: "POST",
          body: {
            workspace_id: workspace.id,
            ...settings,
            reference_ids: masterOnly ? [] : settings.mode === "img2img" ? referenceIds : [],
            master_only: masterOnly,
          },
        });
        workspace.settings = settings;
        this.workspaceJobs.set(workspace.id, data.job);
        this.schedulePoll(ACTIVE_POLL_INTERVAL);
        this.updateWorkspaceJobDisplays();
        if (this.activeWorkspace?.id === workspace.id) {
          this.jobs.unshift(data.job);
          this.renderJobs();
          this.setComposerMode("chat");
        }
        await this.refreshBalance();
        const taskLabel = data.job.kind === "animation_master" ? "母图任务" : "任务";
        UI.toast(`${taskLabel}已提交，${UI.money(Number(data.job.price_per_image_rmb) * data.job.requested_count)} 已预占`, "success");
      } catch (error) {
        UI.toast(error.message, "error");
      } finally {
        button.classList.remove("loading");
        this.updateInteractionState();
      }
    }

    async loadJobs(workspaceId = this.activeWorkspace?.id) {
      if (!workspaceId) return;
      const existing = this.loadingJobWorkspaces.get(workspaceId);
      if (existing) return existing;
      const request = (async () => {
        try {
          const data = await UI.api(`/api/generations?workspace_id=${encodeURIComponent(workspaceId)}&limit=100`);
          const activeJob = data.jobs.find((job) => !TERMINAL.has(job.status));
          if (activeJob) this.workspaceJobs.set(workspaceId, activeJob);
          else this.workspaceJobs.delete(workspaceId);
          this.updateWorkspaceJobDisplays();
          if (this.activeWorkspace?.id === workspaceId) {
            this.jobs = data.jobs;
            this.renderJobs();
          }
        } catch (error) {
          UI.toast(error.message, "error");
        }
      })();
      this.loadingJobWorkspaces.set(workspaceId, request);
      try {
        return await request;
      } finally {
        if (this.loadingJobWorkspaces.get(workspaceId) === request) {
          this.loadingJobWorkspaces.delete(workspaceId);
        }
      }
    }

    async loadWorkspaceJobs() {
      if (this.loadingWorkspaceJobs) return this.loadingWorkspaceJobs;
      const request = (async () => {
        try {
          const data = await UI.api("/api/generations/active");
          this.workspaceJobs = new Map(data.jobs.map((job) => [job.workspace_id, job]));
          this.updateWorkspaceJobDisplays();
        } catch {
          // 当前工作站请求会显示持续性的 API 错误。
        }
      })();
      this.loadingWorkspaceJobs = request;
      try {
        return await request;
      } finally {
        if (this.loadingWorkspaceJobs === request) this.loadingWorkspaceJobs = null;
      }
    }

    renderJobs() {
      this.renderMessages();
      this.updateMetrics();
    }

    jobCard(job) {
      const article = document.createElement("article");
      article.innerHTML = `
        <header class="job-header">
          <div class="job-heading">
            <span class="status-badge" data-job-status><span></span><span data-job-status-label></span></span>
            <span class="queue-note" data-job-queue hidden></span>
            <span class="job-time" data-job-time></span>
          </div>
          <div class="job-actions">
            <span data-job-eta hidden><i data-lucide="clock-3"></i><span></span></span>
            <button class="button ghost small" type="button" data-job-retry hidden><i data-lucide="refresh-cw"></i>继续生成</button>
            <button class="button danger small" type="button" data-job-cancel hidden><i data-lucide="square"></i>取消</button>
          </div>
        </header>
        <div class="job-progress"><span data-job-progress></span></div>
        <div class="job-body">
          <div class="job-copy">
            <p data-job-prompt></p>
            <div class="job-meta">
              <span data-job-channel></span><span data-job-model></span>
              <span data-job-size></span><span data-job-quality></span>
              <span data-job-count></span><span data-job-charge></span>
            </div>
          </div>
          <div class="animation-result" data-animation-result hidden>
            <div class="animation-preview"><img data-animation-image alt="动画预览" decoding="async"></div>
            <div class="animation-result-bar">
              <span data-animation-meta></span>
              <a class="icon-button" data-animation-download download title="下载动画" aria-label="下载动画"><i data-lucide="download"></i></a>
            </div>
          </div>
          <div class="output-grid"></div>
        </div>`;
      this.updateJobCard(article, job);
      return article;
    }

    getJobElements(article) {
      let elements = this.jobElementCache.get(article);
      if (elements) return elements;
      const fields = {};
      article.querySelectorAll(JOB_ELEMENT_SELECTOR).forEach((element) => {
        Object.keys(element.dataset).forEach((key) => {
          fields[key] = element;
        });
        if (element.classList.contains("output-grid")) fields.outputGrid = element;
      });
      elements = {
        status: fields.jobStatus,
        statusLabel: fields.jobStatusLabel,
        queue: fields.jobQueue,
        time: fields.jobTime,
        eta: fields.jobEta,
        etaLabel: fields.jobEta.querySelector("span"),
        retry: fields.jobRetry,
        cancel: fields.jobCancel,
        progress: fields.jobProgress,
        prompt: fields.jobPrompt,
        channel: fields.jobChannel,
        model: fields.jobModel,
        size: fields.jobSize,
        quality: fields.jobQuality,
        count: fields.jobCount,
        charge: fields.jobCharge,
        animationResult: fields.animationResult,
        animationImage: fields.animationImage,
        animationMeta: fields.animationMeta,
        animationDownload: fields.animationDownload,
        outputGrid: fields.outputGrid,
      };
      this.jobElementCache.set(article, elements);
      return elements;
    }

    updateJobCard(article, job) {
      const [statusLabel, statusClass] = STATUS[job.status] || [job.status, ""];
      const enteringClass = article.classList.contains("timeline-enter") ? " timeline-enter" : "";
      const animationClass = job.kind === "animation" ? " animation-job" : "";
      const resultClass = job.animation_url ? " has-animation-result" : "";
      const className = `job-card timeline-job ${statusClass}${animationClass}${resultClass}${enteringClass}`;
      if (article.className !== className) article.className = className;
      if (article.dataset.jobId !== String(job.id)) article.dataset.jobId = job.id;
      if (article.dataset.jobStatus !== job.status) article.dataset.jobStatus = job.status;
      const elements = this.getJobElements(article);
      const statusClassName = `status-badge ${statusClass}`;
      if (elements.status.className !== statusClassName) elements.status.className = statusClassName;
      setText(elements.statusLabel, statusLabel);

      setHidden(elements.queue, job.status !== "queued");
      setText(elements.queue, job.status === "queued"
        ? `第 ${job.queue_position || "-"} 个任务 / 共 ${job.queue_total || 0} 个`
        : "");
      setText(elements.time, UI.dateTime(job.created_at));

      setHidden(elements.eta, !job.estimated_end_at);
      setText(elements.etaLabel, job.estimated_end_at
        ? (job.is_over_estimate ? "仍在处理" : `预计 ${UI.timeOnly(job.estimated_end_at)}`)
        : "");
      setHidden(elements.retry, !job.can_retry);
      if (job.can_retry) {
        if (elements.retry.dataset.retryJob !== String(job.id)) {
          elements.retry.dataset.retryJob = job.id;
        }
      } else if ("retryJob" in elements.retry.dataset) {
        delete elements.retry.dataset.retryJob;
      }
      setHidden(elements.cancel, !job.can_cancel);
      if (job.can_cancel) {
        if (elements.cancel.dataset.cancelJob !== String(job.id)) {
          elements.cancel.dataset.cancelJob = job.id;
        }
      } else if ("cancelJob" in elements.cancel.dataset) {
        delete elements.cancel.dataset.cancelJob;
      }

      const progressWidth = `${job.progress_percent}%`;
      if (elements.progress.style.width !== progressWidth) {
        elements.progress.style.width = progressWidth;
      }
      setText(elements.prompt, job.prompt);
      setText(elements.channel, job.channel);
      setText(elements.model, job.model);
      setText(elements.size, job.size);
      setText(elements.quality, job.quality);
      const unit = job.kind === "animation"
        ? "帧"
        : job.kind === "animation_master" ? "张母图" : "张";
      setText(elements.count, `${job.succeeded_count}/${job.requested_count} ${unit}`);
      setText(elements.charge, `${UI.money(job.charged_rmb)} 已扣`);
      setHidden(elements.animationResult, !job.animation_url);
      if (job.animation_url) {
        if (elements.animationImage.dataset.url !== job.animation_url) {
          elements.animationImage.dataset.url = job.animation_url;
          elements.animationImage.src = job.animation_url;
          this.prepareImageReveal(elements.animationImage);
        }
        const loopLabel = job.animation_loop ? "循环" : "单次";
        setText(
          elements.animationMeta,
          `${job.animation_fps} FPS · ${job.animation_duration_seconds} 秒 · ${loopLabel}`,
        );
        setAttribute(elements.animationDownload, "href", job.animation_download_url);
      }
      this.reconcileOutputTiles(elements.outputGrid, job);
      if (!elements.eta.hidden) UI.icons(elements.eta);
      if (!elements.retry.hidden) UI.icons(elements.retry);
      if (!elements.cancel.hidden) UI.icons(elements.cancel);
      if (!elements.animationResult.hidden) UI.icons(elements.animationDownload);
      return article;
    }

    reconcileOutputTiles(grid, job) {
      const existing = new Map(
        [...grid.children].map((node) => [node.dataset.itemId, node]),
      );
      const desired = new Set();
      job.items.forEach((item, index) => {
        desired.add(item.id);
        let tile = existing.get(item.id);
        if (tile) this.updateOutputTile(tile, job, item);
        else tile = this.outputTile(job, item);
        const current = grid.children[index];
        if (current !== tile) grid.insertBefore(tile, current || null);
      });
      [...grid.children].forEach((node) => {
        if (!desired.has(node.dataset.itemId)) node.remove();
      });
    }

    outputTile(job, item) {
      const button = document.createElement("button");
      button.type = "button";
      return this.updateOutputTile(button, job, item);
    }

    updateOutputTile(button, job, item) {
      const imageUrl = item.thumbnail_url || item.image_url || "";
      const imageArrived = button.isConnected && !button.dataset.imageUrl && Boolean(imageUrl);
      const contentChanged = button.dataset.imageUrl !== imageUrl
        || (!imageUrl && button.dataset.itemStatus !== item.status);
      const transparencyClass = job.transparent_background ? " has-transparency" : "";
      const arrivedClass = button.classList.contains("result-arrived") ? " result-arrived" : "";
      const className = `output-tile ${item.status}${transparencyClass}${arrivedClass}`;
      if (button.className !== className) button.className = className;
      if (button.dataset.jobId !== String(job.id)) button.dataset.jobId = job.id;
      if (button.dataset.itemId !== String(item.id)) button.dataset.itemId = item.id;
      if (button.dataset.itemStatus !== item.status) button.dataset.itemStatus = item.status;
      if (button.dataset.imageUrl !== imageUrl) button.dataset.imageUrl = imageUrl;
      setDisabled(button, !item.image_url);
      if (!contentChanged) return button;
      if (imageUrl) {
        const image = document.createElement("img");
        image.src = imageUrl;
        image.alt = job.kind === "animation"
          ? `动画第 ${item.position + 1} 帧`
          : job.kind === "animation_master" ? "帧动画母图" : `生成结果 ${item.position + 1}`;
        image.loading = "lazy";
        image.decoding = "async";
        this.prepareImageReveal(image);
        button.replaceChildren(image);
      } else {
        const placeholder = document.createElement("span");
        placeholder.className = "output-placeholder";
        const icon = ["failed", "interrupted"].includes(item.status)
          ? "circle-alert" : item.status === "canceled" ? "ban" : "loader-circle";
        placeholder.innerHTML = `<i data-lucide="${icon}"></i><small>${STATUS[item.status]?.[0] || "等待"}</small>`;
        button.replaceChildren(placeholder);
        UI.icons(button);
      }
      if (imageArrived) {
        button.classList.add("result-arrived");
        button.addEventListener("animationend", () => button.classList.remove("result-arrived"), { once: true });
      }
      return button;
    }

    applyJobUpdate(job) {
      const index = this.jobs.findIndex((entry) => entry.id === job.id);
      if (index >= 0) this.jobs[index] = job;
      if (TERMINAL.has(job.status)) this.workspaceJobs.delete(job.workspace_id);
      else this.workspaceJobs.set(job.workspace_id, job);
      this.updateWorkspaceJobDisplays();
      this.renderJobs();
    }

    async handleJobClick(event) {
      const retry = event.target.closest("[data-retry-job]");
      if (retry) {
        retry.disabled = true;
        try {
          const data = await UI.api(`/api/generations/${retry.dataset.retryJob}/retry`, {
            method: "POST",
          });
          this.applyJobUpdate(data.job);
          this.schedulePoll(ACTIVE_POLL_INTERVAL);
          await this.refreshBalance();
          const remaining = data.job.requested_count - data.job.succeeded_count;
          UI.toast(`已保留 ${data.job.succeeded_count} 帧，继续生成剩余 ${remaining} 帧`, "success");
        } catch (error) {
          retry.disabled = false;
          UI.toast(error.message, "error");
        }
        return;
      }
      const cancel = event.target.closest("[data-cancel-job]");
      if (cancel) {
        cancel.disabled = true;
        try {
          const data = await UI.api(`/api/generations/${cancel.dataset.cancelJob}/cancel`, { method: "POST" });
          this.applyJobUpdate(data.job);
          await this.refreshBalance();
          UI.toast("取消请求已提交", "success");
        } catch (error) {
          cancel.disabled = false;
          UI.toast(error.message, "error");
        }
        return;
      }
      const tile = event.target.closest("[data-item-id]");
      if (!tile || tile.disabled) return;
      const job = this.jobs.find((entry) => entry.id === tile.dataset.jobId);
      const item = job?.items.find((entry) => entry.id === tile.dataset.itemId);
      if (job && item) this.showDetail(job, item);
    }

    showDetail(job, item) {
      this.detailItemId = item.id;
      this.el.detailImage.src = item.image_url;
      this.el.detailImage.closest(".image-dialog-preview")
        ?.classList.toggle("has-transparency", job.transparent_background === true);
      this.prepareImageReveal(this.el.detailImage);
      this.el.detailPrompt.textContent = job.prompt;
      const transparentLabel = job.transparent_background ? " · 透明背景" : "";
      const animationLabel = job.kind === "animation"
        ? ` · 第 ${item.position + 1}/${job.requested_count} 帧 · ${job.animation_fps} FPS`
        : "";
      const details = [
        ["渠道", `${job.channel} · ${job.model}`],
        ["参数", `${job.size} · ${job.quality} · ${job.output_format.toUpperCase()}${transparentLabel}${animationLabel}`],
        ["图片", `${item.width || "-"} × ${item.height || "-"} · ${UI.formatBytes(item.bytes)}`],
        ["耗时", item.elapsed_seconds == null ? "--" : `${item.elapsed_seconds.toFixed(1)} 秒`],
        ["费用", UI.money(item.charged_rmb)],
        ["时间", UI.dateTime(item.completed_at)],
      ];
      this.el.detailList.innerHTML = details
        .map(([label, value]) => `<div><dt>${label}</dt><dd>${UI.escapeHtml(value)}</dd></div>`)
        .join("");
      this.el.detailReferences.innerHTML = job.references.length
        ? `<span>垫图</span><div>${job.references.map((asset) => `<img src="${asset.url}" alt="${UI.escapeHtml(asset.name)}" decoding="async">`).join("")}</div>`
        : "";
      this.el.detailReferences.querySelectorAll("img").forEach((image) => this.prepareImageReveal(image));
      this.el.detailReuseLabel.textContent = this.isAnimationWorkspace() ? "设为母图" : "基于此图继续";
      this.el.detailDownload.href = item.download_url;
      UI.openDialog(this.el.imageDialog);
    }

    async reuseDetailImage() {
      if (!this.detailItemId || !this.activeWorkspace) return;
      const workspace = this.activeWorkspace;
      const itemId = this.detailItemId;
      this.el.detailReuse.disabled = true;
      try {
        const data = await UI.api(`/api/generation-items/${itemId}/reference`, {
          method: "POST",
        });
        if (!workspace.assets.some((asset) => asset.id === data.asset.id)) {
          workspace.assets.push(data.asset);
        }
        this.renderWorkspaceList();
        if (workspace.kind === "animation" && this.activeWorkspace?.id === workspace.id) {
          const selection = this.currentSelection(workspace.id);
          selection.clear();
          selection.add(data.asset.id);
          this.setMode("img2img", false);
          this.renderReferences();
          this.settingChanged();
          await this.flushSettings();
          UI.closeDialog(this.el.imageDialog);
          this.setComposerMode("generation");
          this.el.promptInput.focus();
          UI.toast("已设为母图，可以调整帧动画参数", "success");
        } else if (this.activeWorkspace?.id === workspace.id) {
          const selection = this.currentChatSelection(workspace.id);
          selection.clear();
          selection.add(data.asset.id);
          this.chatReferencePickerOpen = true;
          this.renderChatReferences();
          this.renderReferences();
          this.setComposerMode("chat");
          this.el.chatInput.value = "请基于这张图继续调整：";
          UI.closeDialog(this.el.imageDialog);
          this.el.chatInput.focus();
        }
      } catch (error) {
        UI.toast(error.message, "error");
      } finally {
        this.el.detailReuse.disabled = false;
      }
    }

    updateMetrics() {
      const running = this.jobs.filter((job) => ["running", "canceling"].includes(job.status));
      const queued = this.jobs.filter((job) => job.status === "queued");
      setText(this.el.runningMetric, running.length);
      setText(this.el.queueMetric, queued.length);
      this.updateEtaMetric();
      const busy = running.length > 0 || queued.length > 0;
      this.el.workspaceStateDot.classList.toggle("busy", busy);
      setText(this.el.workspaceStatus, running.length
        ? `${running.length} 个任务正在生成`
        : queued.length ? `${queued.length} 个任务排队中` : "等待任务");
      this.updateInteractionState();
    }

    updateEtaMetric() {
      const running = this.jobs.filter((job) => ["running", "canceling"].includes(job.status));
      const ends = running.map((job) => job.estimated_end_at).filter(Boolean).sort();
      const latestEnd = ends.at(-1);
      setText(this.el.etaMetric, latestEnd ? UI.timeOnly(latestEnd) : "--:--");
      setText(this.el.etaRemainingMetric, latestEnd ? this.formatRemaining(latestEnd) : "--");
    }

    async refreshBalance() {
      try {
        const data = await UI.api("/api/me?ledger=0");
        this.user = data.user;
        UI.updateWallet(data.user, data.spending);
      } catch {
        // 后续请求会显示鉴权或网络错误。
      }
    }

    async loadChannels(notify = true) {
      try {
        const data = await UI.api("/api/channels");
        const initialized = Boolean(this.channelVersion);
        const changed = initialized && this.channelVersion !== data.version;
        this.channelVersion = data.version;
        if (initialized && !changed) return;
        const previous = this.collectSettings();
        this.channels = data.channels;
        this.renderChannelOptions(previous.channel_id);
        this.applyChannel(previous, false);
        if (changed && notify) UI.toast("渠道与模型配置已更新", "success");
      } catch (error) {
        if (notify) UI.toast(error.message, "error");
      }
    }

    async loadChatModels(notify = true) {
      try {
        const data = await UI.api("/api/chat-models");
        const initialized = Boolean(this.chatModelVersion);
        const changed = initialized && this.chatModelVersion !== data.version;
        this.chatModelVersion = data.version;
        if (initialized && !changed) return;
        const selected = this.el.chatModelSelect.value
          || this.activeWorkspace?.settings?.chat_model_id;
        this.chatModels = data.models;
        this.renderChatModelOptions(selected);
        if (changed && notify) UI.toast("对话模型配置已更新", "success");
      } catch (error) {
        if (notify) UI.toast(error.message, "error");
      }
    }

    async poll() {
      if (this.polling || document.hidden) return;
      this.polling = true;
      try {
        const selectedWasActive = this.jobs.some((job) => !TERMINAL.has(job.status));
        const hadActiveWorkspace = this.workspaceJobs.size > 0;
        await this.loadWorkspaceJobs();
        const selectedIsActive = this.workspaceJobs.has(this.activeWorkspace?.id);
        const requests = [...this.chatOperations]
          .filter(([, operation]) => !operation.local)
          .map(([workspaceId]) => this.loadMessages(workspaceId));
        if (selectedWasActive || selectedIsActive) requests.push(this.loadJobs());
        if (hadActiveWorkspace || this.workspaceJobs.size > 0 || selectedWasActive) {
          requests.push(this.refreshBalance());
        }
        await Promise.all(requests);
      } finally {
        this.polling = false;
      }
    }

    pollInterval() {
      if (document.hidden) return IDLE_POLL_INTERVAL;
      const active = this.workspaceJobs.size > 0
        || this.chatOperations.size > 0
        || this.jobs.some((job) => !TERMINAL.has(job.status));
      return active ? ACTIVE_POLL_INTERVAL : IDLE_POLL_INTERVAL;
    }

    schedulePoll(delay = this.pollInterval()) {
      window.clearTimeout(this.pollTimer);
      this.pollTimer = window.setTimeout(async () => {
        this.pollTimer = null;
        try {
          await this.poll();
        } finally {
          this.schedulePoll();
        }
      }, delay);
    }
  }

  document.addEventListener("DOMContentLoaded", () => new StudioApp());
})();
