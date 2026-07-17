(() => {
  "use strict";

  const {
    StudioApp,
    UI,
    STATUS,
    setText,
    setHidden,
    setDisabled,
    setAttribute,
  } = window.ImageGenStudio;

  Object.assign(StudioApp.prototype, {
    showDetail(job, item) {
      this.detailItemId = item.id;
      this.detailJobId = job.id;
      this.el.detailImage.src = item.image_url;
      this.el.detailImage.closest(".image-dialog-preview")
        ?.classList.toggle("has-transparency", job.transparent_background === true);
      this.prepareImageReveal(this.el.detailImage);
      this.el.detailPrompt.textContent = job.prompt;
      const transparentLabel = job.transparent_background ? " · 透明背景" : "";
      const animationLabel = job.kind === "animation"
        ? ` · 第 ${item.position + 1}/${job.requested_count} 帧 · ${job.animation_fps} FPS`
        : "";
      const stageLabel = { draft: "草稿", refine: "精修", final: "成品" }[
        job.workflow?.generation_stage
      ] || "未标记";
      const details = [
        ["渠道", `${job.channel} · ${job.model}`],
        ["参数", `${job.size} · ${job.quality} · ${job.output_format.toUpperCase()}${transparentLabel}${animationLabel}`],
        ["流程", [
          job.workflow?.creative_direction_label || "历史任务",
          job.workflow?.template_label,
          stageLabel,
        ].filter(Boolean).join(" · ")],
        ["图片", `${item.width || "-"} × ${item.height || "-"} · ${UI.formatBytes(item.bytes)}`],
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
      this.el.detailReuseLabel.textContent = this.isAnimationWorkspace() ? "设为母图" : "基于此图继续";
      this.el.detailDownload.href = item.download_url;
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
      setText(this.el.slicePreviewTitle, "正在识别图集");
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
        sliceMarginX: analysis.margin_x,
        sliceMarginY: analysis.margin_y,
        sliceGapX: analysis.gap_x,
        sliceGapY: analysis.gap_y,
      };
      Object.entries(values).forEach(([key, value]) => {
        this.el[key].value = value;
      });
      this.el.sliceMarginX.max = Math.max(0, Math.floor(analysis.width / 2) - 4);
      this.el.sliceMarginY.max = Math.max(0, Math.floor(analysis.height / 2) - 4);
      this.el.sliceGapX.max = Math.max(0, analysis.width - 8);
      this.el.sliceGapY.max = Math.max(0, analysis.height - 8);
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
        marginX: numeric(this.el.sliceMarginX),
        marginY: numeric(this.el.sliceMarginY),
        gapX: numeric(this.el.sliceGapX),
        gapY: numeric(this.el.sliceGapY),
      };
      const valid = Number.isInteger(values.rows)
        && Number.isInteger(values.columns)
        && values.rows >= 1 && values.rows <= 8
        && values.columns >= 1 && values.columns <= 8
        && values.rows * values.columns <= 64
        && [values.marginX, values.marginY, values.gapX, values.gapY]
          .every((value) => Number.isInteger(value) && value >= 0);
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
      const usableWidth = analysis.width - values.marginX * 2
        - values.gapX * (values.columns - 1);
      const usableHeight = analysis.height - values.marginY * 2
        - values.gapY * (values.rows - 1);
      if (usableWidth < values.columns * 4 || usableHeight < values.rows * 4) {
        this.sliceBoxes = [];
        this.sliceSelected.clear();
        this.renderSlices();
        return;
      }
      const xEdges = Array.from({ length: values.columns + 1 }, (_value, index) => (
        values.marginX + Math.round(usableWidth * index / values.columns)
      ));
      const yEdges = Array.from({ length: values.rows + 1 }, (_value, index) => (
        values.marginY + Math.round(usableHeight * index / values.rows)
      ));
      this.sliceBoxes = [];
      for (let row = 0; row < values.rows; row += 1) {
        for (let column = 0; column < values.columns; column += 1) {
          this.sliceBoxes.push({
            row,
            column,
            x: xEdges[column] + column * values.gapX,
            y: yEdges[row] + row * values.gapY,
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
        if (data.asset) await this.applySliceReference(data.asset);
      } catch (error) {
        UI.toast(error.message, "error");
      } finally {
        this.sliceBusy = false;
        this.renderSlices();
      }
    },

    async applySliceReference(asset) {
      const workspace = this.activeWorkspace;
      if (!workspace) return;
      if (!workspace.assets.some((entry) => entry.id === asset.id)) {
        workspace.assets.push(asset);
      }
      this.renderWorkspaceList();
      if (workspace.kind === "animation") {
        const selection = this.currentSelection(workspace.id);
        selection.clear();
        selection.add(asset.id);
        this.setMode("img2img", false);
        this.renderReferences();
        this.settingChanged();
        await this.flushSettings();
        UI.closeDialog(this.el.sliceDialog);
        this.setComposerMode("generation");
        this.el.promptInput.focus();
        UI.toast("已将切片设为母图", "success");
        return;
      }
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
      this.el.chatInput.value = "请基于这个切片继续调整：";
      UI.closeDialog(this.el.sliceDialog);
      this.el.chatInput.focus();
      UI.toast("已选择切片，可以继续调整", "success");
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

    renderDetailReview(review) {
      const verdict = review?.verdict || "";
      this.detailReviewSuggestion = review?.suggested_edit || "";
      this.el.detailReview.classList.toggle("is-pass", verdict === "pass");
      this.el.detailReview.classList.toggle("is-revise", verdict === "revise");
      setText(
        this.el.detailReviewVerdict,
        verdict === "pass" ? "通过" : verdict === "revise" ? "需要精修" : "尚未验收",
      );
      const scores = review?.scores || {};
      const hasReview = ["pass", "revise"].includes(verdict);
      setHidden(this.el.detailReviewScores, !hasReview);
      if (hasReview) {
        this.el.detailReviewScores.innerHTML = [
          ["构图", scores.composition],
          ["画质", scores.visual_quality],
          ["可用", scores.usability],
        ].map(([label, value]) => (
          `<span>${label}<strong>${Number(value || 0).toFixed(1)}</strong></span>`
        )).join("");
      } else {
        this.el.detailReviewScores.replaceChildren();
      }
      const checks = [...(review?.hard_checks || [])];
      (review?.findings || []).forEach((finding, index) => {
        checks.push({ id: `finding_${index}`, label: finding, passed: false, evidence: "" });
      });
      setHidden(this.el.detailReviewChecks, !checks.length);
      this.el.detailReviewChecks.replaceChildren(...checks.map((check) => {
        const item = document.createElement("li");
        item.classList.toggle("passed", check.passed === true);
        item.textContent = check.evidence ? `${check.label}：${check.evidence}` : check.label;
        return item;
      }));
      setHidden(this.el.detailReviewSuggestion, !this.detailReviewSuggestion);
      setText(this.el.detailReviewSuggestion, this.detailReviewSuggestion);
      setHidden(this.el.detailApplyReview, !this.detailReviewSuggestion);
      this.el.detailRunReview.innerHTML = hasReview
        ? '<i data-lucide="refresh-cw"></i>重新验收'
        : '<i data-lucide="scan-search"></i>AI 验收';
      UI.icons(this.el.detailRunReview);
    },

    async runDetailReview() {
      const job = this.jobs.find((entry) => entry.id === this.detailJobId);
      const item = job?.items.find((entry) => entry.id === this.detailItemId);
      const modelId = this.el.chatModelSelect.value;
      if (!item || !modelId || this.el.detailRunReview.disabled) return;
      this.el.detailRunReview.disabled = true;
      this.el.detailRunReview.innerHTML = '<i data-lucide="loader-circle"></i>正在验收';
      UI.icons(this.el.detailRunReview);
      try {
        const data = await UI.api(`/api/generation-items/${item.id}/review`, {
          method: "POST",
          body: { model_id: modelId },
        });
        item.review = data.review;
        this.renderDetailReview(data.review);
        UI.toast(data.review.verdict === "pass" ? "AI 验收通过" : "AI 已给出精修建议", "success");
      } catch (error) {
        UI.toast(error.message, "error");
        this.renderDetailReview(item.review || {});
      } finally {
        this.el.detailRunReview.disabled = false;
      }
    },

    async reuseDetailImage(suggestedText = "") {
      if (!this.detailItemId || !this.activeWorkspace) return;
      const workspace = this.activeWorkspace;
      const itemId = this.detailItemId;
      this.el.detailReuse.disabled = true;
      try {
        const data = await UI.api(`/api/generation-items/${itemId}/reference`, {
          method: "POST",
        });
        if (!workspace.assets.some((asset) => asset.id === data.asset.id)) {
          workspace.assets.push(data.asset);
        }
        this.renderWorkspaceList();
        if (workspace.kind === "animation" && this.activeWorkspace?.id === workspace.id) {
          const selection = this.currentSelection(workspace.id);
          selection.clear();
          selection.add(data.asset.id);
          this.setMode("img2img", false);
          this.renderReferences();
          this.settingChanged();
          await this.flushSettings();
          UI.closeDialog(this.el.imageDialog);
          this.setComposerMode("generation");
          this.el.promptInput.focus();
          UI.toast("已设为母图，可以调整帧动画参数", "success");
        } else if (this.activeWorkspace?.id === workspace.id) {
          const chatSelection = this.currentChatSelection(workspace.id);
          chatSelection.clear();
          chatSelection.add(data.asset.id);
          const generationSelection = this.currentSelection(workspace.id);
          generationSelection.clear();
          generationSelection.add(data.asset.id);
          this.setMode("img2img", false);
          this.chatReferencePickerOpen = true;
          this.renderChatReferences();
          this.renderReferences();
          this.settingChanged();
          this.setComposerMode("chat");
          this.el.chatInput.value = suggestedText || "请基于这张图继续调整：";
          UI.closeDialog(this.el.imageDialog);
          this.el.chatInput.focus();
        }
      } catch (error) {
        UI.toast(error.message, "error");
      } finally {
        this.el.detailReuse.disabled = false;
      }
    },

  });
})();
