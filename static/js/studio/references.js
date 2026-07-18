(() => {
  "use strict";

  const {
    StudioApp,
    UI,
    REFERENCE_IMAGE_TYPES,
    REFERENCE_IMAGE_EXTENSION,
  } = window.ImageGenStudio;

  Object.assign(StudioApp.prototype, {
    referenceUploadCard(pending, target = "generation") {
      const chat = target === "chat";
      const card = document.createElement(chat ? "span" : "div");
      card.className = chat
        ? "chat-reference-item reference-upload-card"
        : "reference-card reference-upload-card";
      const preview = document.createElement(chat ? "span" : "div");
      preview.className = `${chat ? "chat-reference-card" : "reference-toggle"} reference-upload-preview is-${pending.state}`;
      const image = document.createElement("img");
      image.src = pending.previewUrl;
      image.alt = pending.file.name || "待上传图片";
      image.decoding = "async";
      const status = document.createElement("span");
      status.className = "reference-upload-status";
      status.title = pending.state === "canceling" ? "正在取消" : "正在上传";
      status.innerHTML = '<i data-lucide="loader-circle"></i>';
      const cancel = document.createElement("button");
      cancel.type = "button";
      cancel.className = `reference-remove${chat ? " chat-reference-remove" : ""}`;
      cancel.dataset.cancelReferenceUpload = pending.id;
      cancel.disabled = pending.state === "canceling";
      cancel.title = pending.state === "canceling" ? "正在取消" : "取消上传";
      cancel.setAttribute("aria-label", cancel.title);
      cancel.innerHTML = '<i data-lucide="x"></i>';
      preview.append(image, status);
      card.append(preview, cancel);
      return card;
    },

    renderChatReferences() {
      const assets = this.activeWorkspace?.assets || [];
      const uploads = this.pendingReferenceUploads();
      const selection = this.currentChatSelection();
      const pickerOpen = this.chatReferencePickerOpen;
      const visibleAssets = pickerOpen
        ? assets
        : assets.filter((asset) => selection.has(asset.id));
      this.el.chatReferenceCount.textContent = selection.size;
      this.el.chatReferenceCount.hidden = selection.size === 0;
      this.el.chatReferenceStrip.hidden = !pickerOpen
        && selection.size === 0 && uploads.length === 0;
      this.el.chatReferenceStrip.classList.toggle("is-picker-open", pickerOpen);
      this.el.chatReferenceStrip.firstElementChild.textContent = pickerOpen
        ? "选择工作站图片"
        : "随消息发送";

      const upload = document.createElement("button");
      upload.type = "button";
      upload.className = "chat-reference-add";
      upload.dataset.uploadChatReference = "true";
      upload.disabled = assets.length + uploads.length >= this.limits.max_assets_per_workspace
        || this.referenceUploadPending;
      upload.title = this.referenceUploadPending
        ? "图片上传中"
        : assets.length + uploads.length >= this.limits.max_assets_per_workspace
        ? `工作站最多保留 ${this.limits.max_assets_per_workspace} 张参考图`
        : "上传参考图";
      upload.innerHTML = '<i data-lucide="image-plus"></i>';

      const library = document.createElement("button");
      library.type = "button";
      library.className = "chat-reference-add";
      library.dataset.openLibrary = "chat";
      library.title = "从图片库选择";
      library.setAttribute("aria-label", library.title);
      library.innerHTML = '<i data-lucide="library"></i>';

      const cards = visibleAssets.map((asset) => {
        const card = document.createElement("span");
        card.className = "chat-reference-item";
        const toggle = document.createElement("button");
        toggle.type = "button";
        toggle.className = `chat-reference-card${selection.has(asset.id) ? " selected" : ""}`;
        toggle.dataset.chatReferenceToggle = asset.id;
        toggle.title = selection.has(asset.id) ? `取消 ${asset.name}` : `随消息发送 ${asset.name}`;
        const image = document.createElement("img");
        image.src = asset.url;
        image.alt = asset.name;
        image.decoding = "async";
        const check = document.createElement("span");
        check.innerHTML = '<i data-lucide="check"></i>';
        toggle.append(image, check);
        const remove = document.createElement("button");
        remove.type = "button";
        remove.className = "reference-remove chat-reference-remove";
        remove.dataset.referenceRemove = asset.id;
        remove.title = `删除 ${asset.name}`;
        remove.setAttribute("aria-label", `删除 ${asset.name}`);
        remove.innerHTML = '<i data-lucide="x"></i>';
        card.append(toggle, this.librarySaveButton(asset), remove);
        return card;
      });
      const uploadCards = uploads.map((pending) => this.referenceUploadCard(pending, "chat"));
      this.el.chatReferenceList.replaceChildren(
        ...(pickerOpen ? [upload, library] : []),
        ...cards,
        ...uploadCards,
      );
      UI.icons(this.el.chatReferenceList);
    },

    async handleChatReferenceClick(event) {
      const cancelUpload = event.target.closest("[data-cancel-reference-upload]");
      if (cancelUpload) {
        this.cancelReferenceUpload(cancelUpload.dataset.cancelReferenceUpload);
        return;
      }
      const save = event.target.closest("[data-save-library-asset]");
      if (save) {
        await this.saveLibrarySource({ asset_id: save.dataset.saveLibraryAsset }, save);
        return;
      }
      if (this.workspaceChatBusy() || this.workspaceHasActiveJob()
        || this.referenceUploadPending) return;
      const library = event.target.closest("[data-open-library]");
      if (library) {
        this.openLibrary("chat");
        return;
      }
      const remove = event.target.closest("[data-reference-remove]");
      if (remove) {
        await this.removeReference(remove.dataset.referenceRemove);
        return;
      }
      const upload = event.target.closest("[data-upload-chat-reference]");
      if (upload) {
        this.openReferencePicker("chat");
        return;
      }
      const toggle = event.target.closest("[data-chat-reference-toggle]");
      if (!toggle) return;
      const selection = this.currentChatSelection();
      const id = toggle.dataset.chatReferenceToggle;
      if (selection.has(id)) selection.delete(id);
      else if (selection.size < this.limits.max_chat_attachments) selection.add(id);
      this.renderChatReferences();
    },

    renderReferences() {
      const assets = this.activeWorkspace?.assets || [];
      const uploads = this.pendingReferenceUploads();
      const selected = this.currentSelection();
      const max = this.generationReferenceLimit();
      this.el.referenceLimit.textContent = `${selected.size} / ${max}`;
      this.el.referenceAdd.disabled = assets.length + uploads.length >= this.limits.max_assets_per_workspace
        || this.referenceUploadPending;
      this.el.referenceList.replaceChildren(
        ...assets.map((asset) => {
          const card = document.createElement("div");
          card.className = `reference-card${selected.has(asset.id) ? " selected" : ""}`;
          card.dataset.assetId = asset.id;
          const toggle = document.createElement("button");
          toggle.type = "button";
          toggle.className = "reference-toggle";
          toggle.dataset.referenceToggle = asset.id;
          toggle.title = this.isAnimationWorkspace()
            ? (selected.has(asset.id) ? "取消母图" : "选择为母图")
            : (selected.has(asset.id) ? "取消选择" : "选择为垫图");
          const image = document.createElement("img");
          image.src = asset.url;
          image.alt = asset.name;
          image.decoding = "async";
          const check = document.createElement("span");
          check.innerHTML = '<i data-lucide="check"></i>';
          toggle.append(image, check);
          const remove = document.createElement("button");
          remove.type = "button";
          remove.className = "reference-remove";
          remove.dataset.referenceRemove = asset.id;
          remove.title = this.isAnimationWorkspace() ? "删除母图" : "删除垫图";
          remove.setAttribute("aria-label", remove.title);
          remove.innerHTML = '<i data-lucide="x"></i>';
          card.append(toggle, this.librarySaveButton(asset), remove);
          return card;
        }),
        ...uploads.map((pending) => this.referenceUploadCard(pending)),
      );
      UI.icons(this.el.referenceList);
      if (this.isAnimationWorkspace() && this.el.modeSwitch.dataset.mode !== "img2img") {
        this.setMode("img2img", false);
      }
      this.updateWorkspaceKindUI();
      this.updatePrice();
      this.updateInteractionState();
    },

    uploadReferences(files, target) {
      this.el.referenceInput.value = "";
      const workspace = this.activeWorkspace;
      if (!files.length || !workspace || this.referenceUploadPending) return;
      const images = files.filter((file) => (
        REFERENCE_IMAGE_TYPES.has(file.type.toLowerCase())
        || REFERENCE_IMAGE_EXTENSION.test(file.name)
      ));
      if (!images.length) {
        UI.toast("仅支持 PNG、JPEG 和 WebP 图片", "error");
        return;
      }
      if (workspace.assets.length + this.pendingReferenceUploads(workspace.id).length + images.length
        > this.limits.max_assets_per_workspace) {
        UI.toast(`每个工作站最多保留 ${this.limits.max_assets_per_workspace} 张垫图`, "error");
        return;
      }
      const maxBytes = this.limits.max_attachment_mb * 1024 * 1024;
      if (images.some((file) => file.size > maxBytes)) {
        UI.toast(`单张垫图不能超过 ${this.limits.max_attachment_mb} MiB`, "error");
        return;
      }
      const maxTotalBytes = this.limits.max_attachment_total_mb * 1024 * 1024;
      if (images.reduce((total, file) => total + file.size, 0) > maxTotalBytes) {
        UI.toast(`垫图合计不能超过 ${this.limits.max_attachment_total_mb} MiB`, "error");
        return;
      }
      const normalizedTarget = target === "chat" ? "chat" : "generation";
      images.forEach((file) => {
        const id = `reference-upload-${++this.referenceUploadSequence}`;
        this.referenceUploads.set(id, {
          id,
          workspaceId: workspace.id,
          target: normalizedTarget,
          file,
          previewUrl: URL.createObjectURL(file),
          state: "queued",
          cancelRequested: false,
        });
        this.enqueueReferenceUpload(id);
      });
      if (files.length > images.length) {
        UI.toast(`已忽略 ${files.length - images.length} 个不支持的文件`, "info");
      }
      this.refreshReferenceUploadUI(workspace.id);
    },

    enqueueReferenceUpload(id) {
      const upload = this.referenceUploads.get(id);
      if (!upload) return;
      const previous = this.referenceUploadQueues.get(upload.workspaceId) || Promise.resolve();
      const task = previous.catch(() => {}).then(() => this.processReferenceUpload(id));
      this.referenceUploadQueues.set(upload.workspaceId, task);
      const clearQueue = () => {
        if (this.referenceUploadQueues.get(upload.workspaceId) === task) {
          this.referenceUploadQueues.delete(upload.workspaceId);
        }
      };
      task.then(clearQueue, clearQueue);
    },

    async processReferenceUpload(id) {
      const upload = this.referenceUploads.get(id);
      if (!upload) return;
      const workspace = this.workspaces.find((item) => item.id === upload.workspaceId);
      if (!workspace) {
        this.finishReferenceUpload(id);
        return;
      }
      upload.state = "uploading";
      this.refreshReferenceUploadUI(workspace.id);
      const data = new FormData();
      data.append("references", upload.file, upload.file.name);
      try {
        const payload = await UI.api(`/api/workspaces/${workspace.id}/assets`, {
          method: "POST", body: data,
        });
        const asset = payload.assets?.[0];
        if (!asset) throw new Error("图片上传结果无效");
        if (upload.cancelRequested) {
          try {
            await UI.api(`/api/workspaces/${workspace.id}/assets/${asset.id}`, { method: "DELETE" });
          } catch {
            this.commitReferenceUpload(workspace, upload, asset);
            UI.toast(`${upload.file.name || "图片"}取消失败，已保留上传结果`, "error");
          }
        } else {
          this.commitReferenceUpload(workspace, upload, asset);
        }
      } catch (error) {
        if (!upload.cancelRequested) {
          UI.toast(`${upload.file.name || "图片"}：${error.message}`, "error");
        }
      } finally {
        this.finishReferenceUpload(id);
      }
    },

    commitReferenceUpload(workspace, upload, asset) {
      if (!workspace.assets.some((item) => item.id === asset.id)) workspace.assets.push(asset);
      const selection = upload.target === "chat"
        ? this.currentChatSelection(workspace.id)
        : this.currentSelection(workspace.id);
      const limit = this.referenceSelectionLimit(upload.target, workspace);
      this.trimReferenceSelection(selection, limit);
      if (selection.size < limit) selection.add(asset.id);
      if (upload.target === "generation" && this.activeWorkspace?.id === workspace.id) {
        if (workspace.kind === "animation") this.setMode("img2img", false);
        this.settingChanged();
      }
      this.renderWorkspaceList();
    },

    cancelReferenceUpload(id) {
      const upload = this.referenceUploads.get(id);
      if (!upload || upload.workspaceId !== this.activeWorkspace?.id || upload.cancelRequested) return;
      upload.cancelRequested = true;
      if (upload.state === "queued") {
        this.finishReferenceUpload(id);
        return;
      }
      upload.state = "canceling";
      this.refreshReferenceUploadUI(upload.workspaceId);
    },

    finishReferenceUpload(id) {
      const upload = this.referenceUploads.get(id);
      if (!upload) return;
      this.referenceUploads.delete(id);
      URL.revokeObjectURL(upload.previewUrl);
      this.refreshReferenceUploadUI(upload.workspaceId);
    },

    refreshReferenceUploadUI(workspaceId) {
      if (this.activeWorkspace?.id !== workspaceId) return;
      this.renderReferences();
      this.renderChatReferences();
      this.updateInteractionState();
    },

    async handleReferenceClick(event) {
      const cancelUpload = event.target.closest("[data-cancel-reference-upload]");
      if (cancelUpload) {
        this.cancelReferenceUpload(cancelUpload.dataset.cancelReferenceUpload);
        return;
      }
      const save = event.target.closest("[data-save-library-asset]");
      if (save) {
        await this.saveLibrarySource({ asset_id: save.dataset.saveLibraryAsset }, save);
        return;
      }
      if (this.referenceUploadPending) return;
      const remove = event.target.closest("[data-reference-remove]");
      if (remove) {
        await this.removeReference(remove.dataset.referenceRemove);
        return;
      }
      const toggle = event.target.closest("[data-reference-toggle]");
      if (!toggle) return;
      const id = toggle.dataset.referenceToggle;
      const selection = this.currentSelection();
      if (selection.has(id)) selection.delete(id);
      else {
        const max = this.generationReferenceLimit();
        if (selection.size >= max) {
          UI.toast(
            this.isAnimationWorkspace()
              ? "帧动画任务必须且只能选择一张母图"
              : `当前渠道最多选择 ${max} 张垫图`,
            "error",
          );
          return;
        }
        selection.add(id);
      }
      if (this.isAnimationWorkspace()) this.setMode("img2img", false);
      this.renderReferences();
      this.settingChanged();
    },

    async removeReference(id) {
      if (!this.activeWorkspace || this.workspaceChatBusy() || this.workspaceHasActiveJob()) return;
      const referenceName = this.isAnimationWorkspace() ? "母图" : "垫图";
      if (!window.confirm(`从工作站删除这张${referenceName}？历史消息和任务中的引用仍会保留。`)) return;
      const workspace = this.activeWorkspace;
      try {
        await UI.api(`/api/workspaces/${workspace.id}/assets/${id}`, { method: "DELETE" });
        workspace.assets = workspace.assets.filter((asset) => asset.id !== id);
        this.referenceSelections.get(workspace.id)?.delete(id);
        this.chatReferenceSelections.get(workspace.id)?.delete(id);
        if (this.activeWorkspace?.id === workspace.id) {
          if (workspace.kind === "animation") this.setMode("img2img", false);
          this.renderReferences();
          this.renderChatReferences();
          this.settingChanged();
        }
        this.renderWorkspaceList();
        UI.toast("参考图已删除", "success");
      } catch (error) {
        UI.toast(error.message, "error");
      }
    },

  });
})();
