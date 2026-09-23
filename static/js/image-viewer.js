(() => {
  "use strict";

  const UI = window.ImageGen;
  const MIN_ZOOM = 0.25;
  const MAX_ZOOM = 8;

  class ImageViewer {
    constructor() {
      const byId = (id) => document.getElementById(id);
      this.el = {
        dialog: byId("imageViewerDialog"),
        title: byId("imageViewerTitle"),
        stage: byId("imageViewerStage"),
        image: byId("imageViewerImage"),
        zoomOut: byId("imageViewerZoomOut"),
        zoomSlider: byId("imageViewerZoomSlider"),
        zoomLabel: byId("imageViewerZoomLabel"),
        zoomIn: byId("imageViewerZoomIn"),
        fit: byId("imageViewerFit"),
        maskToggle: byId("imageViewerMaskToggle"),
        maskOverlay: byId("imageViewerMaskOverlay"),
      };
      this.state = {
        scale: 1,
        fitScale: 1,
        offsetX: 0,
        offsetY: 0,
        dragging: false,
        pointerId: null,
        startX: 0,
        startY: 0,
        originX: 0,
        originY: 0,
      };
      this.source = null;
      this.sourceAlt = "图片预览";
      this.sourceTitle = "图片预览";
      this.mask = null;
      this.maskVisible = false;
      this.maskLoadToken = 0;
      this.bindEvents();
    }

    bindEvents() {
      this.el.zoomOut.addEventListener("click", () => this.adjustZoom(-1));
      this.el.zoomIn.addEventListener("click", () => this.adjustZoom(1));
      this.el.zoomSlider.addEventListener("input", (event) => {
        this.setZoom(Number(event.target.value));
      });
      this.el.fit.addEventListener("click", () => this.fit());
      this.el.maskToggle.addEventListener("click", () => this.toggleMaskView());
      this.el.stage.addEventListener("wheel", (event) => this.handleWheel(event), { passive: false });
      this.el.stage.addEventListener("pointerdown", (event) => this.startPan(event));
      this.el.stage.addEventListener("pointermove", (event) => this.movePan(event));
      this.el.stage.addEventListener("pointerup", (event) => this.endPan(event));
      this.el.stage.addEventListener("pointercancel", (event) => this.endPan(event));
      this.el.stage.addEventListener("lostpointercapture", () => this.cancelPan());
      this.el.stage.addEventListener("keydown", (event) => this.handleKeydown(event));
      this.el.dialog.addEventListener("close", () => this.close());
      document.addEventListener("click", (event) => this.handlePreviewClick(event));
      document.addEventListener("keydown", (event) => this.handlePreviewKeydown(event));
      window.addEventListener("resize", () => this.handleResize());
    }

    open({
      src,
      alt = "图片预览",
      title = "图片预览",
      maskUrl = "",
      maskSource = "",
      maskLabel = "局部重绘区域",
      maskInitial = false,
    } = {}) {
      if (!src) return;
      this.state.scale = 1;
      this.state.fitScale = 1;
      this.state.offsetX = 0;
      this.state.offsetY = 0;
      this.source = src;
      this.sourceAlt = alt || "图片预览";
      this.sourceTitle = title || "图片预览";
      this.mask = maskUrl && maskSource
        ? { url: maskUrl, source: maskSource, label: maskLabel || "局部重绘区域" }
        : null;
      this.maskVisible = false;
      this.maskLoadToken += 1;
      this.clearMaskOverlay();
      this.updateMaskToggle();
      this.el.title.textContent = this.sourceTitle;
      this.el.image.alt = this.sourceAlt;
      this.el.image.onload = () => {
        if (this.source === src) this.fit();
      };
      this.el.image.onerror = () => {
        if (this.source === src) this.el.title.textContent = "图片加载失败";
      };
      this.el.image.src = src;
      this.updateTransform();
      UI.openDialog(this.el.dialog);
      if (maskInitial && this.mask) this.showMaskView();
      window.requestAnimationFrame(() => {
        if (this.source === src && this.el.image.naturalWidth && !this.maskVisible) this.fit();
      });
    }

    close() {
      this.cancelPan();
      this.source = null;
      this.sourceAlt = "图片预览";
      this.sourceTitle = "图片预览";
      this.mask = null;
      this.maskVisible = false;
      this.maskLoadToken += 1;
      this.clearMaskOverlay();
      this.updateMaskToggle();
      this.el.image.removeAttribute("src");
      this.el.image.style.transform = "";
      this.el.stage.classList.remove("is-draggable");
    }

    zoomBounds() {
      const image = this.el.image;
      const stage = this.el.stage;
      if (!image.naturalWidth || !image.naturalHeight || !stage.clientWidth || !stage.clientHeight) {
        return { fit: 1, minimum: MIN_ZOOM, maximum: MAX_ZOOM };
      }
      const fit = Math.min(
        (stage.clientWidth - 48) / image.naturalWidth,
        (stage.clientHeight - 48) / image.naturalHeight,
        1,
      );
      return {
        fit: Math.min(MAX_ZOOM, Math.max(MIN_ZOOM, fit)),
        minimum: MIN_ZOOM,
        maximum: MAX_ZOOM,
      };
    }

    fit() {
      this.state.fitScale = this.zoomBounds().fit;
      this.state.scale = this.state.fitScale;
      this.state.offsetX = 0;
      this.state.offsetY = 0;
      this.updateTransform();
    }

    setZoom(value) {
      const { minimum, maximum } = this.zoomBounds();
      const scale = Number(value);
      this.state.scale = Math.min(
        maximum,
        Math.max(minimum, Number.isFinite(scale) ? scale : this.state.scale),
      );
      this.clampOffset();
      this.updateTransform();
    }

    adjustZoom(direction) {
      const factor = direction > 0 ? 1.25 : 0.8;
      this.setZoom(Number((this.state.scale * factor).toFixed(2)));
    }

    panBounds(scale = this.state.scale) {
      return {
        maxX: Math.abs(
          this.el.image.naturalWidth * scale - this.el.stage.clientWidth,
        ) / 2,
        maxY: Math.abs(
          this.el.image.naturalHeight * scale - this.el.stage.clientHeight,
        ) / 2,
      };
    }

    clampOffset() {
      const { maxX, maxY } = this.panBounds();
      this.state.offsetX = Math.min(maxX, Math.max(-maxX, this.state.offsetX));
      this.state.offsetY = Math.min(maxY, Math.max(-maxY, this.state.offsetY));
    }

    updateTransform() {
      this.clampOffset();
      const transform = `translate3d(calc(-50% + ${this.state.offsetX}px), calc(-50% + ${this.state.offsetY}px), 0) scale(${this.state.scale})`;
      this.el.image.style.transform = transform;
      this.el.maskOverlay.style.transform = transform;
      this.el.zoomSlider.value = String(this.state.scale);
      this.el.zoomLabel.textContent = `${Math.round(this.state.scale * 100)}%`;
      this.el.stage.classList.toggle("is-draggable", Boolean(this.el.image.naturalWidth));
    }

    updateMaskToggle() {
      const available = Boolean(this.mask);
      this.el.maskToggle.hidden = !available;
      this.el.maskToggle.setAttribute("aria-pressed", String(this.maskVisible));
      const labelText = this.maskVisible
        ? (this.source === this.mask?.source ? "隐藏重绘区域" : "返回生成结果")
        : "查看重绘区域";
      this.el.maskToggle.title = labelText;
      const label = this.el.maskToggle.querySelector("span");
      if (label) label.textContent = labelText;
    }

    toggleMaskView() {
      if (!this.mask) return;
      if (this.maskVisible) this.showResultView();
      else this.showMaskView();
    }

    showResultView() {
      if (!this.source) return;
      this.maskVisible = false;
      this.maskLoadToken += 1;
      this.clearMaskOverlay();
      this.updateMaskToggle();
      this.el.title.textContent = this.sourceTitle;
      this.el.image.alt = this.sourceAlt;
      this.el.image.onload = () => {
        if (this.source && !this.maskVisible) this.fit();
      };
      this.el.image.onerror = () => {
        if (this.source && !this.maskVisible) this.el.title.textContent = "图片加载失败";
      };
      this.el.image.src = this.source;
      if (this.el.image.complete && this.el.image.naturalWidth) this.fit();
    }

    showMaskView() {
      if (!this.mask) return;
      this.maskVisible = true;
      const token = ++this.maskLoadToken;
      this.updateMaskToggle();
      this.el.title.textContent = `${this.mask.label} · 原图`;
      this.el.image.alt = `${this.mask.label}原图`;
      let sourceReady = false;
      const showOverlay = () => {
        if (token !== this.maskLoadToken || !this.maskVisible || sourceReady) return;
        sourceReady = true;
        this.fit();
        this.loadMaskOverlay(token);
      };
      this.el.image.onload = showOverlay;
      this.el.image.onerror = () => {
        if (token !== this.maskLoadToken) return;
        this.el.title.textContent = "原图加载失败";
        this.clearMaskOverlay();
      };
      this.clearMaskOverlay();
      this.el.image.src = this.mask.source;
      this.updateTransform();
      if (this.el.image.complete && this.el.image.naturalWidth) {
        showOverlay();
      }
    }

    async loadMaskOverlay(token) {
      if (!this.mask || token !== this.maskLoadToken || !this.maskVisible) return;
      const maskImage = new Image();
      maskImage.decoding = "async";
      try {
        await new Promise((resolve, reject) => {
          maskImage.addEventListener("load", resolve, { once: true });
          maskImage.addEventListener("error", reject, { once: true });
          maskImage.src = this.mask.url;
        });
        if (token !== this.maskLoadToken || !this.maskVisible || !this.el.image.naturalWidth) return;
        this.renderMaskOverlay(maskImage, this.el.image.naturalWidth, this.el.image.naturalHeight);
        this.updateTransform();
      } catch (_error) {
        if (token === this.maskLoadToken) {
          this.clearMaskOverlay();
          this.el.title.textContent = "重绘区域加载失败";
        }
      }
    }

    renderMaskOverlay(maskImage, width, height) {
      const maxPixels = 16_000_000;
      const scale = Math.min(1, Math.sqrt(maxPixels / Math.max(1, width * height)));
      const canvas = this.el.maskOverlay;
      canvas.width = Math.max(1, Math.round(width * scale));
      canvas.height = Math.max(1, Math.round(height * scale));
      canvas.style.width = `${width}px`;
      canvas.style.height = `${height}px`;
      const context = canvas.getContext("2d", { willReadFrequently: true });
      context.clearRect(0, 0, canvas.width, canvas.height);
      context.drawImage(maskImage, 0, 0, canvas.width, canvas.height);
      const pixels = context.getImageData(0, 0, canvas.width, canvas.height);
      for (let index = 0; index < pixels.data.length; index += 4) {
        const selectedAlpha = 255 - pixels.data[index + 3];
        pixels.data[index] = 239;
        pixels.data[index + 1] = 79;
        pixels.data[index + 2] = 79;
        pixels.data[index + 3] = Math.round(selectedAlpha * 0.52);
      }
      context.putImageData(pixels, 0, 0);
      canvas.hidden = false;
    }

    clearMaskOverlay() {
      const canvas = this.el.maskOverlay;
      canvas.hidden = true;
      canvas.width = 1;
      canvas.height = 1;
      canvas.style.width = "";
      canvas.style.height = "";
      canvas.style.transform = "";
    }

    handleWheel(event) {
      if (!this.el.dialog.open) return;
      event.preventDefault();
      const factor = Math.exp(-event.deltaY * 0.0015);
      this.setZoom(Number((this.state.scale * factor).toFixed(2)));
    }

    startPan(event) {
      if (event.button !== 0 || !this.el.image.naturalWidth) return;
      event.preventDefault();
      this.state.dragging = true;
      this.state.pointerId = event.pointerId;
      this.state.startX = event.clientX;
      this.state.startY = event.clientY;
      this.state.originX = this.state.offsetX;
      this.state.originY = this.state.offsetY;
      this.el.stage.classList.add("is-dragging");
      this.el.stage.setPointerCapture(event.pointerId);
    }

    movePan(event) {
      if (!this.state.dragging || this.state.pointerId !== event.pointerId) return;
      this.state.offsetX = this.state.originX + event.clientX - this.state.startX;
      this.state.offsetY = this.state.originY + event.clientY - this.state.startY;
      this.updateTransform();
    }

    cancelPan() {
      this.state.dragging = false;
      this.state.pointerId = null;
      this.el.stage.classList.remove("is-dragging");
    }

    endPan(event) {
      if (this.state.pointerId !== event.pointerId) return;
      const pointerId = this.state.pointerId;
      this.cancelPan();
      if (this.el.stage.hasPointerCapture(pointerId)) this.el.stage.releasePointerCapture(pointerId);
    }

    handleKeydown(event) {
      if (event.key === "+" || event.key === "=") {
        event.preventDefault();
        this.adjustZoom(1);
      } else if (event.key === "-") {
        event.preventDefault();
        this.adjustZoom(-1);
      } else if (event.key === "0") {
        event.preventDefault();
        this.fit();
      }
    }

    handleResize() {
      if (!this.el.dialog.open || !this.el.image.naturalWidth) return;
      if (this.state.scale <= this.state.fitScale + 0.01) this.fit();
      else {
        this.clampOffset();
        this.updateTransform();
      }
    }

    previewSource(trigger) {
      return trigger.dataset.imagePreviewSrc
        || (trigger.tagName === "IMG" ? trigger.currentSrc || trigger.src : "")
        || trigger.getAttribute("href")
        || "";
    }

    handlePreviewClick(event) {
      const trigger = event.target.closest("[data-image-preview]");
      if (!trigger || trigger.closest("#imageViewerDialog") || trigger.disabled) return;
      const src = this.previewSource(trigger);
      if (!src) return;
      event.preventDefault();
      this.open({
        src,
        alt: trigger.dataset.imagePreviewAlt || trigger.getAttribute("alt") || "图片预览",
        title: trigger.dataset.imagePreviewTitle || trigger.getAttribute("title") || "图片预览",
        maskUrl: trigger.dataset.imagePreviewMaskUrl || "",
        maskSource: trigger.dataset.imagePreviewMaskSource || "",
        maskLabel: trigger.dataset.imagePreviewMaskLabel || "局部重绘区域",
        maskInitial: trigger.dataset.imagePreviewMaskInitial === "true",
      });
    }

    handlePreviewKeydown(event) {
      if (!(event.key === "Enter" || event.key === " ")) return;
      const trigger = event.target.closest("[data-image-preview]");
      if (!trigger || trigger.closest("#imageViewerDialog") || trigger.disabled) return;
      event.preventDefault();
      trigger.click();
    }
  }

  document.addEventListener("DOMContentLoaded", () => {
    if (document.getElementById("imageViewerDialog")) new ImageViewer();
  });
})();
