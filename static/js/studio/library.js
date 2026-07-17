(() => {
  "use strict";

  const {
    StudioApp,
    UI,
    REFERENCE_IMAGE_TYPES,
    REFERENCE_IMAGE_EXTENSION,
    setHidden,
    setDisabled,
    setAttribute,
  } = window.ImageGenStudio;

  Object.assign(StudioApp.prototype, {
    async openLibrary(target = "") {
      if (!this.activeWorkspace || this.workspaceLoading) return;
      this.libraryTarget = target || (
        this.isAnimationWorkspace() || !this.el.generationForm.hidden ? "generation" : "chat"
      );
      this.el.libraryTargetLabel.textContent = this.libraryTarget === "chat"
        ? "随消息发送"
        : this.isAnimationWorkspace() ? "设为母图" : "设为垫图";
      this.renderLibrary();
      UI.openDialog(this.el.libraryDialog);
      if (this.libraryImages === null) await this.loadLibraryImages();
    },

    async loadLibraryImages({ append = false } = {}) {
      if (this.libraryLoading || (append && !this.libraryHasMore)) return;
      this.libraryLoading = true;
      this.libraryLoadError = "";
      this.renderLibrary();
      const offset = append ? this.libraryOffset : 0;
      try {
        const data = await UI.api(`/api/library-images?offset=${offset}&limit=60`);
        const images = data.images || [];
        if (append && this.libraryImages !== null) {
          this.libraryImages = [...new Map(
            [...this.libraryImages, ...images].map((image) => [image.id, image]),
          ).values()];
        } else {
          this.libraryImages = images;
        }
        this.libraryOffset = offset + images.length;
        this.libraryTotal = Number(data.total ?? this.libraryImages.length);
        this.libraryHasMore = data.has_more === true;
      } catch (error) {
        this.libraryLoadError = error.message;
        if (!append) this.libraryImages = null;
        UI.toast(error.message, "error");
      } finally {
        this.libraryLoading = false;
        this.renderLibrary();
      }
    },

    renderLibrary() {
      const images = this.libraryImages || [];
      const unloaded = this.libraryImages === null;
      setHidden(this.el.libraryLoading, !unloaded || !this.libraryLoading);
      setHidden(
        this.el.libraryError,
        !unloaded || this.libraryLoading || !this.libraryLoadError,
      );
      setHidden(this.el.libraryEmpty, unloaded || images.length > 0);
      setHidden(this.el.libraryGrid, unloaded || images.length === 0);
      setHidden(this.el.libraryPagination, unloaded || !this.libraryHasMore);
      setDisabled(
        this.el.libraryUploadButton,
        unloaded || this.libraryLoading || this.libraryUploading,
      );
      setDisabled(this.el.libraryLoadMoreButton, this.libraryLoading);
      this.el.libraryLoadMoreButton.classList.toggle("loading", this.libraryLoading && !unloaded);
      const action = this.libraryTarget === "chat"
        ? "随消息发送"
        : this.isAnimationWorkspace() ? "设为母图" : "设为垫图";
      this.el.libraryGrid.innerHTML = images.map((entry) => {
        const id = UI.escapeHtml(entry.id);
        const name = UI.escapeHtml(entry.name);
        const url = UI.escapeHtml(entry.thumbnail_url || entry.url);
        const useTitle = UI.escapeHtml(`${action}：${entry.name}`);
        const deleteTitle = UI.escapeHtml(`从图库删除 ${entry.name}`);
        return `<article class="library-card">
          <button type="button" class="library-use" data-use-library-image="${id}" title="${useTitle}">
            <span class="library-thumbnail"><img src="${url}" alt="${name}" loading="lazy" decoding="async"></span>
            <span class="library-card-copy"><strong>${name}</strong></span>
          </button>
          <button type="button" class="icon-button library-delete" data-delete-library-image="${id}" title="${deleteTitle}" aria-label="${deleteTitle}"><i data-lucide="trash-2"></i></button>
        </article>`;
      }).join("");
      this.el.libraryGrid.querySelectorAll("img").forEach((image) => this.prepareImageReveal(image));
      UI.icons(this.el.libraryGrid);
    },

    mergeLibraryImages(images, addedCount = 0) {
      if (this.libraryImages === null) return;
      const added = Number(addedCount || 0);
      this.libraryTotal += added;
      this.libraryOffset += added;
      const merged = new Map(
        [...images, ...this.libraryImages].map((image) => [image.id, image]),
      );
      this.libraryImages = [...merged.values()];
      this.libraryHasMore = this.libraryOffset < this.libraryTotal;
      this.renderLibrary();
    },

    async uploadLibraryImages(files) {
      this.el.libraryInput.value = "";
      if (!files.length || this.libraryImages === null
        || this.libraryLoading || this.libraryUploading) return;
      const images = files.filter((file) => (
        REFERENCE_IMAGE_TYPES.has(file.type.toLowerCase())
        || REFERENCE_IMAGE_EXTENSION.test(file.name)
      ));
      if (!images.length) {
        UI.toast("仅支持 PNG、JPEG 和 WebP 静态图片", "error");
        return;
      }
      const data = new FormData();
      images.forEach((file) => data.append("images", file, file.name));
      this.libraryUploading = true;
      this.renderLibrary();
      try {
        const payload = await UI.api("/api/library-images", { method: "POST", body: data });
        this.mergeLibraryImages(payload.images || [], payload.added_count);
        UI.toast(
          payload.added_count ? `已将 ${payload.added_count} 张图片存入图库` : "图库中已有这些图片",
          "success",
        );
        if (files.length > images.length) {
          UI.toast(`已忽略 ${files.length - images.length} 个不支持的文件`, "info");
        }
      } catch (error) {
        UI.toast(error.message, "error");
      } finally {
        this.libraryUploading = false;
        this.renderLibrary();
      }
    },

    handleLibraryDrag(event) {
      if (![...(event.dataTransfer?.types || [])].includes("Files")) return;
      event.preventDefault();
      const blocked = this.libraryImages === null
        || this.libraryLoading
        || this.libraryUploading;
      event.dataTransfer.dropEffect = blocked ? "none" : "copy";
    },

    handleLibraryDrop(event) {
      if (![...(event.dataTransfer?.types || [])].includes("Files")) return;
      event.preventDefault();
      if (this.libraryImages === null || this.libraryLoading || this.libraryUploading) return;
      this.uploadLibraryImages([...event.dataTransfer.files]);
    },

    async handleLibraryClick(event) {
      const remove = event.target.closest("[data-delete-library-image]");
      if (remove) {
        const image = this.libraryImages?.find((entry) => entry.id === remove.dataset.deleteLibraryImage);
        if (!image || !window.confirm(`从图库删除“${image.name}”？已复制到工作站的图片不会受影响。`)) return;
        remove.disabled = true;
        try {
          await UI.api(`/api/library-images/${image.id}`, { method: "DELETE" });
          this.libraryImages = this.libraryImages.filter((entry) => entry.id !== image.id);
          this.libraryTotal = Math.max(0, this.libraryTotal - 1);
          this.libraryOffset = Math.max(0, this.libraryOffset - 1);
          this.libraryHasMore = this.libraryOffset < this.libraryTotal;
          this.renderLibrary();
          UI.toast("已从图库删除", "success");
        } catch (error) {
          remove.disabled = false;
          UI.toast(error.message, "error");
        }
        return;
      }
      const use = event.target.closest("[data-use-library-image]");
      if (use) await this.useLibraryImage(use.dataset.useLibraryImage, use);
    },

    async useLibraryImage(imageId, button) {
      if (!this.activeWorkspace || this.workspaceLoading || this.referenceUploadPending
        || this.workspaceChatBusy() || this.workspaceHasActiveJob()) {
        UI.toast("当前工作站忙碌，请稍后选择图片", "error");
        return;
      }
      const workspace = this.activeWorkspace;
      const target = this.libraryTarget;
      const selection = target === "chat" ? this.currentChatSelection() : this.currentSelection();
      const limit = this.referenceSelectionLimit(target, workspace);
      const replacesAnimationMaster = workspace.kind === "animation" && target === "generation";
      if (!replacesAnimationMaster && selection.size >= limit) {
        UI.toast(target === "chat" ? `每条消息最多发送 ${limit} 张图片` : `当前渠道最多选择 ${limit} 张垫图`, "error");
        return;
      }
      button.disabled = true;
      try {
        const data = await UI.api(
          `/api/workspaces/${workspace.id}/assets/from-library/${imageId}`,
          { method: "POST" },
        );
        if (!workspace.assets.some((asset) => asset.id === data.asset.id)) {
          workspace.assets.push(data.asset);
        }
        if (replacesAnimationMaster) selection.clear();
        selection.add(data.asset.id);
        this.renderWorkspaceList();
        this.renderReferences();
        this.renderChatReferences();
        UI.closeDialog(this.el.libraryDialog);
        if (target === "chat") {
          this.chatReferencePickerOpen = true;
          this.setComposerMode("chat");
          this.renderChatReferences();
          this.el.chatInput.focus();
          UI.toast("已加入待发送图片", "success");
        } else {
          this.setMode("img2img", true);
          this.renderReferences();
          if (this.el.generationForm.hidden) this.openGenerationComposer([data.asset.id]);
          else this.el.promptInput.focus();
          UI.toast(workspace.kind === "animation" ? "已设为母图" : "已选择垫图", "success");
        }
      } catch (error) {
        button.disabled = false;
        UI.toast(error.message, "error");
      }
    },

    async saveLibrarySource(source, button) {
      if (button.disabled) return;
      button.disabled = true;
      try {
        const data = await UI.api("/api/library-images", { method: "POST", body: source });
        this.mergeLibraryImages(data.images || [], data.added_count);
        UI.toast(data.added_count ? "已存入图库" : "图库中已有这张图片", "success");
      } catch (error) {
        UI.toast(error.message, "error");
      } finally {
        button.disabled = false;
      }
    },

    saveDetailToLibrary() {
      if (!this.detailItemId) return;
      return this.saveLibrarySource(
        { generation_item_id: this.detailItemId },
        this.el.detailSaveLibrary,
      );
    },

    librarySaveButton(asset) {
      const button = document.createElement("button");
      button.type = "button";
      button.className = "reference-library-save";
      button.dataset.saveLibraryAsset = asset.id;
      button.title = `将 ${asset.name} 存入图库`;
      button.setAttribute("aria-label", button.title);
      button.innerHTML = '<i data-lucide="bookmark-plus"></i>';
      return button;
    },

  });
})();
