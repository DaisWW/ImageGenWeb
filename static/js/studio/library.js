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
    libraryTargetLabel() {
      return this.libraryTarget === "chat"
        ? "随消息发送"
        : this.isAnimationWorkspace() ? "设为母图" : "设为垫图";
    },

    async openLibrary(target = "") {
      if (!this.activeWorkspace) return;
      this.libraryTarget = target || (
        this.isAnimationWorkspace() || !this.el.generationForm.hidden ? "generation" : "chat"
      );
      this.librarySelection.clear();
      this.el.libraryTargetLabel.textContent = this.libraryTargetLabel();
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
        unloaded || this.libraryLoading || this.libraryUploading || this.libraryBusy,
      );
      setDisabled(this.el.libraryLoadMoreButton, this.libraryLoading || this.libraryBusy);
      this.el.libraryLoadMoreButton.classList.toggle("loading", this.libraryLoading && !unloaded);
      const action = this.libraryTargetLabel();
      const disabled = this.libraryBusy ? " disabled" : "";
      this.el.libraryGrid.innerHTML = images.map((entry) => {
        const id = UI.escapeHtml(entry.id);
        const name = UI.escapeHtml(entry.name);
        const url = UI.escapeHtml(entry.thumbnail_url || entry.url);
        const useTitle = UI.escapeHtml(`${action}：${entry.name}`);
        const selected = this.librarySelection.has(entry.id);
        const selectTitle = UI.escapeHtml(selected ? `取消选择 ${entry.name}` : `选择 ${entry.name}`);
        const deleteTitle = UI.escapeHtml(`从图库删除 ${entry.name}`);
        return `<article class="library-card${selected ? " selected" : ""}" data-library-image="${id}">
          <button type="button" class="library-use" data-use-library-image="${id}" title="${useTitle}"${disabled}>
            <span class="library-thumbnail"><img src="${url}" alt="${name}" loading="lazy" decoding="async"></span>
            <span class="library-card-copy"><strong>${name}</strong></span>
          </button>
          <label class="library-select" title="${selectTitle}">
            <input type="checkbox" data-select-library-image="${id}"${selected ? " checked" : ""}${disabled} aria-label="${selectTitle}">
          </label>
          <button type="button" class="icon-button library-delete" data-delete-library-image="${id}" title="${deleteTitle}" aria-label="${deleteTitle}"${disabled}><i data-lucide="trash-2"></i></button>
        </article>`;
      }).join("");
      this.el.libraryGrid.querySelectorAll("img").forEach((image) => this.prepareImageReveal(image));
      UI.icons(this.el.libraryGrid);
      this.updateLibrarySelectionUI();
    },

    librarySelectionContext(workspace = this.activeWorkspace) {
      const target = this.libraryTarget;
      const rawLimit = Number(this.referenceSelectionLimit(target, workspace));
      const limit = Number.isFinite(rawLimit) && rawLimit > 0 ? rawLimit : 0;
      const replacesAnimationMaster = workspace?.kind === "animation"
        && target === "generation";
      const selection = target === "chat"
        ? this.currentChatSelection(workspace?.id)
        : this.currentSelection(workspace?.id);
      const available = replacesAnimationMaster
        ? 1
        : Math.max(0, limit - selection.size);
      return { target, limit, selection, replacesAnimationMaster, available };
    },

    updateLibrarySelectionUI() {
      if (!this.el?.librarySelectionSummary) return;
      const { limit, selection, replacesAnimationMaster } = this.librarySelectionContext();
      const selectionCount = this.librarySelection.size;
      const existing = replacesAnimationMaster ? 0 : selection.size;
      this.el.librarySelectionSummary.textContent = limit > 0
        ? `已选择 ${existing + selectionCount} / ${limit} 张`
        : `已选择 ${selectionCount} 张`;
      const hasImages = (this.libraryImages || []).length > 0;
      setDisabled(this.el.librarySelectAllButton, this.libraryBusy || this.libraryLoading || !hasImages);
      setDisabled(this.el.libraryClearSelectionButton, this.libraryBusy || !selectionCount);
      setDisabled(this.el.libraryConfirmButton, this.libraryBusy || !selectionCount);
      this.el.libraryConfirmButton.classList.toggle("is-loading", this.libraryBusy);
    },

    handleLibrarySelectionChange(event) {
      const input = event.target.closest("[data-select-library-image]");
      if (!input) return;
      const imageId = input.dataset.selectLibraryImage;
      if (input.checked) {
        const { target, limit, available } = this.librarySelectionContext();
        if (!this.librarySelection.has(imageId) && this.librarySelection.size >= available) {
          input.checked = false;
          UI.toast(target === "chat"
            ? `每条消息最多发送 ${limit} 张图片`
            : `当前渠道最多选择 ${limit} 张垫图`, "error");
          return;
        }
        this.librarySelection.add(imageId);
      } else {
        this.librarySelection.delete(imageId);
      }
      const selected = this.librarySelection.has(imageId);
      const image = this.libraryImages?.find((entry) => entry.id === imageId);
      const selectTitle = `${selected ? "取消选择" : "选择"} ${image?.name || "图片"}`;
      input.checked = selected;
      input.setAttribute("aria-label", selectTitle);
      input.closest(".library-select")?.setAttribute("title", selectTitle);
      input.closest(".library-card")?.classList.toggle("selected", selected);
      this.updateLibrarySelectionUI();
    },

    selectAllLibraryImages() {
      if (this.libraryBusy || this.libraryLoading) return;
      const images = this.libraryImages || [];
      const available = this.librarySelectionContext().available - this.librarySelection.size;
      if (available <= 0) {
        UI.toast("已达到当前场景的图片上限", "info");
        return;
      }
      images.filter((image) => !this.librarySelection.has(image.id))
        .slice(0, available)
        .forEach((image) => this.librarySelection.add(image.id));
      this.renderLibrary();
      if (images.some((image) => !this.librarySelection.has(image.id))) {
        UI.toast("已按当前场景上限选择图片", "info");
      }
    },

    clearLibrarySelection() {
      if (this.libraryBusy) return;
      this.librarySelection.clear();
      this.renderLibrary();
    },

    async confirmLibrarySelection() {
      if (!this.librarySelection.size) return;
      await this.useLibraryImages([...this.librarySelection]);
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
        || this.libraryLoading || this.libraryUploading || this.libraryBusy) return;
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
        || this.libraryUploading
        || this.libraryBusy;
      event.dataTransfer.dropEffect = blocked ? "none" : "copy";
    },

    handleLibraryDrop(event) {
      if (![...(event.dataTransfer?.types || [])].includes("Files")) return;
      event.preventDefault();
      if (this.libraryImages === null || this.libraryLoading || this.libraryUploading || this.libraryBusy) return;
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
          this.librarySelection.delete(image.id);
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
      if (use && !use.disabled) await this.useLibraryImages([use.dataset.useLibraryImage]);
    },

    async importLibraryAsset(workspace, imageId) {
      const data = await UI.api(
        `/api/workspaces/${workspace.id}/assets/from-library/${imageId}`,
        { method: "POST" },
      );
      if (!data.asset) throw new Error("图库图片导入结果无效");
      if (!workspace.assets.some((asset) => asset.id === data.asset.id)) {
        workspace.assets.push(data.asset);
      }
      return data.asset;
    },

    async useLibraryImages(imageIds) {
      if (!this.activeWorkspace || this.referenceUploadPending
        || this.workspaceChatBusy() || this.workspaceHasActiveJob() || this.libraryBusy) {
        UI.toast("当前工作站忙碌，请稍后选择图片", "error");
        return;
      }
      const workspace = this.activeWorkspace;
      const {
        target,
        limit,
        selection,
        replacesAnimationMaster,
        available,
      } = this.librarySelectionContext(workspace);
      const requested = [...new Set(imageIds)].slice(0, available);
      if (!requested.length) {
        UI.toast(target === "chat" ? `每条消息最多发送 ${limit} 张图片` : `当前渠道最多选择 ${limit} 张垫图`, "error");
        return;
      }

      this.libraryBusy = true;
      this.renderLibrary();
      const imported = [];
      const failures = [];
      try {
        for (const imageId of requested) {
          try {
            const asset = await this.importLibraryAsset(workspace, imageId);
            if (replacesAnimationMaster) selection.clear();
            selection.add(asset.id);
            imported.push(asset);
          } catch (error) {
            failures.push(error);
          }
        }
      } finally {
        this.libraryBusy = false;
      }

      if (this.activeWorkspace?.id !== workspace.id) {
        this.librarySelection.clear();
        this.renderLibrary();
        return;
      }
      if (!imported.length) {
        this.renderLibrary();
        UI.toast(failures[0]?.message || "图库图片导入失败", "error");
        return;
      }
      this.librarySelection.clear();
      this.renderWorkspaceList();
      UI.closeDialog(this.el.libraryDialog);
      if (target === "chat") {
        this.chatReferencePickerOpen = true;
        this.setComposerMode("chat");
        this.renderChatReferences();
        this.el.chatInput.focus();
        UI.toast(
          imported.length === 1 ? "已加入待发送图片" : `已加入 ${imported.length} 张待发送图片`,
          "success",
        );
      } else {
        if (this.el.generationForm.hidden) {
          this.openGenerationComposer([...selection]);
        } else {
          this.setMode("img2img", true);
          this.renderReferences();
          this.el.promptInput.focus();
        }
        UI.toast(
          workspace.kind === "animation"
            ? "已设为母图"
            : imported.length === 1 ? "已选择垫图" : `已选择 ${imported.length} 张垫图`,
          "success",
        );
      }
      if (failures.length) UI.toast(`有 ${failures.length} 张图片导入失败`, "error");
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
