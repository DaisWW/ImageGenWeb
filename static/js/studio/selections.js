(() => {
  "use strict";

  const {
    StudioApp,
    IMAGE_SIZE_PATTERN,
    IMAGE_DIMENSION_MIN,
    IMAGE_DIMENSION_MAX,
  } = window.ImageGenStudio;

  Object.assign(StudioApp.prototype, {
    routingChannels() {
      return [...(this.channels || [])]
        .filter((channel) => channel.enabled !== false && channel.configured !== false)
        .sort((left, right) => (
          Number(left.priority ?? 100) - Number(right.priority ?? 100)
        ));
    },

    generationRoutingCandidates(settings = {}, workspaceId = this.activeWorkspace?.id) {
      const channelId = String(settings.channel_id || "").trim();
      const modelId = String(settings.model || "").trim();
      const mode = String(settings.mode || "").trim();
      const outputFormat = String(settings.output_format || "").trim();
      const workspace = this.workspaces?.find((item) => item.id === workspaceId)
        || this.activeWorkspace;
      const selection = this.currentSelection(workspaceId);
      const referenceCount = mode === "img2img" ? selection.size : 0;
      const size = String(
        settings.size || workspace?.settings?.size || "1024x1024",
      ).trim().toLowerCase().replaceAll("×", "x");
      const references = mode === "img2img"
        ? [...selection].map((id) => (
          workspace?.assets?.find((asset) => asset.id === id)
        )).filter(Boolean)
        : [];
      const referenceBytes = references.map((asset) => Number(asset.bytes || 0));
      const referencesResolved = mode !== "img2img" || references.length === referenceCount;
      const maxAttachmentBytes = Number(this.limits?.max_attachment_mb || 0) * 1024 * 1024;
      const maxAttachmentTotalBytes = Number(this.limits?.max_attachment_total_mb || 0) * 1024 * 1024;
      const referencesWithinRuntimeLimits = !references.length || (
        maxAttachmentBytes > 0
        && maxAttachmentTotalBytes > 0
        && referenceBytes.every((bytes) => Number.isFinite(bytes) && bytes <= maxAttachmentBytes)
        && referenceBytes.reduce((total, bytes) => total + bytes, 0) <= maxAttachmentTotalBytes
      );
      const sizeIsValid = !size || (() => {
        const match = IMAGE_SIZE_PATTERN.exec(size);
        return Boolean(match)
          && [Number(match[1]), Number(match[2])]
            .every((dimension) => dimension >= IMAGE_DIMENSION_MIN && dimension <= IMAGE_DIMENSION_MAX);
      })();
      if (!referencesResolved || !referencesWithinRuntimeLimits || !sizeIsValid) return [];
      if ((mode === "img2img" && referenceCount === 0)
        || (mode === "text2img" && referenceCount > 0)) return [];
      return this.routingChannels().filter((channel) => {
        if (channelId && channelId !== "__auto__" && channel.id !== channelId) return false;
        if (modelId && !(channel.models || []).some((model) => model.id === modelId)) {
          return false;
        }
        if (mode && !(channel.capabilities?.modes || []).includes(mode)) return false;
        if (
          outputFormat
          && !(channel.capabilities?.formats || []).includes(outputFormat)
        ) return false;
        if (
          referenceCount > Number(channel.capabilities?.max_reference_images || 0)
        ) return false;
        const maxImageBytes = Number(channel.capabilities?.max_reference_image_mb || 0) * 1024 * 1024;
        const maxTotalBytes = Number(channel.capabilities?.max_reference_total_mb || 0) * 1024 * 1024;
        if (references.length && (
          maxImageBytes <= 0
          || maxTotalBytes <= 0
          || referenceBytes.some((bytes) => bytes > maxImageBytes)
          || referenceBytes.reduce((total, bytes) => total + bytes, 0) > maxTotalBytes
        )) return false;
        return true;
      });
    },

    routingProfile(modelId = "") {
      const allChannels = this.routingChannels();
      const normalizedModelId = String(modelId || "").trim();
      const channels = normalizedModelId
        ? allChannels.filter((channel) => (
          (channel.models || []).some((model) => model.id === normalizedModelId)
        ))
        : allChannels;
      if (!channels.length) return null;
      if (channels.length === 1) return channels[0];
      const models = [];
      const modelIds = new Set();
      const modes = new Set();
      const formats = new Set();
      let maxReferenceImages = 0;
      let maxReferenceImageMb = 0;
      let maxReferenceTotalMb = 0;
      channels.forEach((channel) => {
        (channel.models || []).forEach((model) => {
          if (!modelIds.has(model.id)) {
            modelIds.add(model.id);
            models.push(model);
          }
        });
        (channel.capabilities?.modes || []).forEach((mode) => modes.add(mode));
        (channel.capabilities?.formats || []).forEach((format) => formats.add(format));
        maxReferenceImages = Math.max(
          maxReferenceImages,
          Number(channel.capabilities?.max_reference_images || 0),
        );
        maxReferenceImageMb = Math.max(
          maxReferenceImageMb,
          Number(channel.capabilities?.max_reference_image_mb || 0),
        );
        maxReferenceTotalMb = Math.max(
          maxReferenceTotalMb,
          Number(channel.capabilities?.max_reference_total_mb || 0),
        );
      });
      return {
        id: "__auto__",
        label: "系统自动调度",
        configured: true,
        price_rmb: Math.max(...channels.map((channel) => Number(channel.price_rmb || 0))),
        models,
        capabilities: {
          modes: [...modes],
          formats: [...formats],
          max_reference_images: maxReferenceImages,
          max_reference_image_mb: maxReferenceImageMb,
          max_reference_total_mb: maxReferenceTotalMb,
        },
        limits: {
          max_concurrency: channels.reduce(
            (total, channel) => total + Number(channel.limits?.max_concurrency || 0),
            0,
          ),
        },
      };
    },

    currentChannel() {
      return this.routingProfile(this.el?.modelSelect?.value)
        || this.routingProfile()
        || null;
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
        ? this.el?.channelSelect?.value
        : workspace?.settings?.channel_id;
      const channel = this.channels.find((item) => item.id === channelId);
      if (channel) return channel.capabilities.max_reference_images || 0;
      const modelId = workspace?.id === this.activeWorkspace?.id
        ? this.el?.modelSelect?.value
        : workspace?.settings?.model;
      const profile = this.routingProfile(modelId) || this.routingProfile();
      return profile?.capabilities?.max_reference_images || 0;
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
