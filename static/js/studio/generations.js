(() => {
  "use strict";

  const {
    StudioApp,
    UI,
    STATUS,
    TERMINAL,
    ACTIVE_POLL_INTERVAL,
    JOB_ELEMENT_SELECTOR,
    setText,
    setHidden,
    setDisabled,
  } = window.ImageGenStudio;

  Object.assign(StudioApp.prototype, {
    updatePrice() {
      const count = Math.min(
        this.limits.max_batch_images, Math.max(1, Number(this.el.batchCount.value || 1)),
      );
      const unit = "张";
      const price = Number(this.currentChannel()?.price_rmb || 0);
      this.el.priceEstimateLabel.textContent = `${count} ${unit}预计总价`;
      this.el.priceEstimate.textContent = UI.money(price * count);
      this.channels.forEach((channel, index) => {
        const option = this.el.channelSelect.options[index];
        if (option) {
          const unitPrice = UI.money(channel.price_rmb);
          option.textContent = `${channel.label} · ${unitPrice}/${unit}${channel.configured ? "" : " · 未配置"}`;
        }
      });
    },

    async submitGeneration(event) {
      event.preventDefault();
      if (!this.activeWorkspace) {
        UI.toast("暂无可用渠道", "error");
        return;
      }
      const workspace = this.activeWorkspace;
      const activeSubmission = this.generationSubmissions.get(workspace.id);
      if (activeSubmission) {
        this.cancelGenerationSubmission(workspace.id);
        return;
      }
      if (this.referenceUploadPending) {
        UI.toast("请等待图片上传完成或取消上传", "info");
        return;
      }
      if (this.workspaceChatBusy()) {
        UI.toast("请等待当前 AI 回复完成后再开始生成", "error");
        return;
      }
      const button = this.el.generateButton;
      if (button.classList.contains("loading")) return;
      const controller = new AbortController();
      const operation = {
        controller,
        canceled: false,
        postStarted: false,
        operation_id: this.newMessageId(),
      };
      this.generationSubmissions.set(workspace.id, operation);
      button.disabled = true;
      button.classList.add("loading");
      this.updateInteractionState();
      try {
        const requestOptions = { signal: controller.signal };
        await Promise.all([
          this.loadChannels(false, requestOptions),
          this.loadRuntimeSettings(false, requestOptions),
        ]);
        if (operation.canceled || this.activeWorkspace?.id !== workspace.id) return;
        if (!this.currentChannel()) {
          UI.toast("暂无可用渠道", "error");
          return;
        }
        if (!this.validateSizeInput(true)) return;
        const selection = this.currentSelection(workspace.id);
        const omitted = this.trimReferenceSelection(selection, this.generationReferenceLimit());
        if (omitted) {
          this.renderReferences();
          UI.toast(`渠道垫图上限已更新，已取消 ${omitted} 张超限图片`, "info");
        }
        const referenceIds = [...selection];
        const settings = this.collectSettings();
        if (!settings.prompt.trim()) {
          UI.toast("请输入提示词", "error");
          this.el.promptInput.focus();
          return;
        }
        if (settings.mode === "img2img" && !referenceIds.length) {
          UI.toast("垫图生图至少选择一张垫图", "error");
          return;
        }
        const reviewedDraft = this.currentPromptDraft();
        settings.prompt_draft_id = reviewedDraft?.id || "";
        await this.flushSettings(workspace.id, requestOptions);
        if (operation.canceled) return;
        operation.postStarted = true;
        const data = await UI.api("/api/generations", {
          method: "POST",
          body: {
            workspace_id: workspace.id,
            ...settings,
            reference_ids: settings.mode === "img2img" ? referenceIds : [],
            operation_id: operation.operation_id,
          },
        });
        if (operation.canceled) {
          void this.cancelGenerationJob(data.job);
          return;
        }
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
        UI.toast(`任务已提交，${UI.money(Number(data.job.price_per_image_rmb) * data.job.requested_count)} 已预占`, "success");
      } catch (error) {
        if (!operation.canceled && error?.name !== "AbortError") UI.toast(error.message, "error");
      } finally {
        const stillCurrent = this.generationSubmissions.get(workspace.id) === operation;
        if (stillCurrent) {
          this.generationSubmissions.delete(workspace.id);
          button.classList.remove("loading");
          this.updateInteractionState();
        }
      }
    },

    cancelGenerationSubmission(workspaceId = this.activeWorkspace?.id) {
      const operation = this.generationSubmissions.get(workspaceId);
      if (!operation) return;
      operation.canceled = true;
      if (operation.postStarted) {
        this.requestOperationCancellation(workspaceId, operation.operation_id);
      } else {
        operation.controller?.abort();
      }
      if (this.generationSubmissions.get(workspaceId) === operation) {
        this.generationSubmissions.delete(workspaceId);
      }
      this.el.generateButton.classList.remove("loading");
      this.updateInteractionState();
      UI.toast("生成提交已取消，可立即修改后重试", "success");
    },

    async cancelGenerationJob(job) {
      if (!job?.id) return;
      const visible = this.activeWorkspace?.id === job.workspace_id;
      const optimistic = this.optimisticCanceledJob(job);
      if (visible) {
        if (!this.jobs.some((entry) => entry.id === job.id)) this.jobs.unshift(job);
        this.applyJobUpdate(optimistic);
      }
      try {
        const canceledJob = await this.requestGenerationCancellation(job.id);
        if (this.activeWorkspace?.id === canceledJob.workspace_id) {
          this.applyJobUpdate(canceledJob);
        } else if (this.workspaceJobs.get(canceledJob.workspace_id)?.id === canceledJob.id) {
          this.workspaceJobs.delete(canceledJob.workspace_id);
          this.updateWorkspaceJobDisplays();
        }
      } catch (error) {
        const current = this.jobs.find((entry) => entry.id === job.id);
        if (visible && current?.status === optimistic.status) this.applyJobUpdate(job);
        UI.toast(error.message, "error");
      }
    },

    async requestGenerationCancellation(jobId) {
      const data = await UI.api(`/api/generations/${jobId}/cancel`, { method: "POST" });
      await this.refreshBalance();
      return data.job;
    },

    async loadJobs(workspaceId = this.activeWorkspace?.id) {
      if (!workspaceId) return;
      return this.runSingleFlight(this.loadingJobWorkspaces, workspaceId, async () => {
        try {
          const data = await UI.api(`/api/generations?workspace_id=${encodeURIComponent(workspaceId)}&limit=100`);
          const currentJobs = new Map(this.jobs.map((job) => [job.id, job]));
          const jobs = data.jobs.map((job) => {
            const current = currentJobs.get(job.id);
            return current && TERMINAL.has(current.status) && !TERMINAL.has(job.status)
              ? current
              : job;
          });
          const activeJob = jobs.find((job) => !TERMINAL.has(job.status));
          if (activeJob) this.workspaceJobs.set(workspaceId, activeJob);
          else this.workspaceJobs.delete(workspaceId);
          this.updateWorkspaceJobDisplays();
          if (this.activeWorkspace?.id === workspaceId) {
            this.jobs = jobs;
            this.renderJobs();
          }
        } catch (error) {
          UI.toast(error.message, "error");
        }
      });
    },

    async loadWorkspaceJobs() {
      return this.runSingleFlight(this.loadingWorkspaceJobs, "active", async () => {
        try {
          const data = await UI.api("/api/generations/active");
          this.workspaceJobs = new Map(
            data.jobs
              .filter((job) => !this.cancelingJobs.has(job.id))
              .map((job) => [job.workspace_id, job]),
          );
          this.updateWorkspaceJobDisplays();
        } catch {
          // 当前工作站请求会显示持续性的 API 错误。
        }
      });
    },

    renderJobs() {
      this.renderMessages();
      this.updateMetrics();
    },

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
            <div class="job-error" data-job-error role="alert" hidden>
              <i data-lucide="circle-alert"></i>
              <span><strong>失败原因：</strong><span data-job-error-message></span></span>
            </div>
          </div>
          <div class="output-grid"></div>
        </div>`;
      this.updateJobCard(article, job);
      return article;
    },

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
        cancel: fields.jobCancel,
        progress: fields.jobProgress,
        prompt: fields.jobPrompt,
        channel: fields.jobChannel,
        model: fields.jobModel,
        size: fields.jobSize,
        quality: fields.jobQuality,
        count: fields.jobCount,
        charge: fields.jobCharge,
        error: fields.jobError,
        errorMessage: fields.jobErrorMessage,
        outputGrid: fields.outputGrid,
      };
      this.jobElementCache.set(article, elements);
      return elements;
    },

    updateJobCard(article, job) {
      const [statusLabel, statusClass] = STATUS[job.status] || [job.status, ""];
      const enteringClass = article.classList.contains("timeline-enter") ? " timeline-enter" : "";
      const className = `job-card timeline-job ${statusClass}${enteringClass}`;
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
      setText(elements.count, `${job.succeeded_count}/${job.requested_count} 张`);
      setText(elements.charge, `${UI.money(job.charged_rmb)} 已扣`);
      const failureReasons = [...new Set(
        (job.items || [])
          .filter((item) => ["failed", "interrupted"].includes(item.status))
          .map((item) => String(item.error || "").trim())
          .filter(Boolean),
      )];
      setHidden(elements.error, !failureReasons.length);
      setText(elements.errorMessage, failureReasons.join("；"));
      this.reconcileOutputTiles(elements.outputGrid, job);
      if (!elements.eta.hidden) UI.icons(elements.eta);
      if (!elements.cancel.hidden) UI.icons(elements.cancel);
      if (!elements.error.hidden) UI.icons(elements.error);
      return article;
    },

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
    },

    outputTile(job, item) {
      const button = document.createElement("button");
      button.type = "button";
      return this.updateOutputTile(button, job, item);
    },

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
        image.alt = `生成结果 ${item.position + 1}`;
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
    },

    applyJobUpdate(job) {
      const index = this.jobs.findIndex((entry) => entry.id === job.id);
      if (index >= 0) this.jobs[index] = job;
      const activeWorkspaceJob = this.workspaceJobs.get(job.workspace_id);
      if (TERMINAL.has(job.status)) {
        if (!activeWorkspaceJob || activeWorkspaceJob.id === job.id) {
          this.workspaceJobs.delete(job.workspace_id);
        }
      } else {
        this.workspaceJobs.set(job.workspace_id, job);
      }
      this.updateWorkspaceJobDisplays();
      this.renderJobs();
    },

    optimisticCanceledJob(job) {
      const completedAt = new Date().toISOString();
      const items = (job.items || []).map((item) => (
        ["queued", "running", "canceling"].includes(item.status)
          ? { ...item, status: "canceled", completed_at: completedAt, image_url: null, thumbnail_url: null }
          : item
      ));
      const succeededCount = items.filter((item) => item.status === "succeeded").length;
      return {
        ...job,
        status: succeededCount ? "partial" : "canceled",
        progress_percent: 100,
        completed_at: completedAt,
        can_cancel: false,
        reserved_rmb: "0.0000",
        canceled_count: items.filter((item) => item.status === "canceled").length,
        items,
      };
    },

    async handleJobClick(event) {
      const cancel = event.target.closest("[data-cancel-job]");
      if (cancel) {
        const jobId = cancel.dataset.cancelJob;
        if (this.cancelingJobs.has(jobId)) return;
        const job = this.jobs.find((entry) => entry.id === jobId)
          || this.workspaceJobs.get(this.activeWorkspace?.id);
        if (!job || job.id !== jobId) return;
        this.cancelingJobs.add(jobId);
        UI.toast("任务已取消，可立即开始新的生成", "success");
        void this.cancelGenerationJob(job).finally(() => {
          this.cancelingJobs.delete(jobId);
        });
        return;
      }
      const tile = event.target.closest("[data-item-id]");
      if (!tile || tile.disabled) return;
      const job = this.jobs.find((entry) => entry.id === tile.dataset.jobId);
      const item = job?.items.find((entry) => entry.id === tile.dataset.itemId);
      if (job && item) this.showDetail(job, item);
    },

  });
})();
