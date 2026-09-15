(() => {
  "use strict";

  const { StudioApp, UI } = window.ImageGenStudio;
  const MIN_ZOOM = 0.25;
  const MAX_ZOOM = 8;

  Object.assign(StudioApp.prototype, {
    imageViewerState() {
      if (!this._imageViewerState) {
        this._imageViewerState = {
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
      }
      return this._imageViewerState;
    },

    openImageViewer({ src, alt = "图片预览", title = "图片预览" } = {}) {
      if (!src) return;
      const state = this.imageViewerState();
      state.scale = 1;
      state.fitScale = 1;
      state.offsetX = 0;
      state.offsetY = 0;
      this.imageViewerSource = src;
      this.el.imageViewerTitle.textContent = title || "图片预览";
      this.el.imageViewerImage.alt = alt || "图片预览";
      this.el.imageViewerImage.onload = () => {
        if (this.imageViewerSource !== src) return;
        this.fitImageViewer();
      };
      this.el.imageViewerImage.onerror = () => {
        if (this.imageViewerSource !== src) return;
        this.el.imageViewerTitle.textContent = "图片加载失败";
      };
      this.el.imageViewerImage.src = src;
      this.updateImageViewerTransform();
      UI.openDialog(this.el.imageViewerDialog);
      window.requestAnimationFrame(() => {
        if (this.imageViewerSource !== src) return;
        if (this.el.imageViewerImage.naturalWidth) this.fitImageViewer();
      });
    },

    closeImageViewer() {
      this.cancelImageViewerPan();
      this.imageViewerSource = null;
      this.el.imageViewerImage.removeAttribute("src");
      this.el.imageViewerImage.style.transform = "";
    },

    imageViewerZoomBounds() {
      const image = this.el.imageViewerImage;
      const stage = this.el.imageViewerStage;
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
    },

    fitImageViewer() {
      const state = this.imageViewerState();
      state.fitScale = this.imageViewerZoomBounds().fit;
      state.scale = state.fitScale;
      state.offsetX = 0;
      state.offsetY = 0;
      this.updateImageViewerTransform();
    },

    setImageViewerZoom(value) {
      const state = this.imageViewerState();
      const { minimum, maximum } = this.imageViewerZoomBounds();
      const scale = Number(value);
      state.scale = Math.min(maximum, Math.max(minimum, Number.isFinite(scale) ? scale : state.scale));
      this.clampImageViewerOffset();
      this.updateImageViewerTransform();
    },

    adjustImageViewerZoom(direction) {
      const state = this.imageViewerState();
      const factor = direction > 0 ? 1.25 : 0.8;
      this.setImageViewerZoom(Number((state.scale * factor).toFixed(2)));
    },

    clampImageViewerOffset() {
      const state = this.imageViewerState();
      const image = this.el.imageViewerImage;
      const stage = this.el.imageViewerStage;
      const maxX = Math.max(0, (image.naturalWidth * state.scale - stage.clientWidth) / 2);
      const maxY = Math.max(0, (image.naturalHeight * state.scale - stage.clientHeight) / 2);
      state.offsetX = Math.min(maxX, Math.max(-maxX, state.offsetX));
      state.offsetY = Math.min(maxY, Math.max(-maxY, state.offsetY));
    },

    updateImageViewerTransform() {
      const state = this.imageViewerState();
      this.clampImageViewerOffset();
      this.el.imageViewerImage.style.transform = `translate3d(calc(-50% + ${state.offsetX}px), calc(-50% + ${state.offsetY}px), 0) scale(${state.scale})`;
      this.el.imageViewerZoomSlider.value = String(state.scale);
      this.el.imageViewerZoomLabel.textContent = `${Math.round(state.scale * 100)}%`;
      this.el.imageViewerStage.classList.toggle("is-zoomed", state.scale > state.fitScale + 0.01);
    },

    handleImageViewerWheel(event) {
      if (!this.el.imageViewerDialog.open) return;
      event.preventDefault();
      const factor = Math.exp(-event.deltaY * 0.0015);
      this.setImageViewerZoom(Number((this.imageViewerState().scale * factor).toFixed(2)));
    },

    startImageViewerPan(event) {
      const state = this.imageViewerState();
      if (event.button !== 0 || !this.el.imageViewerImage.naturalWidth
        || state.scale <= state.fitScale + 0.01) return;
      state.dragging = true;
      state.pointerId = event.pointerId;
      state.startX = event.clientX;
      state.startY = event.clientY;
      state.originX = state.offsetX;
      state.originY = state.offsetY;
      this.el.imageViewerStage.classList.add("is-dragging");
      this.el.imageViewerStage.setPointerCapture(event.pointerId);
    },

    moveImageViewerPan(event) {
      const state = this.imageViewerState();
      if (!state.dragging || state.pointerId !== event.pointerId) return;
      state.offsetX = state.originX + event.clientX - state.startX;
      state.offsetY = state.originY + event.clientY - state.startY;
      this.updateImageViewerTransform();
    },

    cancelImageViewerPan() {
      const state = this.imageViewerState();
      state.dragging = false;
      state.pointerId = null;
      this.el.imageViewerStage.classList.remove("is-dragging");
    },

    endImageViewerPan(event) {
      const state = this.imageViewerState();
      if (state.pointerId !== event.pointerId) return;
      const pointerId = state.pointerId;
      this.cancelImageViewerPan();
      if (this.el.imageViewerStage.hasPointerCapture(pointerId)) {
        this.el.imageViewerStage.releasePointerCapture(pointerId);
      }
    },

    handleImageViewerKeydown(event) {
      if (event.key === "+" || event.key === "=") {
        event.preventDefault();
        this.adjustImageViewerZoom(1);
      } else if (event.key === "-") {
        event.preventDefault();
        this.adjustImageViewerZoom(-1);
      } else if (event.key === "0") {
        event.preventDefault();
        this.fitImageViewer();
      }
    },

    handleImageViewerResize() {
      const state = this.imageViewerState();
      if (!this.el.imageViewerDialog.open || !this.el.imageViewerImage.naturalWidth) return;
      if (state.scale <= state.fitScale + 0.01) this.fitImageViewer();
      else {
        this.clampImageViewerOffset();
        this.updateImageViewerTransform();
      }
    },

    imagePreviewSource(trigger) {
      return trigger.dataset.imagePreviewSrc
        || (trigger.tagName === "IMG" ? trigger.currentSrc || trigger.src : "")
        || trigger.getAttribute("href")
        || "";
    },

    handleImagePreviewClick(event) {
      const trigger = event.target.closest("[data-image-preview]");
      if (!trigger || trigger.closest("#imageViewerDialog") || trigger.disabled) return;
      const src = this.imagePreviewSource(trigger);
      if (!src) return;
      event.preventDefault();
      this.openImageViewer({
        src,
        alt: trigger.dataset.imagePreviewAlt || trigger.getAttribute("alt") || "图片预览",
        title: trigger.dataset.imagePreviewTitle || trigger.getAttribute("title") || "图片预览",
      });
    },

    handleImagePreviewKeydown(event) {
      if (!(["Enter", " "].includes(event.key))) return;
      const trigger = event.target.closest("[data-image-preview]");
      if (!trigger || trigger.closest("#imageViewerDialog") || trigger.disabled) return;
      event.preventDefault();
      trigger.click();
    },
  });

})();
