(() => {
  "use strict";

  const { StudioApp, UI, setDisabled, setHidden } = window.ImageGenStudio;

  const MAX_EDITOR_SIDE = 1280;
  const MASK_OVERLAY = "rgb(239 79 79)";

  class MaskEditor {
    constructor(canvas, onStateChange) {
      this.canvas = canvas;
      this.context = canvas.getContext("2d", { alpha: false });
      this.selectionCanvas = document.createElement("canvas");
      this.selectionContext = this.selectionCanvas.getContext("2d");
      this.onStateChange = onStateChange;
      this.image = null;
      this.sourceId = "";
      this.operations = [];
      this.activeOperation = null;
      this.tool = "rectangle";
      this.brushRatio = 0.06;
      this.coverage = 0;
      this.pointerId = null;
      this.bindPointerEvents();
    }

    bindPointerEvents() {
      this.canvas.addEventListener("pointerdown", (event) => this.pointerDown(event));
      this.canvas.addEventListener("pointermove", (event) => this.pointerMove(event));
      this.canvas.addEventListener("pointerup", (event) => this.pointerUp(event));
      this.canvas.addEventListener("pointercancel", (event) => this.pointerCancel(event));
    }

    async load({ id, url }, { preserve = false } = {}) {
      const keepOperations = preserve && this.sourceId === id;
      const image = new Image();
      image.decoding = "async";
      image.src = url;
      await new Promise((resolve, reject) => {
        image.addEventListener("load", resolve, { once: true });
        image.addEventListener("error", () => reject(new Error("无法加载局部重绘原图")), {
          once: true,
        });
      });
      const scale = Math.min(1, MAX_EDITOR_SIDE / Math.max(image.naturalWidth, image.naturalHeight));
      const width = Math.max(1, Math.round(image.naturalWidth * scale));
      const height = Math.max(1, Math.round(image.naturalHeight * scale));
      this.canvas.width = width;
      this.canvas.height = height;
      this.selectionCanvas.width = width;
      this.selectionCanvas.height = height;
      this.image = image;
      this.sourceId = id;
      this.operations = keepOperations ? this.operations : [];
      this.activeOperation = null;
      this.render();
      this.updateState();
    }

    setTool(tool) {
      if (!["rectangle", "brush", "eraser"].includes(tool)) return;
      this.tool = tool;
      this.canvas.classList.toggle("is-brush", tool !== "rectangle");
    }

    setBrushRatio(value) {
      this.brushRatio = Math.min(0.2, Math.max(0.01, Number(value) || 0.06));
    }

    undo() {
      if (!this.operations.length) return;
      this.operations.pop();
      this.render();
      this.updateState();
    }

    clear() {
      this.operations = [];
      this.activeOperation = null;
      this.render();
      this.updateState();
    }

    pointerDown(event) {
      if (!this.image || this.pointerId !== null || event.button !== 0) return;
      event.preventDefault();
      const point = this.normalizedPoint(event);
      this.pointerId = event.pointerId;
      this.canvas.setPointerCapture(event.pointerId);
      this.activeOperation = this.tool === "rectangle"
        ? { type: "rectangle", start: point, end: point, replace: true }
        : {
          type: "path",
          mode: this.tool === "eraser" ? "erase" : "add",
          size: this.brushRatio,
          points: [point],
        };
      this.render();
    }

    pointerMove(event) {
      if (event.pointerId !== this.pointerId || !this.activeOperation) return;
      event.preventDefault();
      const point = this.normalizedPoint(event);
      if (this.activeOperation.type === "rectangle") {
        this.activeOperation.end = point;
      } else {
        const previous = this.activeOperation.points.at(-1);
        if (!previous || Math.hypot(point.x - previous.x, point.y - previous.y) > 0.001) {
          this.activeOperation.points.push(point);
        }
      }
      this.render();
    }

    pointerUp(event) {
      if (event.pointerId !== this.pointerId || !this.activeOperation) return;
      event.preventDefault();
      const operation = this.activeOperation;
      if (operation.type !== "rectangle" || this.rectangleHasArea(operation)) {
        this.operations.push(operation);
      }
      this.releasePointer(event.pointerId);
      this.render();
      this.updateState();
    }

    pointerCancel(event) {
      if (event.pointerId !== this.pointerId) return;
      this.activeOperation = null;
      this.releasePointer(event.pointerId);
      this.render();
    }

    releasePointer(pointerId) {
      if (this.canvas.hasPointerCapture(pointerId)) this.canvas.releasePointerCapture(pointerId);
      this.pointerId = null;
      this.activeOperation = null;
    }

    normalizedPoint(event) {
      const bounds = this.canvas.getBoundingClientRect();
      return {
        x: Math.min(1, Math.max(0, (event.clientX - bounds.left) / bounds.width)),
        y: Math.min(1, Math.max(0, (event.clientY - bounds.top) / bounds.height)),
      };
    }

    rectangleHasArea(operation) {
      return Math.abs(operation.end.x - operation.start.x) * this.canvas.width >= 2
        && Math.abs(operation.end.y - operation.start.y) * this.canvas.height >= 2;
    }

    render() {
      if (!this.image) return;
      this.renderSelection();
      this.context.clearRect(0, 0, this.canvas.width, this.canvas.height);
      this.context.drawImage(this.image, 0, 0, this.canvas.width, this.canvas.height);
      this.context.save();
      this.context.globalAlpha = 0.46;
      this.context.drawImage(this.selectionCanvas, 0, 0);
      this.context.restore();
    }

    renderSelection() {
      const context = this.selectionContext;
      context.clearRect(0, 0, this.selectionCanvas.width, this.selectionCanvas.height);
      [...this.operations, ...(this.activeOperation ? [this.activeOperation] : [])]
        .forEach((operation) => this.drawOperation(context, operation));
    }

    drawOperation(context, operation) {
      const width = this.selectionCanvas.width;
      const height = this.selectionCanvas.height;
      context.save();
      if (operation.type === "rectangle") {
        if (operation.replace) context.clearRect(0, 0, width, height);
        const left = Math.min(operation.start.x, operation.end.x) * width;
        const top = Math.min(operation.start.y, operation.end.y) * height;
        const rectangleWidth = Math.abs(operation.end.x - operation.start.x) * width;
        const rectangleHeight = Math.abs(operation.end.y - operation.start.y) * height;
        context.fillStyle = MASK_OVERLAY;
        context.fillRect(left, top, rectangleWidth, rectangleHeight);
        context.restore();
        return;
      }

      context.globalCompositeOperation = operation.mode === "erase"
        ? "destination-out"
        : "source-over";
      context.strokeStyle = MASK_OVERLAY;
      context.fillStyle = MASK_OVERLAY;
      context.lineWidth = operation.size * Math.min(width, height);
      context.lineCap = "round";
      context.lineJoin = "round";
      const points = operation.points;
      if (points.length === 1) {
        context.beginPath();
        context.arc(
          points[0].x * width,
          points[0].y * height,
          context.lineWidth / 2,
          0,
          Math.PI * 2,
        );
        context.fill();
      } else {
        context.beginPath();
        context.moveTo(points[0].x * width, points[0].y * height);
        points.slice(1).forEach((point) => context.lineTo(point.x * width, point.y * height));
        context.stroke();
      }
      context.restore();
    }

    updateState() {
      this.renderSelection();
      const pixels = this.selectionContext.getImageData(
        0,
        0,
        this.selectionCanvas.width,
        this.selectionCanvas.height,
      ).data;
      let selected = 0;
      for (let index = 3; index < pixels.length; index += 4) {
        if (pixels[index] >= 128) selected += 1;
      }
      this.coverage = selected / (this.selectionCanvas.width * this.selectionCanvas.height);
      this.onStateChange?.({
        hasSelection: selected > 0,
        coverage: this.coverage,
        canUndo: this.operations.length > 0,
      });
    }

    async maskBlob() {
      this.renderSelection();
      if (this.coverage <= 0) throw new Error("请先框选需要修改的区域");
      const mask = document.createElement("canvas");
      mask.width = this.selectionCanvas.width;
      mask.height = this.selectionCanvas.height;
      const context = mask.getContext("2d");
      context.fillStyle = "#fff";
      context.fillRect(0, 0, mask.width, mask.height);
      context.globalCompositeOperation = "destination-out";
      context.drawImage(this.selectionCanvas, 0, 0);
      return new Promise((resolve, reject) => {
        mask.toBlob(
          (blob) => blob ? resolve(blob) : reject(new Error("无法生成 PNG 蒙版")),
          "image/png",
        );
      });
    }
  }

  Object.assign(StudioApp.prototype, {
    initializeMaskEditor() {
      this.maskEditorController = new MaskEditor(
        this.el.maskEditorCanvas,
        (state) => this.updateMaskEditorState(state),
      );
      this.el.maskToolButtons.forEach((button) => {
        button.addEventListener("click", () => {
          const tool = button.dataset.maskTool;
          this.maskEditorController.setTool(tool);
          this.el.maskToolButtons.forEach((candidate) => {
            const active = candidate === button;
            candidate.classList.toggle("active", active);
            candidate.setAttribute("aria-pressed", String(active));
          });
        });
      });
      this.el.maskBrushSize.addEventListener("input", () => {
        const value = Number(this.el.maskBrushSize.value || 6);
        this.el.maskBrushSizeValue.textContent = `${value}%`;
        this.maskEditorController.setBrushRatio(value / 100);
      });
      this.el.maskEditorUndo.addEventListener("click", () => this.maskEditorController.undo());
      this.el.maskEditorClear.addEventListener("click", () => this.maskEditorController.clear());
      this.el.maskEditorApply.addEventListener("click", () => this.applyMaskEditorSelection());
      this.el.maskEditChange.addEventListener("click", () => this.editPendingMask());
      this.el.maskEditClear.addEventListener("click", () => this.clearPendingMaskEdit());
      this.el.detailMaskEdit.addEventListener("click", () => this.openDetailMaskEditor());
      this.el.maskEditorDialog.addEventListener("close", () => this.handleMaskEditorClose());
    },

    maskCapableChannels() {
      return this.routingChannels().filter((channel) => (
        channel.capabilities?.supports_mask === true
        && channel.capabilities?.modes?.includes("img2img")
      ));
    },

    updateMaskEditorState({ hasSelection, coverage, canUndo }) {
      setDisabled(this.el.maskEditorApply, !hasSelection);
      setDisabled(this.el.maskEditorUndo, !canUndo);
      setDisabled(this.el.maskEditorClear, !hasSelection && !canUndo);
      this.el.maskEditorStatus.classList.toggle("warning", coverage >= 0.98);
      this.el.maskEditorStatus.textContent = !hasSelection
        ? "请框选需要修改的区域"
        : coverage >= 0.98
          ? "已选择接近整张图片，生成结果可能相当于整图重绘"
          : `已选择约 ${Math.max(1, Math.round(coverage * 100))}% 的画面`;
    },

    async openMaskEditorForAsset(asset, { returnDialog = null, preserve = false } = {}) {
      if (!asset || !this.activeWorkspace) return;
      if (!this.maskCapableChannels().length) {
        UI.toast("暂无已启用局部重绘蒙版能力的生图渠道", "error");
        return;
      }
      this.maskEditorSource = {
        asset,
        workspaceId: this.activeWorkspace.id,
      };
      this.maskEditorReturnDialog = returnDialog;
      this.maskEditorCommitted = false;
      if (returnDialog) UI.closeDialog(returnDialog);
      setHidden(this.el.maskEditorLoading, false);
      setDisabled(this.el.maskEditorApply, true);
      UI.openDialog(this.el.maskEditorDialog);
      try {
        await this.maskEditorController.load(
          { id: asset.id, url: asset.url },
          { preserve },
        );
      } catch (error) {
        UI.closeDialog(this.el.maskEditorDialog);
        UI.toast(error.message, "error");
      } finally {
        setHidden(this.el.maskEditorLoading, true);
      }
    },

    handleMaskEditorClose() {
      const returnDialog = this.maskEditorReturnDialog;
      const shouldReturn = !this.maskEditorCommitted && returnDialog;
      this.maskEditorReturnDialog = null;
      this.maskEditorCommitted = false;
      if (shouldReturn) UI.openDialog(returnDialog);
    },

    ensureMaskCapableChannel() {
      const current = this.currentChannel();
      if (
        current?.capabilities?.supports_mask === true
        && current?.capabilities?.modes?.includes("img2img")
      ) return current;
      const fallback = this.maskCapableChannels()[0];
      if (!fallback) return null;
      this.el.channelSelect.value = fallback.id;
      this.applyChannel(null, true);
      return fallback;
    },

    async applyMaskEditorSelection() {
      const source = this.maskEditorSource;
      const workspace = this.activeWorkspace;
      if (!source || !workspace || source.workspaceId !== workspace.id) return;
      setDisabled(this.el.maskEditorApply, true);
      try {
        const channel = this.ensureMaskCapableChannel();
        if (!channel) throw new Error("暂无已启用局部重绘蒙版能力的生图渠道");
        const blob = await this.maskEditorController.maskBlob();
        if (!workspace.assets.some((asset) => asset.id === source.asset.id)) {
          workspace.assets.push(source.asset);
        }
        if (this.pendingMaskEdit?.previewUrl) {
          URL.revokeObjectURL(this.pendingMaskEdit.previewUrl);
        }
        this.pendingMaskEdit = {
          workspaceId: workspace.id,
          assetId: source.asset.id,
          asset: source.asset,
          blob,
          coverage: this.maskEditorController.coverage,
          previewUrl: URL.createObjectURL(blob),
        };
        workspace.settings.prompt_draft_id = "";
        this.setGenerationStrategy("sample", false);
        this.el.promptInput.value = "";
        this.el.promptInput.placeholder = "描述框选区域要变成什么，并写明必须保持的内容...";
        this.updatePromptCounter();
        const selection = this.currentSelection(workspace.id);
        selection.clear();
        selection.add(source.asset.id);
        this.maskEditorCommitted = true;
        UI.closeDialog(this.el.maskEditorDialog);
        this.showGenerationComposer("", [source.asset.id]);
        this.renderMaskEditNotice();
        UI.toast("已自动生成蒙版，当前图片已设为唯一垫图", "success");
      } catch (error) {
        setDisabled(this.el.maskEditorApply, false);
        UI.toast(error.message, "error");
      }
    },

    renderMaskEditNotice() {
      const edit = this.pendingMaskEdit;
      const visible = Boolean(edit && edit.workspaceId === this.activeWorkspace?.id);
      setHidden(this.el.maskEditNotice, !visible);
      setHidden(this.el.maskEditPreview, !visible);
      if (!visible) {
        [
          "imagePreviewSrc",
          "imagePreviewMaskUrl",
          "imagePreviewMaskSource",
          "imagePreviewMaskLabel",
          "imagePreviewMaskInitial",
        ].forEach((key) => delete this.el.maskEditPreview.dataset[key]);
        return;
      }
      const coverage = Math.max(1, Math.round(edit.coverage * 100));
      this.el.maskEditNoticeLabel.textContent = `局部重绘 · ${edit.asset.name} · 约 ${coverage}% 区域`;
      this.el.maskEditPreview.dataset.imagePreviewSrc = edit.asset.url;
      this.el.maskEditPreview.dataset.imagePreviewMaskUrl = edit.previewUrl;
      this.el.maskEditPreview.dataset.imagePreviewMaskSource = edit.asset.url;
      this.el.maskEditPreview.dataset.imagePreviewMaskLabel = "局部重绘区域";
      this.el.maskEditPreview.dataset.imagePreviewMaskInitial = "true";
      UI.icons(this.el.maskEditNotice);
    },

    clearPendingMaskEdit({ silent = false } = {}) {
      if (!this.pendingMaskEdit) return;
      if (this.pendingMaskEdit.previewUrl) URL.revokeObjectURL(this.pendingMaskEdit.previewUrl);
      this.pendingMaskEdit = null;
      this.el.promptInput.placeholder = "输入画面描述...";
      this.renderMaskEditNotice();
      if (!silent) UI.toast("已退出局部重绘，当前图片仍保留为垫图", "info");
    },

    editPendingMask() {
      const edit = this.pendingMaskEdit;
      if (!edit || edit.workspaceId !== this.activeWorkspace?.id) return;
      this.openMaskEditorForAsset(edit.asset, { preserve: true });
    },

    invalidatePendingMaskEdit(message = "垫图已变化，局部重绘蒙版已清除") {
      if (!this.pendingMaskEdit) return;
      this.clearPendingMaskEdit({ silent: true });
      UI.toast(message, "info");
    },

    async openDetailMaskEditor() {
      if (!this.detailItemId || !this.activeWorkspace || this.detailReferenceBusy) return;
      const itemId = this.detailItemId;
      const workspace = this.activeWorkspace;
      this.detailReferenceBusy = true;
      setDisabled(this.el.detailMaskEdit, true);
      setDisabled(this.el.detailReuse, true);
      try {
        const data = await UI.api(`/api/generation-items/${itemId}/reference`, {
          method: "POST",
        });
        if (!workspace.assets.some((asset) => asset.id === data.asset.id)) {
          workspace.assets.push(data.asset);
        }
        if (this.activeWorkspace?.id !== workspace.id) return;
        await this.openMaskEditorForAsset(data.asset, { returnDialog: this.el.imageDialog });
      } catch (error) {
        UI.toast(error.message, "error");
      } finally {
        this.detailReferenceBusy = false;
        setDisabled(this.el.detailReuse, false);
        setDisabled(this.el.detailMaskEdit, !this.maskCapableChannels().length);
      }
    },
  });

  window.ImageGenStudio.MaskEditor = MaskEditor;
})();
