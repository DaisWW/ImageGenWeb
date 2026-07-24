(() => {
  "use strict";

  const {
    StudioApp,
  } = window.ImageGenStudio;

  Object.assign(StudioApp.prototype, {
    currentChannel() {
      return this.channels.find((channel) => channel.id === this.el.channelSelect.value) || null;
    },

    renderCreativeDirectionOptions(selectedId = "auto") {
      const options = this.creativeDirections.map((direction) => {
        const option = document.createElement("option");
        option.value = direction.id;
        option.textContent = direction.label;
        option.title = direction.description || direction.label;
        return option;
      });
      this.el.creativeDirectionSelect.replaceChildren(...options);
      const valid = this.creativeDirections.some((direction) => direction.id === selectedId);
      this.el.creativeDirectionSelect.value = valid ? selectedId : "auto";
    },

    galleryCategoryCompatible(category, directionId = "auto") {
      return category.id === "auto"
        || directionId === "auto"
        || category.id === "edit-endpoint-showcase"
        || (category.direction_ids || []).includes(directionId);
    },

    renderGalleryCategoryOptions(selectedId = "auto") {
      const directionId = this.el.creativeDirectionSelect.value || "auto";
      const categories = this.galleryCategories.filter((category) => (
        this.galleryCategoryCompatible(category, directionId)
      ));
      const options = categories.map((category) => {
        const option = document.createElement("option");
        option.value = category.id;
        option.textContent = category.label;
        option.title = [category.case_range, category.description].filter(Boolean).join(" · ");
        return option;
      });
      this.el.galleryCategorySelect.replaceChildren(...options);
      const selected = categories.some((category) => category.id === selectedId);
      this.el.galleryCategorySelect.value = selected ? selectedId : "auto";
    },

    referenceSelectionLimit(target, workspace = this.activeWorkspace) {
      if (target === "chat") return this.limits.max_chat_attachments;
      const channelId = workspace?.id === this.activeWorkspace?.id
        ? this.el.channelSelect.value
        : workspace?.settings?.channel_id;
      const channel = this.channels.find((item) => item.id === channelId);
      const limit = channel?.capabilities.max_reference_images || 0;
      return limit;
    },

    trimReferenceSelection(selection, limit) {
      const removed = [...selection].slice(Math.max(0, limit));
      removed.forEach((id) => selection.delete(id));
      return removed.length;
    },

    generationReferenceLimit() {
      return this.referenceSelectionLimit("generation");
    },

    currentSelection(workspaceId = this.activeWorkspace?.id) {
      if (!workspaceId) return new Set();
      if (!this.referenceSelections.has(workspaceId)) {
        this.referenceSelections.set(workspaceId, new Set());
      }
      return this.referenceSelections.get(workspaceId);
    },

    currentChatSelection(workspaceId = this.activeWorkspace?.id) {
      if (!workspaceId) return new Set();
      if (!this.chatReferenceSelections.has(workspaceId)) {
        this.chatReferenceSelections.set(workspaceId, new Set());
      }
      return this.chatReferenceSelections.get(workspaceId);
    },

    pendingReferenceUploads(workspaceId = this.activeWorkspace?.id) {
      if (!workspaceId) return [];
      return [...this.referenceUploads.values()].filter((upload) => (
        upload.workspaceId === workspaceId
      ));
    },

  });

  Object.defineProperty(StudioApp.prototype, "referenceUploadPending", {
    configurable: true,
    get() {
      return this.pendingReferenceUploads().length > 0;
    },
  });
})();
