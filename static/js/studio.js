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
    canceled: ["已取消", "canceled"],
  };
  const TERMINAL = new Set(["succeeded", "partial", "failed", "canceled"]);
  const IMAGE_SIZE_PATTERN = /^([1-9]\d{1,4})x([1-9]\d{1,4})$/;
  const IMAGE_DIMENSION_MIN = 64;
  const IMAGE_DIMENSION_MAX = 8192;
  const REFERENCE_IMAGE_TYPES = new Set(["image/png", "image/jpeg", "image/webp"]);
  const REFERENCE_IMAGE_EXTENSION = /\.(?:png|jpe?g|webp)$/i;

  class StudioApp {
    constructor() {
      this.bootstrap = JSON.parse(document.getElementById("bootstrapData").textContent);
      this.brandMarkUrl = document.getElementById("studioApp").dataset.brandMarkUrl;
      this.user = this.bootstrap.user;
      this.workspaces = this.bootstrap.workspaces;
      this.maxWorkspaces = this.bootstrap.max_workspaces;
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
      this.composerTransition = null;
      this.reducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)");
      this.loadingJobWorkspaces = new Set();
      this.loadingMessageWorkspaces = new Set();
      this.dialogMode = "create";
      this.composerMode = "chat";
      this.uploadTarget = "generation";
      this.referenceUploadPending = false;
      this.chatReferencePickerOpen = false;
      this.detailItemId = null;
      this.channelVersion = "";
      this.chatModelVersion = "";
      this.workspaces.forEach((workspace) => {
        if (workspace.conversation_operation?.busy) {
          this.chatOperations.set(workspace.id, {
            ...workspace.conversation_operation,
            local: false,
          });
        }
      });
      this.cacheElements();
      this.bindEvents();
      this.renderWorkspaceList();
      this.selectWorkspace(this.workspaces[0]?.id);
      this.loadChannels(false);
      this.loadChatModels(false);
      this.pollTimer = window.setInterval(() => this.poll(), 2200);
      this.channelTimer = window.setInterval(() => {
        this.loadChannels(false);
        this.loadChatModels(false);
      }, 15000);
    }

    cacheElements() {
      const byId = (id) => document.getElementById(id);
      this.el = {
        workspaceList: byId("workspaceList"),
        workspaceCount: byId("workspaceCount"),
        workspaceTitle: byId("workspaceTitle"),
        workspaceStatus: byId("workspaceStatus"),
        workspaceStateDot: byId("workspaceStateDot"),
        runningMetric: byId("runningMetric"),
        queueMetric: byId("queueMetric"),
        etaMetric: byId("etaMetric"),
        newWorkspaceButton: byId("newWorkspaceButton"),
        clearWorkspaceButton: byId("clearWorkspaceButton"),
        renameWorkspaceButton: byId("renameWorkspaceButton"),
        deleteWorkspaceButton: byId("deleteWorkspaceButton"),
        workspaceDialog: byId("workspaceDialog"),
        workspaceForm: byId("workspaceForm"),
        workspaceDialogTitle: byId("workspaceDialogTitle"),
        workspaceNameInput: byId("workspaceNameInput"),
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
        modeSwitch: byId("modeSwitch"),
        channelSelect: byId("channelSelect"),
        modelSelect: byId("modelSelect"),
        sizeInput: byId("sizeInput"),
        sizeOptions: byId("sizeOptions"),
        qualitySelect: byId("qualitySelect"),
        formatSelect: byId("formatSelect"),
        batchCount: byId("batchCount"),
        referenceStrip: byId("referenceStrip"),
        referenceInput: byId("referenceInput"),
        referenceAdd: byId("referenceAdd"),
        referenceList: byId("referenceList"),
        referenceLimit: byId("referenceLimit"),
        promptInput: byId("promptInput"),
        promptCounter: byId("promptCounter"),
        priceEstimate: byId("priceEstimate"),
        saveState: byId("saveState"),
        generateButton: byId("generateButton"),
        imageDialog: byId("imageDialog"),
        detailImage: byId("detailImage"),
        detailList: byId("detailList"),
        detailPrompt: byId("detailPrompt"),
        detailReferences: byId("detailReferences"),
        detailReuse: byId("detailReuse"),
        detailDownload: byId("detailDownload"),
      };
    }

    bindEvents() {
      this.el.newWorkspaceButton.addEventListener("click", () => this.showWorkspaceDialog("create"));
      this.el.clearWorkspaceButton.addEventListener("click", () => this.clearWorkspace());
      this.el.renameWorkspaceButton.addEventListener("click", () => this.showWorkspaceDialog("rename"));
      this.el.deleteWorkspaceButton.addEventListener("click", () => this.deleteWorkspace());
      this.el.workspaceForm.addEventListener("submit", (event) => this.saveWorkspaceName(event));
      this.el.workspaceList.addEventListener("click", (event) => {
        const button = event.target.closest("[data-workspace-id]");
        if (button) this.selectWorkspace(button.dataset.workspaceId);
      });
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
      [this.el.modelSelect, this.el.qualitySelect, this.el.formatSelect].forEach((field) => {
        field.addEventListener("change", () => this.settingChanged());
      });
      this.el.sizeInput.addEventListener("input", () => this.el.sizeInput.setCustomValidity(""));
      this.el.sizeInput.addEventListener("change", () => {
        if (this.validateSizeInput(true)) this.settingChanged();
      });
      this.el.batchCount.addEventListener("input", () => {
        this.updatePrice();
        this.settingChanged();
      });
      this.el.promptInput.addEventListener("input", () => {
        window.clearTimeout(this.promptCounterTimer);
        this.promptCounterTimer = window.setTimeout(() => {
          this.el.promptCounter.textContent = `${this.el.promptInput.value.length} / 8000`;
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
    }

    currentChannel() {
      return this.channels.find((channel) => channel.id === this.el.channelSelect.value) || null;
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

    renderWorkspaceList() {
      this.el.workspaceList.replaceChildren(
        ...this.workspaces.map((workspace) => {
          const operation = this.chatOperations.get(workspace.id);
          const button = document.createElement("button");
          button.type = "button";
          button.className = `workspace-item${workspace.id === this.activeWorkspace?.id ? " active" : ""}${operation ? " waiting" : ""}`;
          button.dataset.workspaceId = workspace.id;
          const icon = document.createElement("span");
          icon.className = "workspace-icon";
          icon.innerHTML = `<i data-lucide="${operation ? "loader-circle" : "panels-top-left"}"></i>`;
          const copy = document.createElement("span");
          copy.className = "workspace-copy";
          const name = document.createElement("strong");
          name.textContent = workspace.name;
          const meta = document.createElement("small");
          meta.textContent = operation?.label || `${workspace.assets.length} 张垫图`;
          copy.append(name, meta);
          button.append(icon, copy);
          return button;
        }),
      );
      this.el.workspaceCount.textContent = `${this.workspaces.length} / ${this.maxWorkspaces}`;
      this.el.newWorkspaceButton.disabled = this.workspaces.length >= this.maxWorkspaces;
      UI.icons(this.el.workspaceList);
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
      this.jobs = [];
      this.messages = [];
      this.conversationContext = null;
      this.composerMode = "chat";
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
      this.setComposerMode("chat", false);
      this.animateWorkspaceIn();
      await Promise.all([this.loadJobs(), this.loadMessages()]);
      if (selection !== this.workspaceLoadSequence) return;
      this.setWorkspaceLoading(false, selection);
    }

    setWorkspaceLoading(loading, selection) {
      window.clearTimeout(this.workspaceSkeletonTimer);
      this.workspaceLoading = loading;
      this.el.conversationLoading.hidden = true;
      this.el.conversationScroll.toggleAttribute("aria-busy", loading);
      this.el.messageList.hidden = loading;
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

    setComposerMode(mode, animate = true) {
      const nextMode = mode === "generation" ? "generation" : "chat";
      const incoming = nextMode === "chat" ? this.el.chatForm : this.el.generationForm;
      const outgoing = nextMode === "chat" ? this.el.generationForm : this.el.chatForm;
      const changed = this.composerMode !== nextMode || incoming.hidden;
      this.composerMode = nextMode;
      this.composerTransition?.cancel();
      outgoing.hidden = true;
      incoming.hidden = false;
      if (changed && animate && !this.reducedMotion.matches && typeof incoming.animate === "function") {
        const offset = nextMode === "generation" ? 8 : -6;
        const animation = incoming.animate(
          [
            { opacity: 0, transform: `translateY(${offset}px)` },
            { opacity: 1, transform: "translateY(0)" },
          ],
          { duration: 220, easing: "cubic-bezier(.22, 1, .36, 1)" },
        );
        this.composerTransition = animation;
        animation.finished.catch(() => {}).finally(() => {
          if (this.composerTransition === animation) this.composerTransition = null;
        });
      } else {
        this.composerTransition = null;
      }
      this.updateInteractionState();
    }

    applyWorkspaceSettings() {
      const settings = this.activeWorkspace?.settings || {};
      this.renderChatModelOptions(settings.chat_model_id);
      this.el.translatePrompt.checked = settings.translate_prompt === true;
      this.el.promptInput.value = settings.prompt || "";
      this.el.promptCounter.textContent = `${this.el.promptInput.value.length} / 8000`;
      this.el.batchCount.value = Math.min(20, Math.max(1, Number(settings.batch_count || 1)));
      const preferred = this.channels.find((channel) => channel.id === settings.channel_id && channel.configured)
        || this.channels.find((channel) => channel.configured)
        || this.channels[0];
      this.renderChannelOptions(preferred?.id);
      this.applyChannel(settings, false);
      this.setMode(settings.mode || "text2img", false);
      this.el.saveState.textContent = "参数已保存";
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
        option.textContent = `${channel.label} · ${UI.money(channel.price_rmb)}/张${channel.configured ? "" : " · 未配置"}`;
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
      document.querySelectorAll("[data-mode]").forEach((button) => {
        button.disabled = !channel.capabilities.modes.includes(button.dataset.mode);
      });
      if (!channel.capabilities.modes.includes(this.el.modeSwitch.dataset.mode)) {
        this.setMode(channel.capabilities.modes[0], false);
      }
      const selection = this.currentSelection();
      [...selection].slice(channel.capabilities.max_reference_images).forEach((id) => selection.delete(id));
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

    setMode(mode, shouldSave) {
      const channel = this.currentChannel();
      if (channel && !channel.capabilities.modes.includes(mode)) return;
      this.el.modeSwitch.dataset.mode = mode;
      this.el.modeSwitch.querySelectorAll("[data-mode]").forEach((button) => {
        const active = button.dataset.mode === mode;
        button.classList.toggle("active", active);
        button.setAttribute("aria-pressed", String(active));
      });
      this.el.referenceStrip.hidden = mode !== "img2img";
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
        batch_count: Math.min(20, Math.max(1, Number(this.el.batchCount.value || 1))),
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
      this.el.workspaceNameInput.value = mode === "rename" ? this.activeWorkspace?.name || "" : "";
      UI.openDialog(this.el.workspaceDialog);
      this.el.workspaceNameInput.focus();
    }

    async saveWorkspaceName(event) {
      event.preventDefault();
      const name = this.el.workspaceNameInput.value.trim();
      const submit = this.el.workspaceForm.querySelector('[type="submit"]');
      submit.disabled = true;
      try {
        if (this.dialogMode === "create") {
          const data = await UI.api("/api/workspaces", { method: "POST", body: { name } });
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

    async deleteWorkspace() {
      if (!this.activeWorkspace) return;
      if (this.workspaceChatBusy()) {
        UI.toast("请等待当前 AI 回复完成后再删除工作站", "error");
        return;
      }
      const name = this.activeWorkspace.name;
      if (!window.confirm(`删除“${name}”及其垫图、生成记录和图片？此操作不可恢复。`)) return;
      try {
        await UI.api(`/api/workspaces/${this.activeWorkspace.id}`, { method: "DELETE" });
        const index = this.workspaces.findIndex((item) => item.id === this.activeWorkspace.id);
        this.referenceSelections.delete(this.activeWorkspace.id);
        this.chatReferenceSelections.delete(this.activeWorkspace.id);
        this.chatDrafts.delete(this.activeWorkspace.id);
        this.chatOperations.delete(this.activeWorkspace.id);
        this.pendingUserMessages.delete(this.activeWorkspace.id);
        this.workspaces.splice(index, 1);
        this.activeWorkspace = null;
        if (!this.workspaces.length) {
          const data = await UI.api("/api/workspaces", { method: "POST", body: { name: "默认工作站" } });
          this.workspaces.push(data.workspace);
        }
        await this.selectWorkspace(this.workspaces[Math.max(0, index - 1)]?.id || this.workspaces[0].id);
        UI.toast("工作站已删除", "success");
      } catch (error) {
        UI.toast(error.message, "error");
      }
    }

    async clearWorkspace() {
      if (!this.activeWorkspace || this.workspaceHasActiveJob() || this.workspaceChatBusy()) {
        UI.toast("当前任务完成前不能清空会话", "error");
        return;
      }
      if (!window.confirm("清空当前会话、参考图和生成记录？工作站名称与参数会保留，此操作不可恢复。")) {
        return;
      }
      this.el.clearWorkspaceButton.disabled = true;
      try {
        const data = await UI.api(`/api/workspaces/${this.activeWorkspace.id}/clear`, {
          method: "POST",
        });
        Object.assign(this.activeWorkspace, data.workspace);
        this.messages = [];
        this.jobs = [];
        this.conversationContext = null;
        this.referenceSelections.set(this.activeWorkspace.id, new Set());
        this.chatReferenceSelections.set(this.activeWorkspace.id, new Set());
        this.chatDrafts.set(this.activeWorkspace.id, "");
        this.pendingUserMessages.delete(this.activeWorkspace.id);
        this.chatReferencePickerOpen = false;
        this.applyWorkspaceSettings();
        this.renderReferences();
        this.renderChatReferences();
        this.renderWorkspaceList();
        this.renderMessages();
        this.updateMetrics();
        this.setComposerMode("chat");
        UI.toast("当前会话已清空", "success");
      } catch (error) {
        UI.toast(error.message, "error");
      } finally {
        this.updateInteractionState();
      }
    }

    async loadMessages(workspaceId = this.activeWorkspace?.id) {
      if (!workspaceId) return;
      if (this.loadingMessageWorkspaces.has(workspaceId)) return;
      this.loadingMessageWorkspaces.add(workspaceId);
      try {
        const data = await UI.api(`/api/workspaces/${workspaceId}/messages?limit=200`);
        this.syncServerChatOperation(workspaceId, data.conversation_operation);
        this.renderWorkspaceList();
        if (this.activeWorkspace?.id === workspaceId) {
          this.messages = data.messages;
          this.conversationContext = data.context;
          this.renderMessages();
        }
      } catch (error) {
        UI.toast(error.message, "error");
      } finally {
        this.loadingMessageWorkspaces.delete(workspaceId);
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
      this.reconcileTimeline(timeline);
      this.el.conversationEmpty.hidden = this.workspaceLoading || timeline.length > 0;
      this.renderContextStatus();
      this.updateInteractionState();
      if (!this.workspaceLoading && keepAtBottom) this.scrollConversation();
    }

    reconcileTimeline(timeline) {
      const existing = new Map(
        [...this.el.messageList.children].map((node) => [node.dataset.timelineKey, node]),
      );
      const desiredKeys = new Set();
      timeline.forEach((entry, index) => {
        const key = `${entry.type}:${entry.id}`;
        desiredKeys.add(key);
        const isNew = !existing.has(key);
        let node = existing.get(key);
        if (entry.type === "job") {
          const unchangedTerminal = node
            && node.dataset.jobStatus === entry.value.status
            && TERMINAL.has(entry.value.status);
          if (!unchangedTerminal) {
            node = node ? this.updateJobCard(node, entry.value) : this.jobCard(entry.value);
          }
        } else if (!node) {
          node = this.messageCard(entry.value);
        }
        node.dataset.timelineKey = key;
        const current = this.el.messageList.children[index];
        if (current !== node) this.el.messageList.insertBefore(node, current || null);
        if (isNew) {
          node.classList.add("timeline-enter");
          const clearEntrance = (event) => {
            if (event.target !== node) return;
            node.classList.remove("timeline-enter");
            node.removeEventListener("animationend", clearEntrance);
          };
          node.addEventListener("animationend", clearEntrance);
          UI.icons(node);
        }
      });
      [...this.el.messageList.children].forEach((node) => {
        if (!desiredKeys.has(node.dataset.timelineKey)) node.remove();
      });
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
      promptLabel.textContent = payload.language === "en" ? "English prompt" : "生图提示词";
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
      if (!this.activeWorkspace || this.workspaceChatBusy() || this.workspaceHasActiveJob()) return;
      const workspace = this.activeWorkspace;
      const workspaceId = workspace.id;
      const modelId = this.el.chatModelSelect.value;
      const draft = this.el.chatInput.value;
      const content = draft.trim();
      const selection = this.currentChatSelection();
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
      this.startLocalChatOperation(workspaceId, "reply", "正在等待 AI 回复");
      let rejectedAsBusy = false;
      try {
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
        rejectedAsBusy = error.code === "conversation_busy";
        UI.toast(error.message, "error");
      } finally {
        this.finishLocalChatOperation(workspaceId);
        if (rejectedAsBusy) await this.loadMessages(workspaceId);
      }
    }

    async createPromptDraft() {
      if (!this.activeWorkspace || this.workspaceChatBusy() || this.workspaceHasActiveJob()) return;
      if (!this.messages.some((message) => message.role === "user")) {
        UI.toast("请先描述需要生成的图片", "error");
        this.el.chatInput.focus();
        return;
      }
      const workspace = this.activeWorkspace;
      const workspaceId = workspace.id;
      const modelId = this.el.chatModelSelect.value;
      const translateToEnglish = this.el.translatePrompt.checked;
      this.startLocalChatOperation(workspaceId, "prompt_draft", "正在整理生图提示词");
      let rejectedAsBusy = false;
      try {
        const data = await UI.api(`/api/workspaces/${workspaceId}/prompt-drafts`, {
          method: "POST",
          body: {
            model_id: modelId,
            translate_to_english: translateToEnglish,
          },
        });
        if (this.activeWorkspace?.id === workspaceId) {
          this.messages.push(data.message);
          this.conversationContext = data.context;
        }
        workspace.settings.translate_prompt = translateToEnglish;
      } catch (error) {
        rejectedAsBusy = error.code === "conversation_busy";
        UI.toast(error.message, "error");
      } finally {
        this.finishLocalChatOperation(workspaceId);
        if (rejectedAsBusy) await this.loadMessages(workspaceId);
      }
    }

    applyPromptDraft(messageId) {
      const message = this.messages.find((item) => item.id === messageId);
      const prompt = message?.payload?.prompt;
      if (!prompt) return;
      this.el.promptInput.value = prompt;
      this.el.promptCounter.textContent = `${prompt.length} / 8000`;
      const activeIds = new Set((this.activeWorkspace?.assets || []).map((asset) => asset.id));
      const requested = [...new Set(message.payload.reference_ids || [])];
      const available = requested.filter((id) => activeIds.has(id));
      const max = this.currentChannel()?.capabilities.max_reference_images || 0;
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

    finishLocalChatOperation(workspaceId) {
      if (this.chatOperations.get(workspaceId)?.local) {
        this.chatOperations.delete(workspaceId);
      }
      this.renderWorkspaceList();
      if (this.activeWorkspace?.id === workspaceId) this.renderMessages();
    }

    syncServerChatOperation(workspaceId, operation) {
      if (this.chatOperations.get(workspaceId)?.local) return;
      if (operation?.busy) {
        this.chatOperations.set(workspaceId, { ...operation, local: false });
      } else {
        this.chatOperations.delete(workspaceId);
      }
    }

    workspaceHasActiveJob() {
      return this.jobs.some((job) => !TERMINAL.has(job.status));
    }

    updateInteractionState() {
      const generationBusy = this.workspaceHasActiveJob();
      const operation = this.chatOperations.get(this.activeWorkspace?.id);
      const chatBusy = Boolean(operation);
      const locked = generationBusy || chatBusy;
      const hasModel = Boolean(this.el.chatModelSelect.value);
      this.el.chatInput.disabled = locked || !hasModel;
      this.el.chatSendButton.disabled = locked || !hasModel;
      this.el.chatModelSelect.disabled = locked || !hasModel;
      this.el.translatePrompt.disabled = locked;
      this.el.chatReferenceButton.disabled = locked || this.referenceUploadPending;
      this.el.draftPromptButton.disabled = locked || !hasModel
        || !this.messages.some((message) => message.role === "user");
      this.el.generateButton.disabled = locked || !this.currentChannel();
      this.el.generationBackButton.disabled = generationBusy;
      this.el.clearWorkspaceButton.disabled = locked;
      this.el.deleteWorkspaceButton.disabled = locked;
      this.el.chatInput.placeholder = generationBusy
        ? "图片生成完成前不能继续对话，可在生成记录中取消任务"
        : chatBusy ? `${operation.label}，可切换到其他工作站继续` : "描述你想生成的画面...";
    }

    renderContextStatus() {
      const used = Number(this.conversationContext?.estimated_context_tokens || 0);
      const maximum = Number(this.conversationContext?.max_context_tokens || 0);
      const percent = maximum > 0 ? Math.min(100, Math.round(used / maximum * 100)) : 0;
      const compacted = this.conversationContext?.compacted ? " · 已压缩早期对话" : "";
      this.el.contextStatus.querySelector("span").textContent = `上下文 ${percent}%${compacted}`;
      this.el.contextStatus.title = maximum
        ? `约 ${used.toLocaleString()} / ${maximum.toLocaleString()} tokens`
        : "当前会话上下文";
    }

    scrollConversation() {
      window.requestAnimationFrame(() => {
        this.el.conversationScroll.scrollTop = this.el.conversationScroll.scrollHeight;
      });
    }

    toggleChatReferences() {
      if (this.workspaceChatBusy() || this.workspaceHasActiveJob()) return;
      this.chatReferencePickerOpen = !this.chatReferencePickerOpen;
      this.renderChatReferences();
    }

    openReferencePicker(target) {
      this.uploadTarget = target;
      this.el.referenceInput.click();
    }

    chatCanAcceptImages() {
      return Boolean(this.activeWorkspace)
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
      const selection = this.currentChatSelection();
      this.el.chatReferenceCount.textContent = selection.size;
      this.el.chatReferenceCount.hidden = selection.size === 0;
      this.el.chatReferenceStrip.hidden = !this.chatReferencePickerOpen && selection.size === 0;

      const upload = document.createElement("button");
      upload.type = "button";
      upload.className = "chat-reference-add";
      upload.dataset.uploadChatReference = "true";
      upload.disabled = assets.length >= 8 || this.referenceUploadPending;
      upload.title = assets.length >= 8 ? "工作站最多保留 8 张参考图" : "上传参考图";
      upload.innerHTML = '<i data-lucide="image-plus"></i>';

      const cards = assets.map((asset) => {
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
      this.el.chatReferenceList.replaceChildren(upload, ...cards);
      UI.icons(this.el.chatReferenceList);
    }

    async handleChatReferenceClick(event) {
      if (this.workspaceChatBusy() || this.workspaceHasActiveJob() || this.referenceUploadPending) return;
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
      else if (selection.size < 8) selection.add(id);
      this.renderChatReferences();
    }

    renderReferences() {
      const assets = this.activeWorkspace?.assets || [];
      const selected = this.currentSelection();
      const max = this.currentChannel()?.capabilities.max_reference_images || 0;
      this.el.referenceLimit.textContent = `${selected.size} / ${max}`;
      this.el.referenceAdd.disabled = assets.length >= 8;
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
      );
      UI.icons(this.el.referenceList);
    }

    async uploadReferences(files, target) {
      this.el.referenceInput.value = "";
      const workspace = this.activeWorkspace;
      if (!files.length || !workspace || this.referenceUploadPending) return;
      const images = files.filter((file) => (
        REFERENCE_IMAGE_TYPES.has(file.type.toLowerCase())
        || REFERENCE_IMAGE_EXTENSION.test(file.name)
      ));
      if (!images.length) {
        UI.toast("仅支持 PNG、JPEG 和 WebP 图片", "error");
        return;
      }
      if (workspace.assets.length + images.length > 8) {
        UI.toast("每个工作站最多保留 8 张垫图", "error");
        return;
      }
      const selection = target === "chat"
        ? this.currentChatSelection(workspace.id)
        : this.currentSelection(workspace.id);
      const max = target === "chat"
        ? 8
        : (this.currentChannel()?.capabilities.max_reference_images || 0);
      const data = new FormData();
      images.forEach((file) => data.append("references", file, file.name));
      this.referenceUploadPending = true;
      this.el.referenceAdd.disabled = true;
      this.el.chatReferenceButton.disabled = true;
      try {
        const payload = await UI.api(`/api/workspaces/${workspace.id}/assets`, {
          method: "POST", body: data,
        });
        workspace.assets.push(...payload.assets);
        payload.assets.forEach((asset) => {
          if (selection.size < max) selection.add(asset.id);
        });
        if (workspace === this.activeWorkspace) {
          this.renderReferences();
          this.renderChatReferences();
        }
        this.renderWorkspaceList();
        const skipped = files.length - images.length;
        UI.toast(
          skipped
            ? `已保存 ${payload.assets.length} 张参考图，忽略 ${skipped} 个不支持的文件`
            : `已保存 ${payload.assets.length} 张参考图`,
          skipped ? "info" : "success",
        );
      } catch (error) {
        UI.toast(error.message, "error");
      } finally {
        this.referenceUploadPending = false;
        this.el.referenceAdd.disabled = this.activeWorkspace?.assets.length >= 8;
        this.updateInteractionState();
      }
    }

    async handleReferenceClick(event) {
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
        const max = this.currentChannel()?.capabilities.max_reference_images || 0;
        if (selection.size >= max) {
          UI.toast(`当前渠道最多选择 ${max} 张垫图`, "error");
          return;
        }
        selection.add(id);
      }
      this.renderReferences();
    }

    async removeReference(id) {
      if (!this.activeWorkspace || this.workspaceChatBusy() || this.workspaceHasActiveJob()) return;
      if (!window.confirm("从工作站删除这张垫图？历史消息和任务中的引用仍会保留。")) return;
      const workspace = this.activeWorkspace;
      try {
        await UI.api(`/api/workspaces/${workspace.id}/assets/${id}`, { method: "DELETE" });
        workspace.assets = workspace.assets.filter((asset) => asset.id !== id);
        this.referenceSelections.get(workspace.id)?.delete(id);
        this.chatReferenceSelections.get(workspace.id)?.delete(id);
        if (this.activeWorkspace?.id === workspace.id) {
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
      const count = Math.min(20, Math.max(1, Number(this.el.batchCount.value || 1)));
      const price = Number(this.currentChannel()?.price_rmb || 0);
      this.el.priceEstimate.textContent = UI.money(price * count);
    }

    async submitGeneration(event) {
      event.preventDefault();
      if (!this.activeWorkspace || !this.currentChannel()) {
        UI.toast("暂无可用渠道", "error");
        return;
      }
      if (this.workspaceChatBusy()) {
        UI.toast("请等待当前 AI 回复完成后再生成图片", "error");
        return;
      }
      if (!this.validateSizeInput(true)) return;
      const workspace = this.activeWorkspace;
      const settings = this.collectSettings();
      const referenceIds = [...this.currentSelection(workspace.id)];
      if (!settings.prompt.trim()) {
        UI.toast("请输入提示词", "error");
        this.el.promptInput.focus();
        return;
      }
      if (settings.mode === "img2img" && !referenceIds.length) {
        UI.toast("垫图生图至少选择一张垫图", "error");
        return;
      }
      const button = this.el.generateButton;
      button.disabled = true;
      button.classList.add("loading");
      try {
        const data = await UI.api("/api/generations", {
          method: "POST",
          body: {
            workspace_id: workspace.id,
            ...settings,
            reference_ids: settings.mode === "img2img" ? referenceIds : [],
          },
        });
        workspace.settings = settings;
        if (this.activeWorkspace?.id === workspace.id) {
          this.jobs.unshift(data.job);
          this.renderJobs();
          this.setComposerMode("chat");
        }
        await this.refreshBalance();
        UI.toast(`任务已提交，${UI.money(Number(data.job.price_per_image_rmb) * data.job.requested_count)} 已预占`, "success");
      } catch (error) {
        UI.toast(error.message, "error");
      } finally {
        button.classList.remove("loading");
        this.updateInteractionState();
      }
    }

    async loadJobs() {
      if (!this.activeWorkspace) return;
      const workspaceId = this.activeWorkspace.id;
      if (this.loadingJobWorkspaces.has(workspaceId)) return;
      this.loadingJobWorkspaces.add(workspaceId);
      try {
        const data = await UI.api(`/api/generations?workspace_id=${encodeURIComponent(workspaceId)}&limit=100`);
        if (this.activeWorkspace?.id === workspaceId) {
          this.jobs = data.jobs;
          this.renderJobs();
        }
      } catch (error) {
        UI.toast(error.message, "error");
      } finally {
        this.loadingJobWorkspaces.delete(workspaceId);
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
          <div class="output-grid"></div>
        </div>`;
      return this.updateJobCard(article, job);
    }

    updateJobCard(article, job) {
      const [statusLabel, statusClass] = STATUS[job.status] || [job.status, ""];
      article.className = `job-card timeline-job ${statusClass}`;
      article.dataset.jobId = job.id;
      article.dataset.jobStatus = job.status;
      const status = article.querySelector("[data-job-status]");
      status.className = `status-badge ${statusClass}`;
      status.querySelector("[data-job-status-label]").textContent = statusLabel;

      const queue = article.querySelector("[data-job-queue]");
      queue.hidden = job.status !== "queued";
      queue.textContent = job.status === "queued"
        ? `第 ${job.queue_position || "-"} 个任务 / 共 ${job.queue_total || 0} 个`
        : "";
      article.querySelector("[data-job-time]").textContent = UI.dateTime(job.created_at);

      const eta = article.querySelector("[data-job-eta]");
      eta.hidden = !job.estimated_end_at;
      eta.querySelector("span").textContent = job.estimated_end_at
        ? (job.is_over_estimate ? "仍在处理" : `预计 ${UI.timeOnly(job.estimated_end_at)}`)
        : "";
      const cancel = article.querySelector("[data-job-cancel]");
      cancel.hidden = !job.can_cancel;
      if (job.can_cancel) cancel.dataset.cancelJob = job.id;
      else delete cancel.dataset.cancelJob;

      article.querySelector("[data-job-progress]").style.width = `${job.progress_percent}%`;
      article.querySelector("[data-job-prompt]").textContent = job.prompt;
      article.querySelector("[data-job-channel]").textContent = job.channel;
      article.querySelector("[data-job-model]").textContent = job.model;
      article.querySelector("[data-job-size]").textContent = job.size;
      article.querySelector("[data-job-quality]").textContent = job.quality;
      article.querySelector("[data-job-count]").textContent = `${job.succeeded_count}/${job.requested_count} 张`;
      article.querySelector("[data-job-charge]").textContent = `${UI.money(job.charged_rmb)} 已扣`;
      this.reconcileOutputTiles(article.querySelector(".output-grid"), job);
      UI.icons(article);
      return article;
    }

    reconcileOutputTiles(grid, job) {
      const existing = new Map(
        [...grid.children].map((node) => [node.dataset.itemId, node]),
      );
      const desired = new Set();
      job.items.forEach((item, index) => {
        desired.add(item.id);
        const tile = existing.get(item.id) || this.outputTile(job, item);
        this.updateOutputTile(tile, job, item);
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
      button.className = `output-tile ${item.status}`;
      button.dataset.jobId = job.id;
      button.dataset.itemId = item.id;
      button.dataset.itemStatus = item.status;
      button.dataset.imageUrl = imageUrl;
      button.disabled = !item.image_url;
      if (!contentChanged) return button;
      if (imageUrl) {
        const image = document.createElement("img");
        image.src = imageUrl;
        image.alt = `生成结果 ${item.position + 1}`;
        image.loading = "lazy";
        this.prepareImageReveal(image);
        button.replaceChildren(image);
      } else {
        const placeholder = document.createElement("span");
        placeholder.className = "output-placeholder";
        const icon = item.status === "failed" ? "circle-alert" : item.status === "canceled" ? "ban" : "loader-circle";
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

    async handleJobClick(event) {
      const cancel = event.target.closest("[data-cancel-job]");
      if (cancel) {
        cancel.disabled = true;
        try {
          const data = await UI.api(`/api/generations/${cancel.dataset.cancelJob}/cancel`, { method: "POST" });
          const index = this.jobs.findIndex((job) => job.id === data.job.id);
          if (index >= 0) this.jobs[index] = data.job;
          this.renderJobs();
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
      this.prepareImageReveal(this.el.detailImage);
      this.el.detailPrompt.textContent = job.prompt;
      const details = [
        ["渠道", `${job.channel} · ${job.model}`],
        ["参数", `${job.size} · ${job.quality} · ${job.output_format.toUpperCase()}`],
        ["图片", `${item.width || "-"} × ${item.height || "-"} · ${UI.formatBytes(item.bytes)}`],
        ["耗时", item.elapsed_seconds == null ? "--" : `${item.elapsed_seconds.toFixed(1)} 秒`],
        ["费用", UI.money(item.charged_rmb)],
        ["时间", UI.dateTime(item.completed_at)],
      ];
      this.el.detailList.innerHTML = details
        .map(([label, value]) => `<div><dt>${label}</dt><dd>${UI.escapeHtml(value)}</dd></div>`)
        .join("");
      this.el.detailReferences.innerHTML = job.references.length
        ? `<span>垫图</span><div>${job.references.map((asset) => `<img src="${asset.url}" alt="${UI.escapeHtml(asset.name)}">`).join("")}</div>`
        : "";
      this.el.detailReferences.querySelectorAll("img").forEach((image) => this.prepareImageReveal(image));
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
        const selection = this.currentChatSelection(workspace.id);
        selection.clear();
        selection.add(data.asset.id);
        this.renderWorkspaceList();
        if (this.activeWorkspace?.id === workspace.id) {
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
      this.el.runningMetric.textContent = running.length;
      this.el.queueMetric.textContent = queued.length;
      const ends = running.map((job) => job.estimated_end_at).filter(Boolean).sort();
      this.el.etaMetric.textContent = ends.length ? UI.timeOnly(ends[ends.length - 1]) : "--:--";
      const busy = running.length > 0 || queued.length > 0;
      this.el.workspaceStateDot.classList.toggle("busy", busy);
      this.el.workspaceStatus.textContent = running.length
        ? `${running.length} 个任务正在生成`
        : queued.length ? `${queued.length} 个任务排队中` : "等待任务";
      this.updateInteractionState();
    }

    async refreshBalance() {
      try {
        const data = await UI.api("/api/me");
        this.user = data.user;
        UI.updateWallet(data.user, data.spending);
      } catch {
        // A later request will surface authentication or network errors.
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
      if (this.jobs.some((job) => !TERMINAL.has(job.status))) {
        await this.loadJobs();
        await this.refreshBalance();
      }
      const remoteOperations = [...this.chatOperations]
        .filter(([, operation]) => !operation.local)
        .map(([workspaceId]) => this.loadMessages(workspaceId));
      if (remoteOperations.length) await Promise.all(remoteOperations);
    }
  }

  document.addEventListener("DOMContentLoaded", () => new StudioApp());
})();
