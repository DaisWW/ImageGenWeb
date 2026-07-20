(() => {
  "use strict";

  const {
    StudioApp,
    UI,
    STATUS,
    TERMINAL,
    setText,
    setHidden,
  } = window.ImageGenStudio;

  Object.assign(StudioApp.prototype, {
    async loadMessages(workspaceId = this.activeWorkspace?.id) {
      if (!workspaceId) return;
      return this.runSingleFlight(this.loadingMessageWorkspaces, workspaceId, async () => {
        try {
          const data = await UI.api(`/api/workspaces/${workspaceId}/messages?limit=200`);
          const operationChanged = this.syncServerChatOperation(
            workspaceId,
            data.conversation_operation,
          );
          if (operationChanged) this.renderWorkspaceList();
          const canceledOutgoingIds = new Set(
            [...this.outgoingMessages.values()]
              .filter((message) => (
                message.workspace_id === workspaceId && message.delivery_state === "canceled"
              ))
              .map((message) => message.id),
          );
          const nextMessages = (data.messages || []).filter((message) => (
            !canceledOutgoingIds.has(message.id)
            && !canceledOutgoingIds.has(message.payload?.reply_to_message_id)
          ));
          const serverMessageIds = new Set(nextMessages.map((message) => message.id));
          let outgoingChanged = false;
          for (const [id, message] of this.outgoingMessages) {
            if (
              message.workspace_id === workspaceId
              && serverMessageIds.has(id)
              && message.delivery_state !== "canceled"
            ) {
              this.outgoingMessages.delete(id);
              outgoingChanged = true;
            }
          }
          if (this.activeWorkspace?.id === workspaceId) {
            const messagesChanged = this.messages.length !== nextMessages.length
              || this.messages[0]?.id !== nextMessages[0]?.id
              || this.messages.at(-1)?.id !== nextMessages.at(-1)?.id;
            const contextChanged = JSON.stringify(this.conversationContext)
              !== JSON.stringify(data.context);
            if (messagesChanged) this.messages = nextMessages;
            if (contextChanged) this.conversationContext = data.context;
            if (messagesChanged || contextChanged || operationChanged || outgoingChanged) {
              this.renderMessages();
            }
            this.updatePromptReviewState();
          }
        } catch (error) {
          UI.toast(error.message, "error");
        }
      });
    },

    renderMessages() {
      setText(
        this.el.conversationEmptyLabel,
        this.activeWorkspace ? "开始一段新对话" : "暂无工作站",
      );
      const scrollGap = this.el.conversationScroll.scrollHeight
        - this.el.conversationScroll.scrollTop
        - this.el.conversationScroll.clientHeight;
      const keepAtBottom = scrollGap < 120;
      const workspaceId = this.activeWorkspace?.id;
      const messageIds = new Set(this.messages.map((message) => message.id));
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
        ...[...this.outgoingMessages.values()]
          .filter((message) => (
            message.workspace_id === workspaceId && !messageIds.has(message.id)
          ))
          .map((message) => ({
            type: "message",
            createdAt: message.created_at,
            id: message.id,
            value: message,
          })),
      ].sort((left, right) => {
        const time = new Date(left.createdAt || 0).getTime() - new Date(right.createdAt || 0).getTime();
        return time || String(left.id).localeCompare(String(right.id));
      });
      const operation = this.chatOperations.get(workspaceId);
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
            provider_label: this.el.chatModelSelect.selectedOptions[0]?.textContent || "",
            workspace_id: workspaceId,
            operation_id: operation.operation_id || operation.message_id || "",
          },
        });
      }
      const timelineChanged = this.reconcileTimeline(timeline);
      setHidden(this.el.conversationEmpty, timeline.length > 0);
      this.renderContextStatus();
      this.updateInteractionState();
      if (keepAtBottom && timelineChanged) this.scrollConversation();
    },

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
        } else {
          const state = `${entry.value.kind || "message"}:${entry.value.delivery_state || "stored"}:${entry.value.operation_id || ""}`;
          if (!node || node.dataset.messageState !== state) {
            const replacement = this.messageCard(entry.value);
            if (node) node.replaceWith(replacement);
            node = replacement;
            UI.icons(node);
            layoutChanged = true;
          }
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
    },

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
    },

    chatCancelButton(message, label) {
      const cancel = document.createElement("button");
      cancel.type = "button";
      cancel.className = "button ghost small message-cancel-button";
      cancel.dataset.cancelChat = "";
      cancel.dataset.cancelWorkspace = message.workspace_id || this.activeWorkspace?.id || "";
      cancel.dataset.cancelOperation = message.operation_id || "";
      cancel.innerHTML = `<i data-lucide="square"></i>${label}`;
      return cancel;
    },

    messageCard(message) {
      const row = document.createElement("article");
      row.className = [
        "message-row",
        message.role,
        message.kind || "message",
        message.delivery_state || "",
      ].filter(Boolean).join(" ");
      row.dataset.messageState = `${message.kind || "message"}:${message.delivery_state || "stored"}:${message.operation_id || ""}`;
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
      if (message.delivery_state === "sending") timingParts.push("发送中");
      else if (message.delivery_state === "canceled") timingParts.push("已取消");
      else if (message.delivery_state === "failed") timingParts.push("发送失败");
      else if (message.created_at) timingParts.push(UI.dateTime(message.created_at));
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
        card.append(pending, this.chatCancelButton(message, "取消等待"));
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
        if (message.delivery_state === "failed") {
          const retry = document.createElement("button");
          retry.type = "button";
          retry.className = "button danger small";
          retry.dataset.retrySend = message.id;
          retry.title = message.delivery_error || "消息发送失败，点击重试";
          retry.innerHTML = '<i data-lucide="circle-alert"></i><span>重试发送</span>';
          card.append(retry);
        }
        if (message.delivery_state === "canceled") {
          const retry = document.createElement("button");
          retry.type = "button";
          retry.className = "button ghost small";
          retry.dataset.retrySend = message.id;
          retry.innerHTML = '<i data-lucide="refresh-cw"></i><span>重新发送</span>';
          card.append(retry);
        }
        if (message.delivery_state === "sending" && message.operation_id) {
          card.append(this.chatCancelButton(message, "取消发送"));
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
    },

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
      const meta = document.createElement("div");
      meta.className = "prompt-draft-meta";
      const direction = this.creativeDirections.find(
        (item) => item.id === payload.creative_direction,
      );
      const styleLabel = (payload.style_labels || payload.style_tags || []).join(" / ");
      const sceneLabel = (payload.scene_labels || payload.scene_tags || []).join(" / ");
      const metadata = [
        direction?.label || "其他应用场景",
        payload.template_label || "自定义 Craft",
        payload.reference_usage === "generation"
          ? `使用 ${(payload.reference_ids || []).length} 张垫图`
          : payload.reference_usage === "analysis_only" ? "图片仅用于分析" : "",
        styleLabel,
        sceneLabel,
        `${(payload.hard_checks || []).length} 项验收门槛`,
        `${{ low: "草稿", medium: "精修", high: "成品" }[payload.quality_hint] || "草稿"}建议`,
      ].filter(Boolean);
      metadata.forEach((label) => {
        const chip = document.createElement("span");
        chip.textContent = label;
        meta.append(chip);
      });
      const selection = document.createElement("p");
      selection.className = "prompt-draft-selection";
      selection.textContent = payload.selection_reason
        ? `AI 匹配：${payload.selection_reason}`
        : "AI 使用通用 Craft 整理当前需求";
      const action = document.createElement("button");
      action.type = "button";
      action.className = "button primary small";
      action.dataset.usePromptDraft = message.id;
      action.innerHTML = '<i data-lucide="image-plus"></i>使用此提示词生图';
      wrap.append(summaryLabel, summary, promptLabel, prompt, meta, selection, action);
      return wrap;
    },

    formatElapsed(value) {
      const seconds = Number(value);
      if (!Number.isFinite(seconds)) return "--";
      if (seconds < 60) return `${seconds.toFixed(seconds < 10 ? 1 : 0)} 秒`;
      const minutes = Math.floor(seconds / 60);
      return `${minutes} 分 ${Math.round(seconds % 60)} 秒`;
    },

    newMessageId() {
      const bytes = new Uint8Array(16);
      window.crypto.getRandomValues(bytes);
      return [...bytes].map((value) => value.toString(16).padStart(2, "0")).join("");
    },

  });
})();
