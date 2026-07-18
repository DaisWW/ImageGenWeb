(() => {
  "use strict";

  const {
    StudioApp,
    UI,
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
      if (!this.activeWorkspace || this.workspaceChatBusy() || this.workspaceHasActiveJob()) return;
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
      if (!message || !["failed", "canceled"].includes(message.delivery_state)
        || this.activeWorkspace?.id !== message.workspace_id
        || this.workspaceChatBusy() || this.workspaceHasActiveJob()) return;
      await this.submitOutgoingMessage(message);
    },

    async submitOutgoingMessage(message) {
      message.delivery_state = "sending";
      message.delivery_error = "";
      this.outgoingMessages.set(message.id, message);
      const result = await this.runChatOperation(
        message.workspace_id,
        "正在确认需求并整理最终提示词",
        message.id,
        (operation) => UI.api(`/api/workspaces/${message.workspace_id}/messages`, {
          method: "POST",
          body: {
            message_id: message.id,
            operation_id: operation.operation_id,
            model_id: message.model_id,
            content: message.content,
            attachment_ids: message.attachment_ids,
            generation_mode: message.generation_mode,
            generation_reference_ids: message.generation_reference_ids,
          },
          signal: operation.controller.signal,
        }),
      );
      const { operation, data, failure, canceled } = result;
      if (canceled) {
        const stillOwnMessage = message.operation_id === operation.operation_id
          || this.chatOperations.get(message.workspace_id) === operation;
        if (stillOwnMessage) {
          message.delivery_state = "canceled";
          message.delivery_error = "";
          message.operation_id = "";
          if (this.activeWorkspace?.id === message.workspace_id) this.renderMessages();
        }
        return;
      }
      if (!failure) {
        if (!data) return;
        this.outgoingMessages.delete(message.id);
        if (this.activeWorkspace?.id === message.workspace_id) {
          this.mergeConversationMessages(data.messages, data.context);
          this.renderMessages();
        }
        const workspace = this.workspaces.find((item) => item.id === message.workspace_id);
        if (workspace && data.workspace) {
          Object.assign(workspace, data.workspace);
          if (workspace === this.activeWorkspace) {
            this.el.workspaceTitle.textContent = workspace.name;
          }
          this.renderWorkspaceList();
        }
        return;
      }
      const outgoing = this.outgoingMessages.get(message.id);
      if (!outgoing) return;
      outgoing.delivery_state = "failed";
      outgoing.delivery_error = failure.message;
      if (this.activeWorkspace?.id === message.workspace_id) this.renderMessages();
    },

    async retryChatMessage(errorMessageId) {
      if (!this.activeWorkspace || this.workspaceChatBusy() || this.workspaceHasActiveJob()) return;
      const workspaceId = this.activeWorkspace.id;
      const modelId = this.el.chatModelSelect.value;
      if (!modelId) {
        UI.toast("管理员尚未配置可用的对话模型", "error");
        return;
      }
      const result = await this.runChatOperation(
        workspaceId,
        "正在重新确认需求",
        errorMessageId,
        (operation) => {
          if (this.activeWorkspace?.id !== workspaceId) return null;
          return UI.api(`/api/workspaces/${workspaceId}/messages/${errorMessageId}/retry`, {
            method: "POST",
            body: { model_id: modelId, operation_id: operation.operation_id },
            signal: operation.controller.signal,
          });
        },
      );
      const { data, canceled } = result;
      if (data && this.activeWorkspace?.id === workspaceId) {
        this.mergeConversationMessages([data.message], data.context);
        this.renderMessages();
      }
      if (canceled && this.activeWorkspace?.id === workspaceId) this.renderMessages();
    },

    openGenerationComposer(referenceIds = null) {
      if (!this.activeWorkspace || this.workspaceChatBusy() || this.workspaceHasActiveJob()
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
      this.activeWorkspace.settings.generation_stage = (
        stageByQuality[message.payload.quality_hint] || "draft"
      );
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

    isChatOperationCanceled(operation, error = null) {
      return operation.canceled
        || error?.name === "AbortError"
        || error?.code === "conversation_canceled";
    },

    async runChatOperation(workspaceId, label, messageId, request) {
      const operation = this.startLocalChatOperation(workspaceId, label, messageId);
      let data = null;
      let failure = null;
      let canceled = false;
      try {
        await this.flushSettings(workspaceId, { signal: operation.controller.signal });
        if (!operation.canceled) {
          data = await request(operation);
          if (operation.canceled) {
            canceled = true;
            this.requestOperationCancellation(workspaceId, operation.operation_id);
          }
        }
      } catch (error) {
        canceled = this.isChatOperationCanceled(operation, error);
        if (!canceled) failure = error;
      } finally {
        await this.finishLocalChatOperation(workspaceId, failure, operation);
      }
      return {
        operation,
        data,
        failure,
        canceled: canceled || this.isChatOperationCanceled(operation),
      };
    },

    mergeConversationMessages(messages, context) {
      this.messages = [...new Map(
        [...this.messages, ...messages].map((message) => [message.id, message]),
      ).values()];
      this.conversationContext = context;
    },

    startLocalChatOperation(workspaceId, label, messageId = "") {
      const operation = {
        busy: true,
        kind: "reply",
        label,
        started_at: new Date().toISOString(),
        operation_id: this.newMessageId(),
        message_id: messageId,
        controller: new AbortController(),
        local: true,
        canceled: false,
      };
      const message = messageId ? this.outgoingMessages.get(messageId) : null;
      if (message) message.operation_id = operation.operation_id;
      this.chatOperations.set(workspaceId, operation);
      this.renderWorkspaceList();
      if (this.activeWorkspace?.id === workspaceId) this.renderMessages();
      this.schedulePoll(ACTIVE_POLL_INTERVAL);
      return operation;
    },

    async finishLocalChatOperation(workspaceId, failure, operation) {
      if (this.chatOperations.get(workspaceId) === operation) {
        this.chatOperations.delete(workspaceId);
      }
      this.renderWorkspaceList();
      if (this.activeWorkspace?.id === workspaceId) this.renderMessages();
      if (!failure || operation?.canceled) return;
      UI.toast(failure.message, "error");
      await this.loadMessages(workspaceId);
    },

    requestOperationCancellation(workspaceId, operationId) {
      if (!workspaceId || !operationId) return;
      UI.api(`/api/workspaces/${workspaceId}/operations/${operationId}/cancel`, {
        method: "POST",
        keepalive: true,
      }).catch(() => {});
      this.schedulePoll(0);
    },

    cancelChatOperation(workspaceId = this.activeWorkspace?.id, operationId = "") {
      if (!workspaceId) return;
      const operation = this.chatOperations.get(workspaceId);
      const targetId = operationId || operation?.operation_id || operation?.message_id;
      if (!targetId) return;
      const operationIds = [operation?.operation_id, operation?.message_id].filter(Boolean);
      if (operation && operationIds.length && !operationIds.includes(targetId)) return;
      if (operation) {
        operation.canceled = true;
        operation.controller?.abort();
        if (this.chatOperations.get(workspaceId) === operation) {
          this.chatOperations.delete(workspaceId);
        }
        const message = operation.message_id
          ? this.outgoingMessages.get(operation.message_id)
          : null;
        if (message) {
          message.delivery_state = "canceled";
          message.delivery_error = "";
          message.operation_id = "";
        }
      }
      let canceled = this.canceledChatOperationIds.get(workspaceId);
      if (!canceled) {
        canceled = new Set();
        this.canceledChatOperationIds.set(workspaceId, canceled);
      }
      canceled.add(targetId);
      operationIds.forEach((id) => canceled.add(id));
      this.requestOperationCancellation(workspaceId, targetId);
      this.renderWorkspaceList();
      if (this.activeWorkspace?.id === workspaceId) this.renderMessages();
    },

    syncServerChatOperation(workspaceId, operation) {
      const previous = this.chatOperations.get(workspaceId);
      if (previous?.local) return false;
      const canceled = this.canceledChatOperationIds.get(workspaceId);
      if (operation?.busy && canceled?.size) {
        if (!operation.operation_id || canceled.has(operation.operation_id)) return false;
      }
      if (!operation?.busy && canceled?.size) {
        this.canceledChatOperationIds.delete(workspaceId);
      }
      const next = operation?.busy ? { ...operation, local: false } : null;
      const unchanged = Boolean(previous) === Boolean(next)
        && (!next || (
          previous.kind === next.kind
          && previous.label === next.label
          && previous.started_at === next.started_at
          && previous.operation_id === next.operation_id
          && previous.message_id === next.message_id
        ));
      if (unchanged) return false;
      if (operation?.busy) {
        this.chatOperations.set(workspaceId, next);
      } else {
        this.chatOperations.delete(workspaceId);
      }
      return true;
    },

    workspaceHasActiveJob(workspaceId = this.activeWorkspace?.id) {
      if (!workspaceId) return false;
      return this.workspaceJobs.has(workspaceId)
        || (
          workspaceId === this.activeWorkspace?.id
          && this.jobs.some((job) => !TERMINAL.has(job.status))
        );
    },

    setActionIcon(button, iconName, state) {
      if (!button || button.dataset.actionState === state) return;
      button.dataset.actionState = state;
      button.querySelector("svg[data-lucide], i[data-lucide]")?.remove();
      const icon = document.createElement("i");
      icon.dataset.lucide = iconName;
      button.prepend(icon);
      UI.icons(button);
    },

    updateInteractionState() {
      const noWorkspace = !this.activeWorkspace;
      const generationBusy = this.workspaceHasActiveJob();
      const operation = this.chatOperations.get(this.activeWorkspace?.id);
      const chatBusy = Boolean(operation);
      const generationSubmission = this.generationSubmissions.get(this.activeWorkspace?.id);
      const submissionBusy = Boolean(generationSubmission);
      const locked = noWorkspace || generationBusy || chatBusy || submissionBusy;
      const referenceUploading = this.referenceUploadPending;
      const hasModel = Boolean(this.el.chatModelSelect.value);
      setDisabled(this.el.chatInput, locked);
      const chatCanCancel = chatBusy && !generationBusy && !submissionBusy;
      setDisabled(
        this.el.chatSendButton,
        noWorkspace || generationBusy || submissionBusy
          || referenceUploading || (!chatCanCancel && !hasModel),
      );
      this.el.chatSendButton.type = chatCanCancel ? "button" : "submit";
      this.setActionIcon(
        this.el.chatSendButton,
        chatCanCancel ? "square" : "arrow-up",
        chatCanCancel ? "cancel" : "send",
      );
      const sendTitle = chatCanCancel
        ? "取消等待"
        : referenceUploading ? "等待图片上传完成" : "发送消息";
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
        submissionBusy
          ? false
          : locked || referenceUploading || !this.currentChannel() || missingAnimationMaster,
      );
      this.setActionIcon(
        this.el.generateButton,
        submissionBusy ? "square" : "sparkles",
        submissionBusy ? "cancel" : "generate",
      );
      setText(this.el.generateButtonLabel, submissionBusy ? "取消生成" : (
        this.isAnimationWorkspace() ? "开始生成帧" : "开始生成"
      ));
      const generateTitle = submissionBusy
        ? "取消生成"
        : missingAnimationMaster ? "请先添加并选择一张母图" : "";
      setAttribute(this.el.generateButton, "title", generateTitle);
      setDisabled(this.el.generationBackButton, submissionBusy);
      setDisabled(
        this.el.referenceAdd,
        referenceUploading
          || (this.activeWorkspace?.assets.length || 0) >= this.limits.max_assets_per_workspace,
      );
      setDisabled(this.el.referenceLibrary, referenceUploading);
      setDisabled(this.el.clearWorkspaceButton, locked || referenceUploading);
      setDisabled(this.el.libraryButton, noWorkspace);
      this.el.workspaceList.querySelectorAll("[data-delete-workspace]").forEach((button) => {
        const workspaceId = button.dataset.deleteWorkspace;
        const activeLocked = workspaceId === this.activeWorkspace?.id
          && (locked || referenceUploading);
        setDisabled(button, this.chatOperations.has(workspaceId) || activeLocked);
      });
      this.el.messageList.querySelectorAll("[data-retry-message], [data-retry-send]").forEach((button) => {
        setDisabled(button, locked || !hasModel);
      });
      this.el.messageList.querySelectorAll("[data-cancel-chat]").forEach((button) => {
        const buttonWorkspace = button.dataset.cancelWorkspace || this.activeWorkspace?.id;
        const buttonOperation = button.dataset.cancelOperation;
        const activeOperation = this.chatOperations.get(buttonWorkspace);
        const activeOperationIds = [
          activeOperation?.operation_id,
          activeOperation?.message_id,
        ].filter(Boolean);
        setDisabled(
          button,
          !activeOperation && !buttonOperation
            || Boolean(activeOperation && buttonOperation
              && !activeOperationIds.includes(buttonOperation)),
        );
      });
      this.el.messageList.querySelectorAll("[data-use-prompt-draft]").forEach((button) => {
        setDisabled(button, locked);
      });
      this.el.messageList.querySelectorAll("[data-retry-job]").forEach((button) => {
        setDisabled(button, locked);
      });
      const placeholder = noWorkspace ? "暂无工作站"
        : generationBusy
        ? "当前生成完成前不能继续对话，可在生成记录中取消任务"
        : submissionBusy ? "正在提交生成，可立即取消"
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
      if (this.workspaceChatBusy() || this.workspaceHasActiveJob()) return;
      this.chatReferencePickerOpen = !this.chatReferencePickerOpen;
      this.renderChatReferences();
    },

    openReferencePicker(target) {
      if (this.referenceUploadPending) return;
      this.uploadTarget = target;
      this.el.referenceInput.click();
    },

    chatCanAcceptImages() {
      return Boolean(this.activeWorkspace)
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
