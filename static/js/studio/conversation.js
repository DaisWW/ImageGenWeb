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

  const CHAT_PREVIEW_HANDOFF_MS = 180;

  Object.assign(StudioApp.prototype, {
    clearOutgoingMessages(workspaceId) {
      for (const [id, message] of this.outgoingMessages) {
        if (message.workspace_id === workspaceId) this.outgoingMessages.delete(id);
      }
    },

    async copyMessage(messageId) {
      const message = this.messages.find((item) => item.id === messageId)
        || this.outgoingMessages.get(messageId);
      const content = String(message?.content || "");
      if (!content.trim()) {
        UI.toast("暂无可复制的消息", "info");
        return;
      }
      try {
        await navigator.clipboard.writeText(content);
        UI.toast("消息已复制", "success");
      } catch (_error) {
        UI.toast("复制失败，请手动复制", "error");
      }
    },

    latestOpenClarification(workspaceId = this.activeWorkspace?.id) {
      if (!workspaceId) return null;
      const ordered = [...this.messages]
        .filter((message) => (
          !message.workspace_id || message.workspace_id === workspaceId
        ))
        .sort((left, right) => String(left.created_at || "").localeCompare(String(right.created_at || ""))
          || String(left.id || "").localeCompare(String(right.id || "")));
      for (let index = ordered.length - 1; index >= 0; index -= 1) {
        const message = ordered[index];
        if (message.role !== "assistant") continue;
        if (message.payload?.status === "ready" && message.kind === "prompt_draft") return null;
        if (message.payload?.status === "needs_clarification") return message;
      }
      return null;
    },

    continueClarification(messageId) {
      const workspaceId = this.activeWorkspace?.id;
      const clarification = this.latestOpenClarification(workspaceId);
      if (!clarification || clarification.id !== messageId) {
        UI.toast("该澄清问题已结束，请选择最新问题", "info");
        return;
      }
      this.clarificationReplies.set(workspaceId, messageId);
      this.renderClarificationContinuation();
      this.el.chatInput.focus();
    },

    cancelClarificationContinuation() {
      const workspaceId = this.activeWorkspace?.id;
      if (!workspaceId) return;
      this.clarificationReplies.delete(workspaceId);
      this.renderClarificationContinuation();
      this.el.chatInput.focus();
    },

    renderClarificationContinuation() {
      const workspaceId = this.activeWorkspace?.id;
      const selectedId = workspaceId ? this.clarificationReplies.get(workspaceId) : "";
      const open = this.latestOpenClarification(workspaceId);
      const active = Boolean(selectedId && open?.id === selectedId);
      if (selectedId && !active) this.clarificationReplies.delete(workspaceId);
      this.el.chatClarification.hidden = !active;
    },

    async sendChatMessage(event) {
      event.preventDefault();
      if (!this.activeWorkspace) return;
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
      const selectedGenerationMode = this.el.modeSwitch.dataset.mode || "text2img";
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
        clarification_reply_to_id: this.clarificationReplies.get(workspaceId) || "",
        created_at: new Date().toISOString(),
      };
      workspace.settings.prompt_draft_id = "";
      this.el.chatInput.value = "";
      this.chatDrafts.set(workspaceId, "");
      this.clarificationReplies.delete(workspaceId);
      this.renderClarificationContinuation();
      selection.clear();
      this.chatReferencePickerOpen = false;
      this.renderChatReferences();
      await this.submitOutgoingMessage(message);
    },

    async retryFailedChatMessage(messageId) {
      const message = this.outgoingMessages.get(messageId);
      if (!message || !["failed", "canceled"].includes(message.delivery_state)
        || this.activeWorkspace?.id !== message.workspace_id) return;
      await this.submitOutgoingMessage(message);
    },

    async resendChatMessage(messageId) {
      const workspace = this.activeWorkspace;
      if (!workspace) return;
      if (this.referenceUploadPending) {
        UI.toast("请等待图片上传完成或取消上传", "info");
        return;
      }
      const original = this.messages.find((message) => (
        message.id === messageId && message.role === "user"
      ));
      const modelId = this.el.chatModelSelect.value;
      if (!original || !modelId) return;

      const activeAssetIds = new Set(workspace.assets.map((asset) => asset.id));
      const attachmentIds = (original.attachments || []).map((asset) => asset.id);
      const generationReferenceIds = original.payload?.generation_reference_ids || [];
      if ([...attachmentIds, ...generationReferenceIds].some((id) => !activeAssetIds.has(id))) {
        UI.toast("原消息使用的图片已不存在，无法重新发送", "error");
        return;
      }
      const generationMode = original.payload?.generation_mode || (
        attachmentIds.length ? "auto" : "text2img"
      );
      if (generationMode === "img2img" && !generationReferenceIds.length) {
        UI.toast("原消息使用的垫图已不存在，无法重新发送", "error");
        return;
      }
      const content = String(original.content || "");
      if (!content.trim() && !attachmentIds.length) {
        UI.toast("原消息内容已不可用，无法重新发送", "error");
        return;
      }
      const message = {
        ...original,
        id: this.newMessageId(),
        workspace_id: workspace.id,
        model_id: modelId,
        attachment_ids: attachmentIds,
        generation_mode: generationMode,
        generation_reference_ids: generationReferenceIds,
        clarification_reply_to_id: original.payload?.clarification_reply_to_id || "",
        created_at: new Date().toISOString(),
      };
      workspace.settings.prompt_draft_id = "";
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
            clarification_reply_to_id: message.clarification_reply_to_id || "",
          },
          signal: operation.controller.signal,
        }),
      );
      const { operation, data, failure, canceled } = result;
      if (canceled) {
        const stillOwnMessage = String(message.operation_id || "").toLowerCase()
          === this.operationKey(operation);
        if (stillOwnMessage) {
          message.delivery_state = "canceled";
          message.delivery_error = "";
          message.operation_id = "";
          if (this.activeWorkspace?.id === message.workspace_id) this.renderMessages();
        }
        return;
      }
      if (!failure) {
        if (!data) {
          if (this.activeWorkspace?.id === message.workspace_id) this.renderMessages();
          return;
        }
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
      const outgoing = this.outgoingMessages.get(message.id) || message;
      outgoing.delivery_state = "failed";
      outgoing.delivery_error = failure.message;
      outgoing.operation_id = "";
      this.outgoingMessages.set(message.id, outgoing);
      if (this.activeWorkspace?.id === message.workspace_id) this.renderMessages();
    },

    async retryChatMessage(errorMessageId) {
      if (!this.activeWorkspace) return;
      const workspaceId = this.activeWorkspace.id;
      const modelId = this.el.chatModelSelect.value;
      if (!modelId) {
        UI.toast("管理员尚未配置可用的对话模型", "error");
        return;
      }
      const previousError = this.messages.find((message) => message.id === errorMessageId);
      if (previousError) delete previousError.retry_error;
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
      const { data, failure } = result;
      if (this.activeWorkspace?.id === workspaceId) {
        if (data) this.mergeConversationMessages([data.message], data.context);
        if (failure && previousError) previousError.retry_error = failure.message;
        this.renderMessages();
      }
    },

    openGenerationComposer(referenceIds = null) {
      if (!this.activeWorkspace || this.referenceUploadPending) return;
      const hadReviewedDraft = Boolean(this.activeWorkspace.settings.prompt_draft_id);
      this.activeWorkspace.settings.prompt_draft_id = "";
      this.activeWorkspace.settings.generation_stage = "final";
      const draft = this.el.chatInput.value.trim();
      const prompt = draft.slice(0, this.limits.max_prompt_characters);
      const requested = referenceIds === null
        ? [...this.currentChatSelection()]
        : [...new Set(referenceIds)];
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
        this.setMode(references.size ? "img2img" : "text2img", false);
        this.renderReferences();
        omitted = requested.length - references.size;
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
        stageByQuality[message.payload.quality_hint] || "final"
      );
      const omitted = this.showGenerationComposer(prompt, message.payload.reference_ids || []);
      this.updatePromptReviewState();
      this.renderGenerationPlan?.();
      if (omitted > 0) {
        const max = this.generationReferenceLimit();
        UI.toast(`当前渠道最多使用 ${max} 张垫图，已忽略 ${omitted} 张超限或已删除的参考图`);
      }
    },

    operationKey(operation) {
      return String(operation?.operation_id || operation?.message_id || "").trim().toLowerCase();
    },

    chatOperationMap(workspaceId, create = false) {
      if (!workspaceId) return null;
      let operations = this.chatOperations.get(workspaceId);
      if (operations && typeof operations.get !== "function") {
        const key = this.operationKey(operations);
        operations = key ? new Map([[key, operations]]) : new Map();
        this.chatOperations.set(workspaceId, operations);
      }
      if (!operations && create) {
        operations = new Map();
        this.chatOperations.set(workspaceId, operations);
      }
      return operations || null;
    },

    chatOperationList(workspaceId = this.activeWorkspace?.id) {
      return [...(this.chatOperationMap(workspaceId)?.values() || [])];
    },

    chatOperationForId(workspaceId, operationId = "") {
      const operations = this.chatOperationMap(workspaceId);
      if (!operations) return null;
      const normalized = String(operationId || "").trim().toLowerCase();
      if (normalized && operations.has(normalized)) return operations.get(normalized);
      return [...operations.values()].find((operation) => (
        [operation.operation_id, operation.message_id]
          .filter(Boolean)
          .map((value) => String(value).toLowerCase())
          .includes(normalized)
      )) || null;
    },

    workspacePrimaryChatOperation(workspaceId = this.activeWorkspace?.id) {
      return this.chatOperationList(workspaceId)
        .filter((operation) => ["reply", "prompt_draft"].includes(operation.kind))
        .sort((left, right) => String(left.started_at || "").localeCompare(String(right.started_at || "")))[0]
        || null;
    },

    workspaceHasActiveConversationOperation(workspaceId = this.activeWorkspace?.id) {
      return Boolean(workspaceId && (
        this.chatOperationList(workspaceId).length
        || this.canceledChatOperationIds.get(workspaceId)?.size
      ));
    },

    workspaceChatBusy(workspaceId = this.activeWorkspace?.id) {
      return this.workspaceHasActiveConversationOperation(workspaceId);
    },

    chatOperationAwaitingMessageAcceptance(
      operation,
      workspaceId = this.activeWorkspace?.id,
    ) {
      if (!operation?.message_id) return false;
      if (this.chatPreviewForOperation(workspaceId, operation)?.targetText) return false;
      if (this.outgoingMessages.has(operation.message_id)) return true;
      return workspaceId === this.activeWorkspace?.id && !this.messages.some((message) => (
        message.id === operation.message_id
      ));
    },

    chatOperationHasReply(operation) {
      if (!operation?.message_id) return false;
      return this.messages.some((message) => (
        message.role === "assistant"
        && message.payload?.reply_to_message_id === operation.message_id
      ));
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
          await this.startChatPreviewStream(workspaceId, operation);
          data = await request(operation);
          await this.completeChatPreview(workspaceId, operation, data, messageId);
          if (operation.canceled) {
            canceled = true;
            this.requestOperationCancellation(workspaceId, operation.operation_id);
          }
        }
      } catch (error) {
        canceled = this.isChatOperationCanceled(operation, error);
        if (!canceled) failure = error;
      } finally {
        this.stopChatPreviewStream(workspaceId, operation.operation_id);
        this.finishLocalChatOperation(workspaceId, operation);
      }
      return {
        operation,
        data,
        failure,
        canceled: canceled || this.isChatOperationCanceled(operation),
      };
    },

    chatPreviewForOperation(workspaceId, operation) {
      if (!workspaceId || !operation) return null;
      const previews = this.chatPreviewMap(workspaceId);
      const operationIds = [operation.operation_id, operation.message_id]
        .filter(Boolean)
        .map((value) => String(value).toLowerCase());
      return operationIds.map((id) => previews?.get(id)).find(Boolean)
        || [...(previews?.values() || [])].find((preview) => operationIds.includes(preview.operationId))
        || null;
    },

    chatPreviewMap(workspaceId, create = false) {
      if (!workspaceId) return null;
      let previews = this.chatPreviews.get(workspaceId);
      if (previews && typeof previews.get !== "function") {
        const key = String(previews.operationId || "").toLowerCase();
        previews = key ? new Map([[key, previews]]) : new Map();
        this.chatPreviews.set(workspaceId, previews);
      }
      if (!previews && create) {
        previews = new Map();
        this.chatPreviews.set(workspaceId, previews);
      }
      return previews || null;
    },

    async startChatPreviewStream(workspaceId, operation) {
      const operationId = this.operationKey(operation);
      if (!workspaceId || !operationId
        || !["reply", "prompt_draft"].includes(operation.kind)
        || typeof window.EventSource !== "function") return;
      const previews = this.chatPreviewMap(workspaceId, true);
      const existing = previews.get(operationId);
      if (existing && (existing.source || existing.finished)) return;
      const state = {
        workspaceId,
        operationId: operationId.toLowerCase(),
        source: null,
        targetText: "",
        displayedText: "",
        handoffTimer: null,
        drainResolve: null,
        finalText: "",
        finished: false,
      };
      previews.set(operationId, state);
      try {
        await UI.api(
          `/api/workspaces/${encodeURIComponent(workspaceId)}`
            + `/operations/${encodeURIComponent(operationId)}/preview-reservation`,
          { method: "POST", body: {} },
        );
      } catch (_error) {
        if (previews.get(operationId) === state) previews.delete(operationId);
        if (!previews.size) this.chatPreviews.delete(workspaceId);
        return;
      }
      if (previews.get(operationId) !== state || operation.canceled) return;
      const source = new EventSource(
        `/api/workspaces/${encodeURIComponent(workspaceId)}`
          + `/operations/${encodeURIComponent(operationId)}/events`,
      );
      state.source = source;
      source.addEventListener("preview", (event) => {
        if (this.chatPreviewMap(workspaceId)?.get(operationId) !== state) return;
        try {
          const payload = JSON.parse(event.data);
          this.queueChatPreview(state, String(payload.text || ""));
        } catch (_error) {
          // Ignore malformed preview events; the final JSON response remains authoritative.
        }
      });
      source.addEventListener("close", () => {
        if (this.chatPreviewMap(workspaceId)?.get(operationId) !== state) return;
        source.close();
        state.source = null;
        state.finished = true;
      });
      source.onerror = () => {
        if (this.chatPreviewMap(workspaceId)?.get(operationId) !== state) return;
        source.close();
        state.source = null;
        state.finished = true;
      };
    },

    queueChatPreview(state, text) {
      if (!text || state.targetText === text
        || this.chatPreviewMap(state.workspaceId)?.get(state.operationId) !== state) {
        return;
      }
      if (state.finalText && text !== state.finalText) return;
      state.targetText = text;
      state.displayedText = text;
      const operation = this.chatOperationForId(state.workspaceId, state.operationId);
      const outgoing = operation?.message_id
        ? this.outgoingMessages.get(operation.message_id)
        : null;
      if (outgoing?.delivery_state === "sending") outgoing.delivery_state = "accepted";
      if (this.activeWorkspace?.id === state.workspaceId) this.renderMessages();
      this.updateChatPreviewText(state);
    },

    completeChatPreview(workspaceId, operation, data, messageId) {
      const state = this.chatPreviewForOperation(workspaceId, operation);
      if (!state || operation.canceled) return Promise.resolve();
      const messages = [
        ...(Array.isArray(data?.messages) ? data.messages : []),
        ...(data?.message ? [data.message] : []),
      ];
      const reply = messages.find((message) => (
        message?.role === "assistant"
        && message.kind !== "error"
        && (!messageId || message.payload?.reply_to_message_id === messageId || messages.length === 1)
      ));
      const finalText = String(reply?.content || "");
      if (finalText) {
        state.finalText = finalText;
        this.queueChatPreview(state, finalText);
      }
      if (!state.targetText) {
        return Promise.resolve();
      }
      if (this.reducedMotion.matches || document.hidden) {
        return Promise.resolve();
      }
      return new Promise((resolve) => {
        state.drainResolve = resolve;
        state.handoffTimer = window.setTimeout(() => {
          state.handoffTimer = null;
          if (state.drainResolve !== resolve) return;
          state.drainResolve = null;
          resolve();
        }, CHAT_PREVIEW_HANDOFF_MS);
      });
    },

    updateChatPreviewText(state) {
      if (this.activeWorkspace?.id !== state.workspaceId) return;
      const row = [...this.el.messageList.querySelectorAll(".message-row.assistant.pending")]
        .find((node) => node.dataset.workspaceId === state.workspaceId
          && node.dataset.operationId === state.operationId);
      const content = row?.querySelector(".message-stream-text");
      if (!content) return;
      const scrollGap = this.el.conversationScroll.scrollHeight
        - this.el.conversationScroll.scrollTop
        - this.el.conversationScroll.clientHeight;
      setText(content, state.displayedText);
      if (scrollGap < 120) this.scrollConversation();
    },

    stopChatPreviewStream(workspaceId, operationId = "") {
      const previews = this.chatPreviewMap(workspaceId);
      if (!previews) return;
      const targets = operationId
        ? [previews.get(String(operationId).toLowerCase())].filter(Boolean)
        : [...previews.values()];
      targets.forEach((state) => {
        state.source?.close();
        if (state.handoffTimer !== null) window.clearTimeout(state.handoffTimer);
        const finish = state.drainResolve;
        state.handoffTimer = null;
        state.drainResolve = null;
        finish?.();
        previews.delete(state.operationId);
      });
      if (!previews.size) this.chatPreviews.delete(workspaceId);
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
        stage: "preparing",
        stage_label: label,
        elapsed_seconds: 0,
        first_output_seconds: null,
        output_characters: 0,
        request_body_bytes: null,
        started_at: new Date().toISOString(),
        operation_id: this.newMessageId(),
        message_id: messageId,
        controller: new AbortController(),
        local: true,
        canceled: false,
      };
      const message = messageId ? this.outgoingMessages.get(messageId) : null;
      if (message) message.operation_id = operation.operation_id;
      this.chatOperationMap(workspaceId, true).set(operation.operation_id, operation);
      this.renderWorkspaceList();
      if (this.activeWorkspace?.id === workspaceId) this.renderMessages();
      this.schedulePoll(ACTIVE_POLL_INTERVAL);
      return operation;
    },

    finishLocalChatOperation(workspaceId, operation) {
      const operations = this.chatOperationMap(workspaceId);
      const key = this.operationKey(operation);
      if (operations?.get(key) === operation
        || operations?.get(key)?.local && operations.get(key).operation_id === operation.operation_id) {
        operations.delete(key);
      }
      if (operations && !operations.size) this.chatOperations.delete(workspaceId);
      this.renderWorkspaceList();
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
      const requestedId = String(operationId || "").trim().toLowerCase();
      const operation = requestedId
        ? this.chatOperationForId(workspaceId, requestedId)
        : this.workspacePrimaryChatOperation(workspaceId);
      const targetId = requestedId || this.operationKey(operation);
      if (!targetId) return;
      const operationIds = [operation?.operation_id, operation?.message_id]
        .filter(Boolean)
        .map((id) => String(id).toLowerCase());
      if (operation && operationIds.length && !operationIds.includes(targetId)) return;
      if (operation) {
        operation.canceled = true;
        operation.controller?.abort();
        const operations = this.chatOperationMap(workspaceId);
        const key = this.operationKey(operation);
        if (operations?.get(key) === operation) operations.delete(key);
        if (operations && !operations.size) this.chatOperations.delete(workspaceId);
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
      this.stopChatPreviewStream(workspaceId, this.operationKey(operation) || targetId);
      this.renderWorkspaceList();
      if (this.activeWorkspace?.id === workspaceId) this.renderMessages();
    },

    syncServerChatOperation(workspaceId, operation) {
      const previous = new Map(this.chatOperationMap(workspaceId) || []);
      const rawOperations = Array.isArray(operation?.operations)
        ? operation.operations
        : operation?.busy ? [operation] : [];
      const serverOperations = rawOperations.filter((item) => item?.busy !== false);
      const canceled = this.canceledChatOperationIds.get(workspaceId);
      const next = new Map();
      const sameId = (left, right) => String(left || "").toLowerCase() === String(right || "").toLowerCase();
      for (const serverOperation of serverOperations) {
        const operationId = this.operationKey(serverOperation);
        if (!operationId) continue;
        const canceledIds = [serverOperation.operation_id, serverOperation.message_id]
          .filter(Boolean)
          .map((id) => String(id).toLowerCase());
        if (canceled?.size && canceledIds.some((id) => canceled.has(id))) continue;
        const previousOperation = previous.get(operationId)
          || [...previous.values()].find((item) => sameId(item.message_id, serverOperation.message_id));
        if (previousOperation?.local
          && previousOperation.operation_id
          && serverOperation.operation_id
          && !sameId(previousOperation.operation_id, serverOperation.operation_id)) {
          continue;
        }
        const nextOperation = previousOperation?.local
          ? {
            ...previousOperation,
            ...serverOperation,
            local: true,
            controller: previousOperation.controller,
            canceled: previousOperation.canceled,
          }
          : { ...serverOperation, local: false };
        next.set(this.operationKey(nextOperation), nextOperation);
      }
      for (const [key, previousOperation] of previous) {
        if (previousOperation.local && !next.has(key)) next.set(key, previousOperation);
      }
      if (canceled?.size) {
        const serverIds = new Set(
          rawOperations.flatMap((item) => [item?.operation_id, item?.message_id])
            .filter(Boolean)
            .map((id) => String(id).toLowerCase()),
        );
        for (const id of canceled) {
          if (!serverIds.has(id)) canceled.delete(id);
        }
        if (!canceled.size) this.canceledChatOperationIds.delete(workspaceId);
      }
      const signature = (items) => JSON.stringify([...items.entries()].map(([key, item]) => [
        key,
        item.kind,
        item.label,
        item.started_at,
        item.operation_id,
        item.message_id,
        item.stage,
        item.stage_label,
        item.first_output_seconds,
        item.output_characters,
        item.request_body_bytes,
      ]));
      const changed = signature(previous) !== signature(next);
      if (next.size) this.chatOperations.set(workspaceId, next);
      else this.chatOperations.delete(workspaceId);
      for (const nextOperation of next.values()) void this.startChatPreviewStream(workspaceId, nextOperation);
      for (const [key, previousOperation] of previous) {
        if (!next.has(key)) this.stopChatPreviewStream(workspaceId, this.operationKey(previousOperation));
      }
      return changed;
    },

    workspaceHasActiveJob(workspaceId = this.activeWorkspace?.id) {
      if (!workspaceId) return false;
      return Boolean(this.workspaceJobMap(workspaceId)?.size)
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
      this.renderCanvasConflict();
      const noWorkspace = !this.activeWorkspace;
      const submissionCount = this.generationSubmissionList().length;
      const submissionBusy = submissionCount > 0;
      const canvasConflictPending = Boolean(
        this.canvasConflict && !this.canvasConflict.resolution,
      );
      const generationLocked = noWorkspace;
      const referenceUploading = this.referenceUploadPending;
      const hasModel = Boolean(this.el.chatModelSelect.value);
      this.el.generateButton.classList.toggle("loading", submissionBusy);
      setDisabled(this.el.chatInput, noWorkspace);
      setDisabled(
        this.el.chatSendButton,
        noWorkspace || referenceUploading || !hasModel,
      );
      this.el.chatSendButton.type = "submit";
      this.setActionIcon(this.el.chatSendButton, "arrow-up", "send");
      const sendTitle = referenceUploading ? "等待图片上传完成" : "发送消息";
      setAttribute(this.el.chatSendButton, "title", sendTitle);
      setAttribute(this.el.chatSendButton, "aria-label", sendTitle);
      setDisabled(this.el.chatModelSelect, noWorkspace || !hasModel);
      setDisabled(this.el.creativeDirectionSelect, noWorkspace);
      setDisabled(this.el.galleryCategorySelect, noWorkspace);
      setDisabled(this.el.translatePrompt, noWorkspace);
      setDisabled(this.el.chatReferenceButton, noWorkspace || referenceUploading);
      setDisabled(this.el.directGenerationButton, generationLocked || referenceUploading);
      setDisabled(this.el.generationStrategy, generationLocked || referenceUploading);
      const promptReviewed = Boolean(this.currentPromptDraft());
      this.el.promptReviewStatus.classList.toggle("is-reviewed", promptReviewed);
      setText(
        this.el.promptReviewStatus.querySelector("span"),
        promptReviewed ? "最终提示词已就绪" : "可直接编辑提示词",
      );
      setDisabled(
        this.el.generateButton,
        generationLocked || referenceUploading || !this.currentChannel() || canvasConflictPending,
      );
      this.setActionIcon(this.el.generateButton, "sparkles", "generate");
      setText(this.el.generateButtonLabel, "开始生成");
      setDisabled(
        this.el.canvasConflictApply,
        generationLocked
          || !this.canvasConflict
          || !this.canvasRequestTargetSize(this.canvasConflict.request)
          || this.canvasConflict.resolution === "conversation",
      );
      setDisabled(
        this.el.canvasConflictKeep,
        generationLocked || !this.canvasConflict || this.canvasConflict.resolution === "panel",
      );
      const generateTitle = submissionBusy ? `已有 ${submissionCount} 笔正在提交` : "";
      setAttribute(this.el.generateButton, "title", generateTitle);
      setDisabled(this.el.generationBackButton, noWorkspace);
      setDisabled(
        this.el.referenceAdd,
        referenceUploading
          || (this.activeWorkspace?.assets.length || 0) >= this.limits.max_assets_per_workspace,
      );
      setDisabled(this.el.referenceLibrary, referenceUploading);
      setDisabled(
        this.el.clearWorkspaceButton,
        this.workspaceHasActiveJob()
          || this.workspaceHasActiveConversationOperation()
          || this.workspaceHasGenerationSubmission()
          || generationLocked
          || referenceUploading,
      );
      setDisabled(this.el.libraryButton, noWorkspace);
      this.el.workspaceList.querySelectorAll("[data-delete-workspace]").forEach((button) => {
        const workspaceId = button.dataset.deleteWorkspace;
        const activeLocked = workspaceId === this.activeWorkspace?.id
          && (generationLocked || referenceUploading);
        setDisabled(
          button,
          this.workspaceHasActiveConversationOperation(workspaceId)
            || this.workspaceHasActiveJob(workspaceId)
            || this.workspaceHasGenerationSubmission(workspaceId)
            || activeLocked,
        );
      });
      this.el.messageList.querySelectorAll(
        "[data-retry-message], [data-retry-send], [data-resend-message]",
      ).forEach((button) => {
        setDisabled(button, noWorkspace || referenceUploading || !hasModel);
      });
      this.el.messageList.querySelectorAll("[data-cancel-chat]").forEach((button) => {
        const buttonWorkspace = button.dataset.cancelWorkspace || this.activeWorkspace?.id;
        const buttonOperation = button.dataset.cancelOperation;
        setDisabled(button, !this.chatOperationForId(buttonWorkspace, buttonOperation));
      });
      this.el.messageList.querySelectorAll("[data-use-prompt-draft]").forEach((button) => {
        setDisabled(button, noWorkspace);
      });
      const placeholder = noWorkspace ? "暂无工作站" : "描述你想生成的画面...";
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
