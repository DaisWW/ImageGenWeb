(() => {
  "use strict";

  const {
    StudioApp,
    UI,
    setText,
    setHidden,
    setDisabled,
  } = window.ImageGenStudio;

  const UI_KIT_RECONSTRUCTION_PROMPT = [
    "我要把参考图重建为可直接开发使用的游戏 UI Kit。不要抠取、分割或复制原图像素，也不要直接生成整屏、组件展示板或图集。",
    "请先把参考界面整理成“模块 → 原子资源”组件树，明确区分：可九宫格拉伸的空面板/边框、独立图标/装饰、状态条轨道/填充/开关状态，以及必须由引擎渲染的文字和动态数值。",
    "本轮只做组件拆解，并用一个选择问题让我选定一个原子资源；未选定前不要给最终生图提示词。选定后每次只为一个无文字、无动态数值、真实透明背景的独立素材整理提示词。",
  ].join("\n\n");

  Object.assign(StudioApp.prototype, {
    showDetail(job, item) {
      this.detailItemId = item.id;
      this.detailJobId = job.id;
      this.el.detailImage.src = item.image_url;
      this.prepareImageReveal(this.el.detailImage);
      this.el.detailPrompt.textContent = item.prompt || job.prompt;
      const transparentLabel = job.transparent_background ? " · 透明背景" : "";
      const stageLabel = { draft: "草稿", refine: "精修", final: "成品" }[
        job.workflow?.generation_stage
      ] || "未标记";
      const canvasResolutionLabel = {
        conversation: "采用对话画幅",
        panel: "保持面板尺寸",
      }[job.workflow?.canvas_resolution] || "";
      const details = [
        ["渠道", `${job.channel} · ${job.model}`],
        ["请求参数", [
          job.size,
          job.quality,
          job.output_format.toUpperCase(),
          canvasResolutionLabel,
        ].filter(Boolean).join(" · ") + transparentLabel],
        ["流程", [
          job.workflow?.creative_direction_label || "历史任务",
          job.workflow?.template_label,
          job.workflow?.generation_strategy === "explore"
            ? (job.workflow?.variant_plan?.[item.position]?.label || `探索方案 ${item.position + 1}`)
            : job.workflow?.generation_strategy === "series" ? "系列延续" : "同提示词抽样",
          stageLabel,
        ].filter(Boolean).join(" · ")],
        ["实际图片", `${item.width || "-"} × ${item.height || "-"} · ${UI.formatBytes(item.bytes)}`],
        ["耗时", item.elapsed_seconds == null ? "--" : `${item.elapsed_seconds.toFixed(1)} 秒`],
        ["费用", UI.money(item.charged_rmb)],
        ["时间", UI.dateTime(item.completed_at)],
      ];
      this.el.detailList.innerHTML = details
        .map(([label, value]) => `<div><dt>${label}</dt><dd>${UI.escapeHtml(value)}</dd></div>`)
        .join("");
      this.el.detailReferences.innerHTML = job.references.length
        ? `<span>垫图</span><div>${job.references.map((asset) => `<img src="${asset.url}" alt="${UI.escapeHtml(asset.name)}" decoding="async">`).join("")}</div>`
        : "";
      this.el.detailReferences.querySelectorAll("img").forEach((image) => this.prepareImageReveal(image));
      this.renderDetailReview(item.review || {});
      this.el.detailDownload.href = item.download_url;
      const hasSeriesContract = Boolean(
        job.workflow?.series_contract && Object.keys(job.workflow.series_contract).length,
      );
      const isCurrentAnchor = this.activeWorkspace?.settings?.series_anchor?.source_item_id === item.id;
      this.el.detailSeriesAnchor.disabled = !hasSeriesContract || isCurrentAnchor;
      this.el.detailSeriesAnchor.innerHTML = isCurrentAnchor
        ? '<i data-lucide="layers-3"></i>当前系列基准'
        : '<i data-lucide="layers-3"></i>设为系列基准';
      UI.icons(this.el.detailSeriesAnchor);
      UI.openDialog(this.el.imageDialog);
    },

    async openSliceTool() {
      const job = this.jobs.find((entry) => entry.id === this.detailJobId);
      const item = job?.items.find((entry) => entry.id === this.detailItemId);
      if (!item?.image_url || this.el.detailSlice.disabled) return;
      this.el.detailSlice.disabled = true;
      this.sliceItemId = item.id;
      this.sliceAnalysis = null;
      this.sliceBoxes = [];
      this.sliceSelected.clear();
      this.el.sliceImage.src = item.image_url;
      setText(this.el.slicePreviewTitle, "正在识别规则图集");
      setText(this.el.sliceConfidence, "分析中");
      this.el.sliceConfidence.className = "slice-confidence loading";
      this.el.sliceCanvas.classList.add("loading");
      this.renderSlices();
      UI.closeDialog(this.el.imageDialog);
      UI.openDialog(this.el.sliceDialog);
      try {
        const data = await UI.api(
          "/api/generation-items/" + item.id + "/slice-analysis",
          { method: "POST" },
        );
        if (this.sliceItemId !== item.id) return;
        this.sliceAnalysis = data.analysis;
        this.applySliceAnalysis();
      } catch (error) {
        if (this.sliceItemId !== item.id) return;
        this.el.sliceCanvas.classList.remove("loading");
        UI.closeDialog(this.el.sliceDialog);
        UI.openDialog(this.el.imageDialog);
        UI.toast(error.message, "error");
      } finally {
        this.el.detailSlice.disabled = false;
        if (this.sliceItemId === item.id) this.el.sliceCanvas.classList.remove("loading");
      }
    },

    applySliceAnalysis() {
      const analysis = this.sliceAnalysis;
      if (!analysis) return;
      const values = {
        sliceRows: analysis.rows,
        sliceColumns: analysis.columns,
      };
      Object.entries(values).forEach(([key, value]) => {
        this.el[key].value = value;
      });
      this.el.sliceCanvas.style.setProperty("--slice-ratio", analysis.width / analysis.height);
      const confidenceLabels = { high: "高置信度", medium: "中置信度", low: "低置信度" };
      const title = analysis.detected
        ? analysis.rows + " 行 × " + analysis.columns + " 列"
        : "未发现稳定的规则图集";
      setText(this.el.slicePreviewTitle, title);
      setText(this.el.sliceConfidence, confidenceLabels[analysis.confidence] || "低置信度");
      this.el.sliceConfidence.className = "slice-confidence " + (analysis.confidence || "low");
      this.rebuildSliceGrid();
      if (!analysis.detected || analysis.confidence === "low") {
        this.sliceSelected.clear();
        this.renderSlices();
      }
    },

    sliceGridValues() {
      const numeric = (element) => Number.parseInt(element.value, 10);
      const values = {
        rows: numeric(this.el.sliceRows),
        columns: numeric(this.el.sliceColumns),
      };
      const valid = Number.isInteger(values.rows)
        && Number.isInteger(values.columns)
        && values.rows >= 1 && values.rows <= 8
        && values.columns >= 1 && values.columns <= 8
        && values.rows * values.columns <= 64;
      return valid ? values : null;
    },

    rebuildSliceGrid() {
      const values = this.sliceGridValues();
      const analysis = this.sliceAnalysis;
      if (!values || !analysis) {
        this.sliceBoxes = [];
        this.sliceSelected.clear();
        this.renderSlices();
        return;
      }
      if (analysis.width < values.columns * 4 || analysis.height < values.rows * 4) {
        this.sliceBoxes = [];
        this.sliceSelected.clear();
        this.renderSlices();
        return;
      }
      const xEdges = Array.from({ length: values.columns + 1 }, (_value, index) => (
        Math.floor(analysis.width * index / values.columns + 0.5)
      ));
      const yEdges = Array.from({ length: values.rows + 1 }, (_value, index) => (
        Math.floor(analysis.height * index / values.rows + 0.5)
      ));
      this.sliceBoxes = [];
      for (let row = 0; row < values.rows; row += 1) {
        for (let column = 0; column < values.columns; column += 1) {
          this.sliceBoxes.push({
            row,
            column,
            x: xEdges[column],
            y: yEdges[row],
            width: xEdges[column + 1] - xEdges[column],
            height: yEdges[row + 1] - yEdges[row],
          });
        }
      }
      this.sliceSelected = new Set(this.sliceBoxes.map((_box, index) => index));
      this.renderSlices();
    },

    renderSlices() {
      const analysis = this.sliceAnalysis;
      const imageUrl = this.el.sliceImage.src;
      this.el.sliceOverlay.replaceChildren(...this.sliceBoxes.map((box, index) => {
        const button = document.createElement("button");
        button.type = "button";
        button.className = "slice-box" + (this.sliceSelected.has(index) ? " selected" : "");
        button.dataset.sliceIndex = index;
        button.setAttribute("aria-pressed", this.sliceSelected.has(index) ? "true" : "false");
        button.setAttribute(
          "aria-label",
          "切片 " + (index + 1) + "，" + box.width + " × " + box.height,
        );
        if (analysis) {
          button.style.left = (box.x / analysis.width * 100) + "%";
          button.style.top = (box.y / analysis.height * 100) + "%";
          button.style.width = (box.width / analysis.width * 100) + "%";
          button.style.height = (box.height / analysis.height * 100) + "%";
        }
        const label = document.createElement("span");
        label.textContent = index + 1;
        button.append(label);
        return button;
      }));
      this.el.sliceList.replaceChildren(...this.sliceBoxes.map((box, index) => {
        const button = document.createElement("button");
        button.type = "button";
        button.className = "slice-list-item" + (this.sliceSelected.has(index) ? " selected" : "");
        button.dataset.sliceIndex = index;
        button.setAttribute("aria-pressed", this.sliceSelected.has(index) ? "true" : "false");
        const preview = document.createElement("span");
        preview.className = "slice-thumb";
        preview.style.aspectRatio = box.width + " / " + box.height;
        if (analysis && imageUrl) {
          preview.style.backgroundImage = "url(" + JSON.stringify(imageUrl) + ")";
          preview.style.backgroundSize = (analysis.width / box.width * 100) + "% "
            + (analysis.height / box.height * 100) + "%";
          const backgroundX = analysis.width === box.width
            ? 0 : box.x / (analysis.width - box.width) * 100;
          const backgroundY = analysis.height === box.height
            ? 0 : box.y / (analysis.height - box.height) * 100;
          preview.style.backgroundPosition = backgroundX + "% " + backgroundY + "%";
        }
        const copy = document.createElement("span");
        const name = document.createElement("strong");
        name.textContent = "#" + String(index + 1).padStart(2, "0");
        const size = document.createElement("small");
        size.textContent = box.width + " × " + box.height;
        copy.append(name, size);
        button.append(preview, copy);
        return button;
      }));
      const selected = this.sliceSelected.size;
      setText(
        this.el.sliceSelectionSummary,
        this.sliceBoxes.length
          ? "已选择 " + selected + " / " + this.sliceBoxes.length + " 个切片"
          : "布局参数无效",
      );
      setDisabled(this.el.sliceDownload, !selected || this.sliceBusy);
      setDisabled(this.el.sliceSaveLibrary, !selected || this.sliceBusy);
      setDisabled(this.el.sliceReuse, selected !== 1 || this.sliceBusy);
    },

    handleSliceSelection(event) {
      const button = event.target.closest("[data-slice-index]");
      if (!button || this.sliceBusy) return;
      const index = Number.parseInt(button.dataset.sliceIndex, 10);
      if (this.sliceSelected.has(index)) this.sliceSelected.delete(index);
      else this.sliceSelected.add(index);
      this.renderSlices();
    },

    selectedSliceBoxes() {
      return [...this.sliceSelected]
        .sort((left, right) => left - right)
        .map((index) => this.sliceBoxes[index])
        .filter(Boolean)
        .map(({ x, y, width, height }) => ({ x, y, width, height }));
    },

    async exportSlices(action) {
      const boxes = this.selectedSliceBoxes();
      if (!this.sliceItemId || !boxes.length || this.sliceBusy) return;
      if (action === "reference" && boxes.length !== 1) return;
      const workspace = this.activeWorkspace;
      this.sliceBusy = true;
      this.renderSlices();
      try {
        if (action === "download") {
          await this.downloadSlices(boxes);
          UI.toast("已导出 " + boxes.length + " 个切片", "success");
          return;
        }
        const data = await UI.api(
          "/api/generation-items/" + this.sliceItemId + "/slice-export",
          { method: "POST", body: { action, boxes } },
        );
        if (action === "library") {
          this.mergeLibraryImages(data.images || [], data.added_count);
          UI.toast("已将 " + boxes.length + " 个切片存入图库", "success");
          return;
        }
        if (data.asset) await this.applySliceReference(data.asset, workspace);
      } catch (error) {
        UI.toast(error.message, "error");
      } finally {
        this.sliceBusy = false;
        this.renderSlices();
      }
    },

    async applyReferenceAsset(
      asset,
      {
        workspace = this.activeWorkspace,
        dialog,
        prompt = "请基于这张图继续调整：",
        imageToast = "",
      },
    ) {
      if (!workspace || !asset) return;
      if (!workspace.assets.some((entry) => entry.id === asset.id)) {
        workspace.assets.push(asset);
      }
      this.renderWorkspaceList();
      if (this.activeWorkspace?.id !== workspace.id) return;
      const chatSelection = this.currentChatSelection(workspace.id);
      chatSelection.clear();
      chatSelection.add(asset.id);
      const generationSelection = this.currentSelection(workspace.id);
      generationSelection.clear();
      generationSelection.add(asset.id);
      this.setMode("img2img", false);
      this.chatReferencePickerOpen = true;
      this.renderChatReferences();
      this.renderReferences();
      this.settingChanged();
      this.setComposerMode("chat");
      this.el.chatInput.value = prompt;
      UI.closeDialog(dialog);
      this.el.chatInput.focus();
      if (imageToast) UI.toast(imageToast, "success");
    },

    async applySliceReference(asset, workspace = this.activeWorkspace) {
      await this.applyReferenceAsset(asset, {
        workspace,
        dialog: this.el.sliceDialog,
        prompt: "请基于这个切片继续调整：",
        imageToast: "已选择切片，可以继续调整",
      });
    },

    async downloadSlices(boxes) {
      const headers = new Headers({
        Accept: "application/zip",
        "Content-Type": "application/json",
      });
      const csrfToken = document.querySelector('meta[name="csrf-token"]')?.content || "";
      if (csrfToken) headers.set("X-CSRFToken", csrfToken);
      const response = await fetch(
        "/api/generation-items/" + this.sliceItemId + "/slice-export",
        {
          method: "POST",
          credentials: "same-origin",
          headers,
          body: JSON.stringify({ action: "download", boxes }),
        },
      );
      if (!response.ok) {
        const payload = (response.headers.get("content-type") || "").includes("application/json")
          ? await response.json() : null;
        throw new Error(payload?.error || "导出失败（HTTP " + response.status + "）");
      }
      const url = URL.createObjectURL(await response.blob());
      const link = document.createElement("a");
      link.href = url;
      link.download = "image_" + this.sliceItemId + "_slices.zip";
      document.body.append(link);
      link.click();
      link.remove();
      URL.revokeObjectURL(url);
    },

    async startUiKitReconstruction() {
      if (!this.detailItemId || !this.activeWorkspace) return;
      const channel = this.currentChannel();
      if (!channel?.capabilities.modes.includes("img2img")) {
        UI.toast("当前渠道不支持参考图生成", "error");
        return;
      }
      if (!channel.capabilities.formats.includes("png")) {
        UI.toast("当前渠道不支持 UI Kit 所需的透明 PNG", "error");
        return;
      }
      await this.useDetailAsReference({
        prepare: () => {
          this.activeWorkspace.settings.prompt_draft_id = "";
          this.el.creativeDirectionSelect.value = "game_ui";
          const squareSize = channel.capabilities.sizes
            .map((value) => this.normalizeSize(value))
            .find((value) => {
              const [width, height] = value.split("x").map(Number);
              return width === height;
            });
          if (squareSize) this.el.sizeInput.value = squareSize;
          this.el.formatSelect.value = "png";
          this.updateTransparentBackgroundState();
          this.el.transparentBackground.checked = true;
          this.el.batchCount.value = "1";
          this.updatePrice();
        },
        prompt: UI_KIT_RECONSTRUCTION_PROMPT,
        imageToast: "已进入开发 UI Kit 重建流程",
      });
    },

    renderDetailReview(review) {
      const verdict = review?.verdict || "";
      const hasReview = ["pass", "revise"].includes(verdict);
      this.detailReviewSuggestion = review?.suggested_edit || "";
      setHidden(this.el.detailReview, !hasReview);
      this.el.detailReview.classList.toggle("is-pass", verdict === "pass");
      this.el.detailReview.classList.toggle("is-revise", verdict === "revise");
      setText(this.el.detailReviewVerdict, verdict === "pass" ? "通过" : "需要精修");
      const scores = review?.scores || {};
      this.el.detailReviewScores.innerHTML = hasReview
        ? [
          ["构图", scores.composition],
          ["画质", scores.visual_quality],
          ["可用", scores.usability],
        ].map(([label, value]) => (
          `<span>${label}<strong>${Number(value || 0).toFixed(1)}</strong></span>`
        )).join("")
        : "";
      const checks = [...(review?.hard_checks || [])];
      (review?.findings || []).forEach((finding, index) => {
        checks.push({ id: `finding_${index}`, label: finding, passed: false, evidence: "" });
      });
      this.el.detailReviewChecks.replaceChildren(...checks.map((check) => {
        const item = document.createElement("li");
        item.classList.toggle("passed", check.passed === true);
        item.textContent = check.evidence ? `${check.label}：${check.evidence}` : check.label;
        return item;
      }));
      setHidden(this.el.detailReviewSuggestion, !this.detailReviewSuggestion);
      setText(this.el.detailReviewSuggestion, this.detailReviewSuggestion);
      setHidden(this.el.detailApplyReview, !this.detailReviewSuggestion);
      setDisabled(
        this.el.detailApplyReview,
        !this.detailReviewSuggestion || this.detailReviewBusy || this.detailReferenceBusy,
      );
      this.el.detailRunReview.innerHTML = hasReview
        ? '<i data-lucide="refresh-cw"></i>重新验收'
        : '<i data-lucide="scan-search"></i>AI 验收';
      UI.icons(this.el.detailRunReview);
    },

    async runDetailReview() {
      const job = this.jobs.find((entry) => entry.id === this.detailJobId);
      const item = job?.items.find((entry) => entry.id === this.detailItemId);
      const modelId = this.el.chatModelSelect.value;
      if (!item || !modelId || this.detailReviewBusy) return;
      const itemId = item.id;
      this.detailReviewBusy = true;
      setDisabled(this.el.detailRunReview, true);
      setDisabled(this.el.detailApplyReview, true);
      this.el.detailRunReview.innerHTML = '<i data-lucide="loader-circle"></i>正在验收';
      UI.icons(this.el.detailRunReview);
      try {
        const data = await UI.api(`/api/generation-items/${itemId}/review`, {
          method: "POST",
          body: { model_id: modelId },
        });
        item.review = data.review;
        if (this.detailItemId === itemId) this.renderDetailReview(data.review);
        UI.toast(data.review.verdict === "pass" ? "AI 验收通过" : "AI 已给出精修建议", "success");
      } catch (error) {
        UI.toast(error.message, "error");
      } finally {
        this.detailReviewBusy = false;
        setDisabled(this.el.detailRunReview, false);
        if (this.detailItemId === itemId) this.renderDetailReview(item.review || {});
      }
    },

    async applyDetailReview() {
      if (!this.detailReviewSuggestion || this.detailReviewBusy) return;
      await this.useDetailAsReference({
        prompt: this.detailReviewSuggestion,
        imageToast: "已载入验收建议，可以继续精修",
      });
    },

    async setDetailAsSeriesAnchor() {
      if (!this.detailItemId || !this.activeWorkspace || this.detailReferenceBusy) return;
      const workspace = this.activeWorkspace;
      this.detailReferenceBusy = true;
      setDisabled(this.el.detailSeriesAnchor, true);
      try {
        const data = await UI.api(`/api/generation-items/${this.detailItemId}/series-anchor`, {
          method: "POST",
        });
        const target = this.workspaces.find((item) => item.id === workspace.id);
        if (target && data.workspace) Object.assign(target, data.workspace);
        if (this.activeWorkspace?.id === workspace.id && data.workspace) {
          Object.assign(this.activeWorkspace, data.workspace);
          this.setGenerationStrategy("series", false);
        }
        await this.applyReferenceAsset(data.asset, {
          workspace: this.activeWorkspace,
          dialog: this.el.imageDialog,
          prompt: "请描述这个系列下一张图片需要改变的内容：",
          imageToast: "已设为系列基准，可以继续创作",
        });
      } catch (error) {
        UI.toast(error.message, "error");
      } finally {
        this.detailReferenceBusy = false;
        setDisabled(this.el.detailSeriesAnchor, false);
      }
    },

    async useDetailAsReference({ prepare, prompt, imageToast } = {}) {
      if (!this.detailItemId || !this.activeWorkspace || this.detailReferenceBusy) return;
      const itemId = this.detailItemId;
      const workspace = this.activeWorkspace;
      this.detailReferenceBusy = true;
      setDisabled(this.el.detailReuse, true);
      setDisabled(this.el.detailUiKit, true);
      setDisabled(this.el.detailSeriesAnchor, true);
      setDisabled(this.el.detailApplyReview, true);
      try {
        const data = await UI.api(`/api/generation-items/${itemId}/reference`, {
          method: "POST",
        });
        if (this.activeWorkspace?.id === workspace.id) prepare?.();
        await this.applyReferenceAsset(data.asset, {
          workspace,
          dialog: this.el.imageDialog,
          prompt,
          imageToast,
        });
      } catch (error) {
        UI.toast(error.message, "error");
      } finally {
        this.detailReferenceBusy = false;
        setDisabled(this.el.detailReuse, false);
        setDisabled(this.el.detailUiKit, false);
        setDisabled(this.el.detailSeriesAnchor, false);
        setDisabled(this.el.detailApplyReview, !this.detailReviewSuggestion);
      }
    },

  });
})();
