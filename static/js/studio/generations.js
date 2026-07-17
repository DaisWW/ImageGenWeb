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
    setAttribute,
  } = window.ImageGenStudio;

  Object.assign(StudioApp.prototype, {
    updatePrice() {
      const count = this.isAnimationWorkspace()
        ? Math.min(
          this.limits.max_animation_frames,
          Math.max(2, Number(this.el.animationFrameCount.value || 8)),
        )
        : Math.min(
          this.limits.max_batch_images, Math.max(1, Number(this.el.batchCount.value || 1)),
        );
      const unit = this.isAnimationWorkspace() ? "帧" : "张";
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
        const selection = this.currentSelection(workspace.id);
        const omitted = this.trimReferenceSelection(selection, this.generationReferenceLimit());
        if (omitted) {
          this.renderReferences();
          UI.toast(`渠道垫图上限已更新，已取消 ${omitted} 张超限图片`, "info");
        }
        const referenceIds = [...selection];
        const settings = this.collectSettings();
        if (this.isAnimationWorkspace()) settings.mode = "img2img";
        if (this.isAnimationWorkspace() && referenceIds.length !== 1) {
          UI.toast("请先添加并选择一张母图", "error");
          return;
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
        const reviewedDraft = this.currentPromptDraft();
        settings.prompt_draft_id = reviewedDraft?.id || "";
        await this.flushSettings();
        const data = await UI.api("/api/generations", {
          method: "POST",
          body: {
            workspace_id: workspace.id,
            ...settings,
            reference_ids: settings.mode === "img2img" ? referenceIds : [],
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
        UI.toast(`任务已提交，${UI.money(Number(data.job.price_per_image_rmb) * data.job.requested_count)} 已预占`, "success");
      } catch (error) {
        UI.toast(error.message, "error");
      } finally {
        button.classList.remove("loading");
        this.updateInteractionState();
      }
    },

    async loadJobs(workspaceId = this.activeWorkspace?.id) {
      if (!workspaceId) return;
      const existing = this.loadingJobWorkspaces.get(workspaceId);
      if (existing) return existing;
      const request = (async () => {
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
      })();
      this.loadingJobWorkspaces.set(workspaceId, request);
      try {
        return await request;
      } finally {
        if (this.loadingJobWorkspaces.get(workspaceId) === request) {
          this.loadingJobWorkspaces.delete(workspaceId);
        }
      }
    },

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
    },

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
    },

    applyJobUpdate(job) {
      const index = this.jobs.findIndex((entry) => entry.id === job.id);
      if (index >= 0) this.jobs[index] = job;
      if (TERMINAL.has(job.status)) this.workspaceJobs.delete(job.workspace_id);
      else this.workspaceJobs.set(job.workspace_id, job);
      this.updateWorkspaceJobDisplays();
      this.renderJobs();
    },

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
          UI.toast("任务已取消", "success");
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
    },

  });
})();
