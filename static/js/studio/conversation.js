(() => {
  "use strict";

  const {
    StudioApp,
    UI,
    STATUS,
    TERMINAL,
    ACTIVE_POLL_INTERVAL,
    setText,
    setDisabled,
    setAttribute,
  } = window.ImageGenStudio;

  Object.assign(StudioApp.prototype, {
    clearOutgoingMessages(workspaceId) {
      for (const [id, message] of this.outgoingMessages) {
        if (message.workspace_id === workspaceId) this.outgoingMessages.delete(id);
      }
    },

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
      const content = this.el.chatInput.value.trim();
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
      const selectedIds = new Set(attachmentIds);
      const selectedGenerationMode = workspace.kind === "animation"
        ? "img2img"
        : (this.el.modeSwitch.dataset.mode || "text2img");
      const selectedGenerationReferences = [...this.currentSelection(workspaceId)];
      const generationMode = selectedGenerationMode === "img2img"
        && (selectedGenerationReferences.length || !attachmentIds.length)
        ? "img2img"
        : attachmentIds.length ? "auto" : "text2img";
      const generationReferenceIds = generationMode === "img2img"
        ? selectedGenerationReferences
        : [];
      const message = {
        id: this.newMessageId(),
        workspace_id: workspaceId,
        model_id: modelId,
        role: "user",
        kind: "message",
        content,
        attachments: workspace.assets.filter((asset) => selectedIds.has(asset.id)),
        attachment_ids: attachmentIds,
        generation_mode: generationMode,
        generation_reference_ids: generationReferenceIds,
        created_at: new Date().toISOString(),
      };
      workspace.settings.prompt_draft_id = "";
      this.el.chatInput.value = "";
      this.chatDrafts.set(workspaceId, "");
      selection.clear();
      this.chatReferencePickerOpen = false;
      this.renderChatReferences();
      await this.submitOutgoingMessage(message);
    },

    async retryFailedChatMessage(messageId) {
      const message = this.outgoingMessages.get(messageId);
      if (!message || message.delivery_state !== "failed"
        || this.activeWorkspace?.id !== message.workspace_id
        || this.workspaceChatBusy() || this.workspaceHasActiveJob()) return;
      await this.submitOutgoingMessage(message);
    },

    async submitOutgoingMessage(message) {
      message.delivery_state = "sending";
      message.delivery_error = "";
      this.outgoingMessages.set(message.id, message);
      this.startLocalChatOperation(message.workspace_id, "reply", "正在确认需求并整理最终提示词");
      let failure = null;
      try {
        await this.flushSettings(message.workspace_id);
        const data = await UI.api(`/api/workspaces/${message.workspace_id}/messages`, {
          method: "POST",
          body: {
            message_id: message.id,
            model_id: message.model_id,
            content: message.content,
            attachment_ids: message.attachment_ids,
            generation_mode: message.generation_mode,
            generation_reference_ids: message.generation_reference_ids,
          },
        });
        this.outgoingMessages.delete(message.id);
        if (this.activeWorkspace?.id === message.workspace_id) {
          this.messages = [...new Map(
            [...this.messages, ...data.messages].map((item) => [item.id, item]),
          ).values()];
          this.conversationContext = data.context;
        }
        const workspace = this.workspaces.find((item) => item.id === message.workspace_id);
        if (workspace && data.workspace) {
          Object.assign(workspace, data.workspace);
          if (workspace === this.activeWorkspace) this.el.workspaceTitle.textContent = workspace.name;
          this.renderWorkspaceList();
        }
      } catch (error) {
        failure = error;
      } finally {
        await this.finishLocalChatOperation(message.workspace_id);
      }
      if (!failure) return;
      await this.loadMessages(message.workspace_id);
      const outgoing = this.outgoingMessages.get(message.id);
      if (!outgoing) return;
      outgoing.delivery_state = "failed";
      outgoing.delivery_error = failure.message;
      if (this.activeWorkspace?.id === message.workspace_id) this.renderMessages();
    },

    async retryChatMessage(errorMessageId) {
      if (this.workspaceLoading || !this.activeWorkspace
        || this.workspaceChatBusy() || this.workspaceHasActiveJob()) return;
      const workspaceId = this.activeWorkspace.id;
      const modelId = this.el.chatModelSelect.value;
      if (!modelId) {
        UI.toast("管理员尚未配置可用的对话模型", "error");
        return;
      }
      this.startLocalChatOperation(workspaceId, "reply", "正在重新确认需求");
      let failure = null;
      try {
        await this.flushSettings();
        if (this.activeWorkspace?.id !== workspaceId) return;
        const data = await UI.api(
          `/api/workspaces/${workspaceId}/messages/${errorMessageId}/retry`,
          { method: "POST", body: { model_id: modelId } },
        );
        if (this.activeWorkspace?.id === workspaceId) {
          this.messages = [...new Map(
            [...this.messages, data.message].map((message) => [message.id, message]),
          ).values()];
          this.conversationContext = data.context;
        }
      } catch (error) {
        failure = error;
      } finally {
        await this.finishLocalChatOperation(workspaceId, failure);
      }
    },

    openGenerationComposer(referenceIds = null) {
      if (this.workspaceLoading || !this.activeWorkspace
        || this.workspaceChatBusy() || this.workspaceHasActiveJob()
        || this.referenceUploadPending) return;
      const hadReviewedDraft = Boolean(this.activeWorkspace.settings.prompt_draft_id);
      this.activeWorkspace.settings.prompt_draft_id = "";
      const draft = this.el.chatInput.value.trim();
      const prompt = draft.slice(0, this.limits.max_prompt_characters);
      const requested = referenceIds === null
        ? [...this.currentChatSelection()]
        : [...new Set(referenceIds)];
      if (this.isAnimationWorkspace() && !requested.length
        && !this.currentSelection().size && this.activeWorkspace.assets.length === 1) {
        requested.push(this.activeWorkspace.assets[0].id);
      }
      if (prompt.length < draft.length) UI.toast("描述过长，已按提示词长度上限截取", "info");
      this.showGenerationComposer(prompt, requested.length ? requested : null);
      if (hadReviewedDraft) this.settingChanged();
    },

    showGenerationComposer(prompt, referenceIds = null) {
      if (prompt) {
        this.el.promptInput.value = prompt;
        this.updatePromptCounter();
      }
      let omitted = 0;
      if (referenceIds !== null) {
        const requested = [...new Set(referenceIds)];
        const activeIds = new Set(this.activeWorkspace.assets.map((asset) => asset.id));
        const references = this.currentSelection();
        references.clear();
        requested.filter((id) => activeIds.has(id)).forEach((id) => references.add(id));
        this.trimReferenceSelection(references, this.generationReferenceLimit());
        this.setMode(this.isAnimationWorkspace() || references.size ? "img2img" : "text2img", false);
        this.renderReferences();
        omitted = requested.length - references.size;
      } else if (this.isAnimationWorkspace()) {
        this.setMode("img2img", false);
      }
      this.setComposerMode("generation");
      if (prompt || referenceIds !== null) this.settingChanged();
      const preserveComposerTop = window.innerWidth >= 640;
      this.el.promptInput.focus({ preventScroll: preserveComposerTop });
      if (preserveComposerTop) this.el.generationForm.scrollTop = 0;
      return omitted;
    },

    applyPromptDraft(messageId) {
      const message = this.messages.find((item) => item.id === messageId);
      const prompt = message?.payload?.prompt;
      if (!prompt) return;
      this.activeWorkspace.settings.prompt_draft_id = message.id;
      this.activeWorkspace.settings.creative_direction_id = (
        this.el.creativeDirectionSelect.value || "auto"
      );
      const stageByQuality = { low: "draft", medium: "refine", high: "final" };
      this.setGenerationStage(stageByQuality[message.payload.quality_hint] || "draft", false);
      const omitted = this.showGenerationComposer(prompt, message.payload.reference_ids || []);
      this.updatePromptReviewState();
      if (omitted > 0) {
        const max = this.generationReferenceLimit();
        UI.toast(`当前渠道最多使用 ${max} 张垫图，已忽略 ${omitted} 张超限或已删除的参考图`);
      }
    },

    workspaceChatBusy(workspaceId = this.activeWorkspace?.id) {
      return Boolean(workspaceId && this.chatOperations.has(workspaceId));
    },

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
      this.schedulePoll(ACTIVE_POLL_INTERVAL);
    },

    async finishLocalChatOperation(workspaceId, failure = null) {
      if (this.chatOperations.get(workspaceId)?.local) {
        this.chatOperations.delete(workspaceId);
      }
      this.renderWorkspaceList();
      if (this.activeWorkspace?.id === workspaceId) this.renderMessages();
      if (!failure) return;
      UI.toast(failure.message, "error");
      await this.loadMessages(workspaceId);
    },

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
    },

    workspaceHasActiveJob() {
      return this.jobs.some((job) => !TERMINAL.has(job.status));
    },

    updateInteractionState() {
      const noWorkspace = !this.activeWorkspace;
      const generationBusy = this.workspaceHasActiveJob();
      const operation = this.chatOperations.get(this.activeWorkspace?.id);
      const chatBusy = Boolean(operation);
      const locked = noWorkspace || this.workspaceLoading || generationBusy || chatBusy;
      const referenceUploading = this.referenceUploadPending;
      const hasModel = Boolean(this.el.chatModelSelect.value);
      setDisabled(this.el.chatInput, locked);
      setDisabled(this.el.chatSendButton, locked || referenceUploading || !hasModel);
      const sendTitle = referenceUploading ? "等待图片上传完成" : "发送消息";
      setAttribute(this.el.chatSendButton, "title", sendTitle);
      setAttribute(this.el.chatSendButton, "aria-label", sendTitle);
      setDisabled(this.el.chatModelSelect, locked || !hasModel);
      setDisabled(this.el.creativeDirectionSelect, locked);
      setDisabled(this.el.translatePrompt, locked);
      setDisabled(this.el.chatReferenceButton, locked || referenceUploading);
      setDisabled(this.el.directGenerationButton, locked || referenceUploading);
      setDisabled(this.el.animationParametersButton, locked || referenceUploading);
      const missingAnimationMaster = this.isAnimationWorkspace() && this.currentSelection().size !== 1;
      const promptReviewed = Boolean(this.currentPromptDraft());
      this.el.promptReviewStatus.classList.toggle("is-reviewed", promptReviewed);
      setText(
        this.el.promptReviewStatus.querySelector("span"),
        promptReviewed ? "最终提示词已就绪" : "可直接编辑提示词",
      );
      setDisabled(
        this.el.generateButton,
        locked || referenceUploading || !this.currentChannel()
          || missingAnimationMaster,
      );
      const generateTitle = missingAnimationMaster ? "请先添加并选择一张母图" : "";
      setAttribute(this.el.generateButton, "title", generateTitle);
      this.el.qualityStageSwitch.querySelectorAll("button").forEach((button) => {
        setDisabled(button, locked);
      });
      setDisabled(this.el.generationBackButton, this.workspaceLoading);
      setDisabled(
        this.el.referenceAdd,
        this.workspaceLoading || referenceUploading
          || (this.activeWorkspace?.assets.length || 0) >= this.limits.max_assets_per_workspace,
      );
      setDisabled(this.el.referenceLibrary, this.workspaceLoading || referenceUploading);
      setDisabled(this.el.clearWorkspaceButton, locked || referenceUploading);
      setDisabled(this.el.libraryButton, noWorkspace);
      this.el.workspaceList.querySelectorAll("[data-delete-workspace]").forEach((button) => {
        const workspaceId = button.dataset.deleteWorkspace;
        const activeLocked = workspaceId === this.activeWorkspace?.id
          && (locked || referenceUploading);
        setDisabled(button, this.chatOperations.has(workspaceId) || activeLocked);
      });
      this.el.messageList.querySelectorAll("[data-retry-message]").forEach((button) => {
        setDisabled(button, locked || !hasModel);
      });
      this.el.messageList.querySelectorAll("[data-retry-send]").forEach((button) => {
        setDisabled(button, locked || !hasModel);
      });
      this.el.messageList.querySelectorAll("[data-use-prompt-draft]").forEach((button) => {
        setDisabled(button, locked);
      });
      this.el.messageList.querySelectorAll("[data-retry-job]").forEach((button) => {
        setDisabled(button, locked);
      });
      const placeholder = this.workspaceLoading
        ? "正在加载工作站..."
        : noWorkspace ? "暂无工作站"
        : generationBusy
        ? "当前生成完成前不能继续对话，可在生成记录中取消任务"
        : chatBusy ? `${operation.label}，可切换到其他工作站继续`
        : this.isAnimationWorkspace() ? "描述画面、动作和循环方式..." : "描述你想生成的画面...";
      if (this.el.chatInput.placeholder !== placeholder) this.el.chatInput.placeholder = placeholder;
    },

    renderContextStatus() {
      const used = Number(this.conversationContext?.estimated_context_tokens || 0);
      const maximum = Number(this.conversationContext?.max_context_tokens || 0);
      const percent = maximum > 0 ? Math.min(100, Math.round(used / maximum * 100)) : 0;
      setText(this.el.contextStatus.querySelector("span"), `上下文 ${percent}%`);
      const title = maximum
        ? `约 ${used.toLocaleString()} / ${maximum.toLocaleString()} tokens`
        : "当前会话上下文";
      setAttribute(this.el.contextStatus, "title", title);
    },

    scrollConversation(includePage = false) {
      if (this.scrollFrame !== null) window.cancelAnimationFrame(this.scrollFrame);
      this.scrollFrame = window.requestAnimationFrame(() => {
        if (includePage) this.el.chatForm.scrollIntoView({ block: "end" });
        this.el.conversationScroll.scrollTop = this.el.conversationScroll.scrollHeight;
        this.scrollFrame = window.requestAnimationFrame(() => {
          this.el.conversationScroll.scrollTop = this.el.conversationScroll.scrollHeight;
          this.scrollFrame = null;
        });
      });
    },

    toggleChatReferences() {
      if (this.workspaceLoading || this.workspaceChatBusy() || this.workspaceHasActiveJob()) return;
      this.chatReferencePickerOpen = !this.chatReferencePickerOpen;
      this.renderChatReferences();
    },

    openReferencePicker(target) {
      if (this.workspaceLoading || this.referenceUploadPending) return;
      this.uploadTarget = target;
      this.el.referenceInput.click();
    },

    chatCanAcceptImages() {
      return Boolean(this.activeWorkspace)
        && !this.workspaceLoading
        && !this.workspaceChatBusy()
        && !this.workspaceHasActiveJob()
        && !this.referenceUploadPending;
    },

    handleChatDrag(event) {
      if (![...(event.dataTransfer?.types || [])].includes("Files")) return;
      event.preventDefault();
      if (!this.chatCanAcceptImages()) {
        event.dataTransfer.dropEffect = "none";
        return;
      }
      event.dataTransfer.dropEffect = "copy";
      this.el.chatForm.classList.add("is-image-dragover");
    },

    handleChatDragLeave(event) {
      if (this.el.chatForm.contains(event.relatedTarget)) return;
      this.el.chatForm.classList.remove("is-image-dragover");
    },

    handleChatDrop(event) {
      if (![...(event.dataTransfer?.types || [])].includes("Files")) return;
      event.preventDefault();
      this.el.chatForm.classList.remove("is-image-dragover");
      if (!this.chatCanAcceptImages()) return;
      this.uploadReferences([...event.dataTransfer.files], "chat");
    },

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
    },

  });
})();
