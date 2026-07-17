(() => {
  "use strict";

  const {
    StudioApp,
    UI,
    STATUS,
    TERMINAL,
    ACTIVE_POLL_INTERVAL,
    IDLE_POLL_INTERVAL,
    setText,
  } = window.ImageGenStudio;

  Object.assign(StudioApp.prototype, {
    updateMetrics() {
      const running = this.jobs.filter((job) => ["running", "canceling"].includes(job.status));
      const queued = this.jobs.filter((job) => job.status === "queued");
      setText(this.el.runningMetric, running.length);
      setText(this.el.queueMetric, queued.length);
      this.updateEtaMetric();
      const busy = running.length > 0 || queued.length > 0;
      this.el.workspaceStateDot.classList.toggle("busy", busy);
      setText(this.el.workspaceStatus, !this.activeWorkspace
        ? "未选择工作站"
        : running.length
        ? `${running.length} 个任务正在生成`
        : queued.length ? `${queued.length} 个任务排队中` : "等待任务");
      this.updateInteractionState();
    },

    updateEtaMetric() {
      const running = this.jobs.filter((job) => ["running", "canceling"].includes(job.status));
      const ends = running.map((job) => job.estimated_end_at).filter(Boolean).sort();
      const latestEnd = ends.at(-1);
      setText(this.el.etaMetric, latestEnd ? UI.timeOnly(latestEnd) : "--:--");
      setText(this.el.etaRemainingMetric, latestEnd ? this.formatRemaining(latestEnd) : "--");
    },

    async refreshBalance() {
      try {
        const data = await UI.api("/api/me?ledger=0");
        this.user = data.user;
        UI.updateWallet(data.user, data.spending);
      } catch {
        // 后续请求会显示鉴权或网络错误。
      }
    },

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
    },

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
    },

    async poll() {
      if (this.polling || document.hidden) return;
      this.polling = true;
      try {
        const selectedWasActive = this.jobs.some((job) => !TERMINAL.has(job.status));
        const hadActiveWorkspace = this.workspaceJobs.size > 0;
        await this.loadWorkspaceJobs();
        const selectedIsActive = this.workspaceJobs.has(this.activeWorkspace?.id);
        const requests = [...this.chatOperations]
          .map(([workspaceId]) => this.loadMessages(workspaceId));
        if (selectedWasActive || selectedIsActive) requests.push(this.loadJobs());
        if (hadActiveWorkspace || this.workspaceJobs.size > 0 || selectedWasActive) {
          requests.push(this.refreshBalance());
        }
        await Promise.all(requests);
      } finally {
        this.polling = false;
      }
    },

    pollInterval() {
      if (document.hidden) return IDLE_POLL_INTERVAL;
      const active = this.workspaceJobs.size > 0
        || this.chatOperations.size > 0
        || this.jobs.some((job) => !TERMINAL.has(job.status));
      return active ? ACTIVE_POLL_INTERVAL : IDLE_POLL_INTERVAL;
    },

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
    },
  });
})();
