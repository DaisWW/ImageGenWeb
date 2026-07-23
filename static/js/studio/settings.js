(() => {
  "use strict";

  const {
    StudioApp,
    UI,
    COMPOSER_CLOSE_TIMEOUT,
    IMAGE_SIZE_PATTERN,
    IMAGE_DIMENSION_MIN,
    IMAGE_DIMENSION_MAX,
    GenerationStrategyPolicy,
    setHidden,
    setAttribute,
    setText,
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
      this.canvasConflict = null;
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
      this.el.batchCount.value = this.generationStrategyPolicy()
        .normalizeCount("sample", settings.batch_count);
      const preferred = this.channels.find((channel) => channel.id === settings.channel_id && channel.configured)
        || this.channels.find((channel) => channel.configured)
        || this.channels[0];
      this.renderChannelOptions(preferred?.id);
      this.applyChannel(settings, false);
      this.setMode(settings.mode || "text2img", false);
      this.setGenerationStrategy(settings.generation_strategy || "sample", false);
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
      if (
        this.el.generationStrategy?.value === "series"
        && !channel.capabilities.modes.includes("img2img")
      ) {
        this.setGenerationStrategy("sample", false);
        if (shouldSave) UI.toast("当前渠道不支持系列延续，已切回同提示词抽样", "info");
      }
      const selection = this.currentSelection();
      this.ensureSeriesAnchorSelection(selection);
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

    normalizeCanvasRequest(value) {
      if (!value || typeof value !== "object") return null;
      const dimension = (raw) => {
        if (typeof raw === "boolean") return null;
        const number = Number(raw);
        return Number.isInteger(number)
          && number >= IMAGE_DIMENSION_MIN
          && number <= IMAGE_DIMENSION_MAX
          ? number : null;
      };
      const width = dimension(value.width);
      const height = dimension(value.height);
      const ratioMatch = String(value.aspect_ratio || "")
        .trim()
        .replaceAll("：", ":")
        .match(/^([1-9]\d{0,3}):([1-9]\d{0,3})$/);
      const ratio = ratioMatch
        ? this.canvasRatio(Number(ratioMatch[1]), Number(ratioMatch[2]))
        : "";
      if (width && height) {
        const derived = this.canvasRatio(width, height);
        if (ratio && ratio !== derived) return null;
        return { width, height, aspect_ratio: derived };
      }
      return ratio ? { aspect_ratio: ratio } : null;
    },

    canvasRatio(width, height) {
      let left = width;
      let right = height;
      while (right) [left, right] = [right, left % right];
      return `${width / left}:${height / left}`;
    },

    canvasRequestLabel(request) {
      if (!request) return "";
      return request.width && request.height
        ? `${request.width}×${request.height} · ${request.aspect_ratio}`
        : request.aspect_ratio;
    },

    canvasRequestTargetSize(request) {
      if (!request) return "";
      if (request.width && request.height) return `${request.width}x${request.height}`;
      const sizes = this.currentChannel()?.capabilities?.sizes || [];
      return sizes.map((value) => this.normalizeSize(value)).find((size) => {
        const match = IMAGE_SIZE_PATTERN.exec(size);
        return match && this.canvasRatio(Number(match[1]), Number(match[2]))
          === request.aspect_ratio;
      }) || "";
    },

    canvasRequestConflicts(request, size) {
      if (!request) return false;
      const match = IMAGE_SIZE_PATTERN.exec(this.normalizeSize(size));
      if (!match) return false;
      const width = Number(match[1]);
      const height = Number(match[2]);
      return request.width && request.height
        ? request.width !== width || request.height !== height
        : request.aspect_ratio !== this.canvasRatio(width, height);
    },

    renderCanvasConflict() {
      if (!this.el?.canvasConflict) return;
      const draft = this.currentPromptDraft();
      const request = this.normalizeCanvasRequest(draft?.payload?.canvas_request);
      const currentSize = this.normalizeSize(this.el.sizeInput.value);
      const sameDraft = this.canvasConflict?.draftId === draft?.id;
      let resolution = sameDraft ? this.canvasConflict?.resolution || "" : "";
      const conflict = Boolean(draft && request && this.canvasRequestConflicts(request, currentSize));
      if (conflict && resolution === "conversation") resolution = "";
      if (!conflict) {
        if (draft && request && sameDraft && resolution === "conversation") {
          this.canvasConflict = { draftId: draft.id, request, resolution };
          const currentLabel = currentSize.replace("x", "×");
          setHidden(this.el.canvasConflict, false);
          this.el.canvasConflict.classList.add("resolved");
          setText(
            this.el.canvasConflictMessage,
            `已应用对话画幅，本次按 ${currentLabel} 提交。`,
          );
          return;
        }
        this.canvasConflict = null;
        setHidden(this.el.canvasConflict, true);
        return;
      }
      this.canvasConflict = { draftId: draft.id, request, resolution };
      const requestLabel = this.canvasRequestLabel(request);
      const currentLabel = currentSize.replace("x", "×");
      const targetSize = this.canvasRequestTargetSize(request);
      const resolvedText = resolution === "panel"
        ? `已保持当前尺寸 ${currentLabel}，对话建议为 ${requestLabel}。`
        : `对话要求 ${requestLabel}，当前为 ${currentLabel}。请选择后再生成。`;
      setHidden(this.el.canvasConflict, false);
      this.el.canvasConflict.classList.toggle("resolved", Boolean(resolution));
      setText(this.el.canvasConflictMessage, resolvedText);
      setText(
        this.el.canvasConflictApply,
        targetSize ? `应用 ${targetSize.replace("x", "×")}` : "无可用对话尺寸",
      );
    },

    applyCanvasRequest() {
      const targetSize = this.canvasRequestTargetSize(this.canvasConflict?.request);
      if (!targetSize) {
        UI.toast("当前渠道没有可直接应用的对话画幅，请手动填写尺寸", "info");
        return;
      }
      this.el.sizeInput.value = targetSize;
      if (!this.validateSizeInput(true)) return;
      this.canvasConflict = {
        ...this.canvasConflict,
        resolution: "conversation",
      };
      this.settingChanged();
      this.updateInteractionState();
    },

    keepPanelCanvas() {
      if (!this.canvasConflict) return;
      this.canvasConflict = {
        ...this.canvasConflict,
        resolution: "panel",
      };
      this.updateInteractionState();
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
        ? "生成后由 Lucida 自动抠成透明背景 PNG/WebP（不请求上游原生透明）"
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
      if (mode === "text2img" && this.el.generationStrategy?.value === "series") {
        this.setGenerationStrategy("sample", false);
      }
      this.el.referenceStrip.hidden = mode !== "img2img";
      this.updatePrice();
      this.updatePromptReviewState();
      if (shouldSave) this.settingChanged();
    },

    generationStrategyPolicy() {
      return new GenerationStrategyPolicy(this.limits.max_batch_images);
    },

    generationCount() {
      const strategy = this.el.generationStrategy.value || "sample";
      return this.generationStrategyPolicy().normalizeCount(strategy, this.el.batchCount.value);
    },

    ensureSeriesAnchorSelection(selection = this.currentSelection()) {
      if (this.el.generationStrategy?.value !== "series") return false;
      const workspace = this.activeWorkspace;
      const anchorId = workspace?.settings?.series_anchor?.asset_id;
      if (!anchorId || !workspace?.assets?.some((asset) => asset.id === anchorId)) return false;
      const ordered = [anchorId, ...[...selection].filter((id) => id !== anchorId)];
      const changed = ordered.length !== selection.size
        || ordered.some((id, index) => [...selection][index] !== id);
      if (changed) {
        selection.clear();
        ordered.forEach((id) => selection.add(id));
      }
      return true;
    },

    normalizeGenerationCount(force = false) {
      const strategy = this.el.generationStrategy.value || "sample";
      const policy = this.generationStrategyPolicy();
      const { minimum, maximum } = policy.countRange(strategy);
      this.el.batchCount.min = String(minimum);
      this.el.batchCount.max = String(maximum);
      if (force || this.el.batchCount.value.trim()) {
        this.el.batchCount.value = String(
          policy.normalizeCount(strategy, this.el.batchCount.value),
        );
      }
    },

    setGenerationStrategy(value, shouldSave) {
      const workspace = this.activeWorkspace;
      const anchor = workspace?.settings?.series_anchor;
      const anchorAvailable = Boolean(
        anchor?.asset_id && workspace.assets.some((asset) => asset.id === anchor.asset_id),
      );
      const img2imgAvailable = Boolean(
        this.currentChannel()?.capabilities?.modes?.includes("img2img"),
      );
      const policy = this.generationStrategyPolicy();
      const explorationAvailable = policy.explorationAvailable;
      const seriesAvailable = policy.seriesAvailable({ anchorAvailable, img2imgAvailable });
      const exploreOption = [...this.el.generationStrategy.options]
        .find((option) => option.value === "explore");
      if (exploreOption) exploreOption.disabled = !explorationAvailable;
      const seriesOption = [...this.el.generationStrategy.options]
        .find((option) => option.value === "series");
      if (seriesOption) seriesOption.disabled = !seriesAvailable;
      const requestedStrategy = policy.normalizeStrategy(value);
      const strategy = policy.resolveStrategy(requestedStrategy, {
        anchorAvailable,
        img2imgAvailable,
      });
      if (requestedStrategy === "explore" && strategy !== requestedStrategy && shouldSave) {
        UI.toast("当前批量上限不足以进行受控探索，已切回同提示词抽样", "info");
      }
      if (requestedStrategy === "series" && strategy !== requestedStrategy) {
        if (shouldSave) {
          UI.toast(
            !anchorAvailable ? "请先将一张生成结果设为系列基准" : "当前渠道不支持系列延续",
            "info",
          );
        }
      }
      this.el.generationStrategy.value = strategy;
      if (strategy === "explore" && Number(this.el.batchCount.value || 1) < 2) {
        this.el.batchCount.value = String(policy.countRange(strategy).maximum);
      }
      this.normalizeGenerationCount(true);
      if (strategy === "series") {
        const selected = this.currentSelection();
        this.ensureSeriesAnchorSelection(selected);
        const ordered = [...selected];
        const limit = this.generationReferenceLimit();
        selected.clear();
        ordered.slice(0, limit).forEach((id) => selected.add(id));
        this.setMode("img2img", false);
        this.renderReferences();
      }
      this.renderGenerationPlan();
      this.updatePrice();
      this.updatePromptReviewState();
      if (shouldSave) this.settingChanged();
    },

    renderGenerationPlan() {
      const strategy = this.el.generationStrategy.value || "sample";
      if (strategy === "sample") {
        setHidden(this.el.generationPlan, true);
        this.el.generationPlanList.replaceChildren();
        return;
      }
      setHidden(this.el.generationPlan, false);
      const rows = [];
      if (strategy === "explore") {
        const draft = this.currentPromptDraft();
        const variants = Array.isArray(draft?.payload?.exploration_plan)
          ? draft.payload.exploration_plan.slice(0, this.generationCount())
          : [];
        setText(this.el.generationPlanTitle, "受控探索");
        setText(
          this.el.generationPlanHint,
          variants.length ? "只变化声明维度，其余制作契约保持一致" : "请先使用 AI 整理最终提示词",
        );
        variants.forEach((variant, index) => {
          rows.push({
            label: `${String.fromCharCode(65 + index)} · ${variant.label || "探索方案"}`,
            detail: Array.isArray(variant.delta) ? variant.delta.join("；") : "",
          });
        });
      } else {
        const anchor = this.activeWorkspace?.settings?.series_anchor || {};
        const contract = anchor.contract || {};
        const labels = {
          identity_anchors: "身份锚点",
          visual_language: "视觉语言",
          palette_materials: "色板材质",
          composition_rules: "构图规则",
          typography_rules: "排版规则",
          must_preserve: "必须保持",
          allowed_changes: "允许变化",
        };
        setText(this.el.generationPlanTitle, "系列延续");
        setText(this.el.generationPlanHint, "基准图作为第一参考，系列契约逐项重复");
        Object.entries(contract).slice(0, 7).forEach(([key, values]) => {
          rows.push({
            label: labels[key] || key,
            detail: Array.isArray(values) ? values.join("；") : String(values || ""),
          });
        });
      }
      this.el.generationPlanList.replaceChildren(...rows.map((row) => {
        const item = document.createElement("div");
        item.className = "generation-plan-item";
        const label = document.createElement("strong");
        label.textContent = row.label;
        const detail = document.createElement("span");
        detail.textContent = row.detail;
        item.append(label, detail);
        return item;
      }));
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
      this.renderGenerationPlan?.();
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
        batch_count: this.generationCount(),
        generation_strategy: this.el.generationStrategy.value || "sample",
        series_anchor: this.activeWorkspace?.settings?.series_anchor || {},
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
