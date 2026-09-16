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
      this.bindEvents();
    }

    bindEvents() {
      this.el.zoomOut.addEventListener("click", () => this.adjustZoom(-1));
      this.el.zoomIn.addEventListener("click", () => this.adjustZoom(1));
      this.el.zoomSlider.addEventListener("input", (event) => {
        this.setZoom(Number(event.target.value));
      });
      this.el.fit.addEventListener("click", () => this.fit());
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

    open({ src, alt = "图片预览", title = "图片预览" } = {}) {
      if (!src) return;
      this.state.scale = 1;
      this.state.fitScale = 1;
      this.state.offsetX = 0;
      this.state.offsetY = 0;
      this.source = src;
      this.el.title.textContent = title || "图片预览";
      this.el.image.alt = alt || "图片预览";
      this.el.image.onload = () => {
        if (this.source === src) this.fit();
      };
      this.el.image.onerror = () => {
        if (this.source === src) this.el.title.textContent = "图片加载失败";
      };
      this.el.image.src = src;
      this.updateTransform();
      UI.openDialog(this.el.dialog);
      window.requestAnimationFrame(() => {
        if (this.source === src && this.el.image.naturalWidth) this.fit();
      });
    }

    close() {
      this.cancelPan();
      this.source = null;
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
      this.el.image.style.transform = `translate3d(calc(-50% + ${this.state.offsetX}px), calc(-50% + ${this.state.offsetY}px), 0) scale(${this.state.scale})`;
      this.el.zoomSlider.value = String(this.state.scale);
      this.el.zoomLabel.textContent = `${Math.round(this.state.scale * 100)}%`;
      this.el.stage.classList.toggle("is-draggable", Boolean(this.el.image.naturalWidth));
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
