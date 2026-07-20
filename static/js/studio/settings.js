(() => {
  "use strict";

  const {
    StudioApp,
    UI,
    COMPOSER_CLOSE_TIMEOUT,
    IMAGE_SIZE_PATTERN,
    IMAGE_DIMENSION_MIN,
    IMAGE_DIMENSION_MAX,
    setAttribute,
  } = window.ImageGenStudio;

  Object.assign(StudioApp.prototype, {
    setComposerMode(mode) {
      const generation = mode === "generation";
      window.clearTimeout(this.composerCloseTimer);
      this.composerCloseTimer = null;
      this.el.chatForm.hidden = generation || !this.activeWorkspace;
      if (!generation && !this.el.generationForm.hidden) {
        this.el.generationBackdrop.hidden = true;
        this.el.generationForm.classList.add("is-closing");
        this.composerCloseTimer = window.setTimeout(() => {
          this.finishComposerClose();
        }, COMPOSER_CLOSE_TIMEOUT);
      } else {
        this.el.generationForm.classList.remove("is-closing");
        this.el.generationBackdrop.hidden = !generation;
        this.el.generationForm.hidden = !generation;
      }
      this.updateInteractionState();
    },

    finishComposerClose(event = null) {
      if (event && (event.target !== this.el.generationForm
        || event.animationName !== "generation-composer-out")) return;
      if (!this.el.generationForm.classList.contains("is-closing")) return;
      window.clearTimeout(this.composerCloseTimer);
      this.composerCloseTimer = null;
      this.el.generationForm.hidden = true;
      this.el.generationBackdrop.hidden = true;
      this.el.generationForm.classList.remove("is-closing");
    },

    updatePromptCounter() {
      this.el.promptCounter.textContent = `${this.el.promptInput.value.length} / ${this.limits.max_prompt_characters}`;
    },

    async copyPrompt() {
      const prompt = this.el.promptInput.value;
      if (!prompt.trim()) {
        UI.toast("暂无可复制的提示词", "info");
        return;
      }
      try {
        await navigator.clipboard.writeText(prompt);
        UI.toast("提示词已复制", "success");
      } catch (_error) {
        UI.toast("复制失败，请手动复制", "error");
      }
    },

    applyWorkspaceSettings() {
      const settings = this.activeWorkspace?.settings || {};
      const activeAssetIds = new Set(this.activeWorkspace?.assets.map((asset) => asset.id) || []);
      const savedReferenceIds = Array.isArray(settings.reference_ids)
        ? settings.reference_ids
        : [];
      const referenceSelection = new Set(
        savedReferenceIds.filter((id) => activeAssetIds.has(id)),
      );
      this.referenceSelections.set(this.activeWorkspace.id, referenceSelection);
      this.renderChatModelOptions(settings.chat_model_id);
      this.renderCreativeDirectionOptions(settings.creative_direction_id || "auto");
      this.el.translatePrompt.checked = settings.translate_prompt === true;
      this.el.transparentBackground.checked = settings.transparent_background === true;
      this.el.promptInput.value = settings.prompt || "";
      this.updatePromptCounter();
      this.el.batchCount.value = Math.min(
        this.limits.max_batch_images, Math.max(1, Number(settings.batch_count || 1)),
      );
      const preferred = this.channels.find((channel) => channel.id === settings.channel_id && channel.configured)
        || this.channels.find((channel) => channel.configured)
        || this.channels[0];
      this.renderChannelOptions(preferred?.id);
      this.applyChannel(settings, false);
      this.setMode(settings.mode || "text2img", false);
      this.el.saveState.textContent = this.workspaceSettingSaves.has(this.activeWorkspace.id)
        ? "正在保存..."
        : "参数已保存";
      this.updatePromptReviewState();
    },

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
    },

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
    },

    applyChannel(saved = null, shouldSave = false) {
      const channel = this.currentChannel();
      if (!channel) {
        [this.el.modelSelect, this.el.formatSelect].forEach((field) => {
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
    },

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
    },

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
    },

    normalizeSize(value) {
      return String(value || "").trim().toLowerCase().replaceAll("×", "x");
    },

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
    },

    updateTransparentBackgroundState() {
      const available = ["png", "webp"].includes(this.el.formatSelect.value);
      this.el.transparentBackground.disabled = !available;
      if (!available) this.el.transparentBackground.checked = false;
      this.el.transparentBackgroundControl.classList.toggle("is-disabled", !available);
      this.el.transparentBackgroundControl.title = available
        ? "生成包含 Alpha 通道的透明背景图片"
        : "透明背景仅支持 PNG 或 WebP";
    },

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
      this.updatePrice();
      this.updatePromptReviewState();
      if (shouldSave) this.settingChanged();
    },

    currentPromptDraft() {
      const draftId = this.activeWorkspace?.settings?.prompt_draft_id;
      if (!draftId) return null;
      const draft = this.messages.find((message) => message.id === draftId);
      const payload = draft?.payload || {};
      if (draft?.kind !== "prompt_draft" || payload.status !== "ready") return null;
      if ((payload.prompt || "").trim() !== this.el.promptInput.value.trim()) return null;
      const mode = this.el.modeSwitch.dataset.mode;
      if (payload.generation_mode !== mode) return null;
      const selectedDirection = this.el.creativeDirectionSelect.value || "auto";
      if (selectedDirection !== "auto" && payload.creative_direction !== selectedDirection) {
        return null;
      }
      const expectedReferences = mode === "img2img" ? (payload.reference_ids || []) : [];
      const selectedReferences = mode === "img2img" ? [...this.currentSelection()] : [];
      if (expectedReferences.length !== selectedReferences.length
        || expectedReferences.some((id, index) => id !== selectedReferences[index])) return null;
      return draft;
    },

    updatePromptReviewState() {
      if (!this.el?.promptReviewStatus) return;
      this.updateInteractionState();
    },

    collectSettings() {
      return {
        mode: this.el.modeSwitch.dataset.mode,
        prompt: this.el.promptInput.value,
        channel_id: this.el.channelSelect.value,
        model: this.el.modelSelect.value,
        size: this.normalizeSize(this.el.sizeInput.value)
          || this.activeWorkspace?.settings?.size
          || "1024x1024",
        output_format: this.el.formatSelect.value,
        compression: 90,
        transparent_background: this.el.transparentBackground.checked,
        batch_count: Math.min(
          this.limits.max_batch_images, Math.max(1, Number(this.el.batchCount.value || 1)),
        ),
        chat_model_id: this.el.chatModelSelect.value,
        translate_prompt: this.el.translatePrompt.checked,
        creative_direction_id: this.el.creativeDirectionSelect.value || "auto",
        prompt_draft_id: this.activeWorkspace?.settings?.prompt_draft_id || "",
        generation_stage: this.activeWorkspace?.settings?.generation_stage || "draft",
        reference_ids: [...this.currentSelection()],
      };
    },

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
    },

    async flushSettings(workspaceId = this.activeWorkspace?.id, options = {}) {
      if (!workspaceId || this.activeWorkspace?.id !== workspaceId) return;
      if (this.saveTimer === null) {
        await this.workspaceSettingSaves.get(workspaceId);
        return;
      }
      window.clearTimeout(this.saveTimer);
      this.saveTimer = null;
      await this.saveSettings(options);
    },

    async saveSettings(options = {}) {
      const workspace = this.activeWorkspace;
      if (!workspace) return;
      if (!this.validateSizeInput(false)) {
        this.el.saveState.textContent = "尺寸无效";
        return;
      }
      const settings = this.collectSettings();
      workspace.settings = settings;
      const previous = this.workspaceSettingSaves.get(workspace.id) || Promise.resolve();
      const request = previous
        .catch(() => {})
        .then(() => UI.api(`/api/workspaces/${workspace.id}`, {
          method: "PATCH",
          body: { settings },
          ...options,
        }));
      this.workspaceSettingSaves.set(workspace.id, request);
      try {
        const data = await request;
        if (this.workspaceSettingSaves.get(workspace.id) !== request) return;
        workspace.settings = data.workspace.settings;
        if (workspace === this.activeWorkspace) this.el.saveState.textContent = "参数已保存";
      } catch (error) {
        if (error?.name === "AbortError") return;
        if (this.workspaceSettingSaves.get(workspace.id) !== request) return;
        if (workspace === this.activeWorkspace) this.el.saveState.textContent = "保存失败";
        UI.toast(error.message, "error");
      } finally {
        if (this.workspaceSettingSaves.get(workspace.id) === request) {
          this.workspaceSettingSaves.delete(workspace.id);
        }
      }
    },

  });
})();
