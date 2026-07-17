(() => {
  "use strict";

  const UI = window.ImageGen;
  const STATUS = {
    queued: ["排队中", "queued"],
    running: ["生成中", "running"],
    canceling: ["取消中", "canceling"],
    succeeded: ["已完成", "succeeded"],
    partial: ["部分完成", "partial"],
    failed: ["失败", "failed"],
    interrupted: ["已中断", "failed"],
    canceled: ["已取消", "canceled"],
  };
  const TERMINAL = new Set(["succeeded", "partial", "failed", "canceled"]);
  const ACTIVE_POLL_INTERVAL = 2200;
  const IDLE_POLL_INTERVAL = 8000;
  const COMPOSER_CLOSE_TIMEOUT = 300;
  const IMAGE_SIZE_PATTERN = /^([1-9]\d{1,4})x([1-9]\d{1,4})$/;
  const IMAGE_DIMENSION_MIN = 64;
  const IMAGE_DIMENSION_MAX = 8192;
  const REFERENCE_IMAGE_TYPES = new Set(["image/png", "image/jpeg", "image/webp"]);
  const REFERENCE_IMAGE_EXTENSION = /\.(?:png|jpe?g|webp)$/i;
  const JOB_ELEMENT_SELECTOR = [
    "[data-job-status]",
    "[data-job-status-label]",
    "[data-job-queue]",
    "[data-job-time]",
    "[data-job-eta]",
    "[data-job-retry]",
    "[data-job-cancel]",
    "[data-job-progress]",
    "[data-job-prompt]",
    "[data-job-channel]",
    "[data-job-model]",
    "[data-job-size]",
    "[data-job-quality]",
    "[data-job-count]",
    "[data-job-charge]",
    "[data-animation-result]",
    "[data-animation-image]",
    "[data-animation-meta]",
    "[data-animation-download]",
    ".output-grid",
  ].join(",");

  const setText = (element, value) => {
    const next = String(value ?? "");
    if (element.textContent !== next) element.textContent = next;
  };
  const setHidden = (element, hidden) => {
    const next = Boolean(hidden);
    if (element.hidden !== next) element.hidden = next;
  };
  const setDisabled = (element, disabled) => {
    const next = Boolean(disabled);
    if (element.disabled !== next) element.disabled = next;
  };
  const setAttribute = (element, name, value) => {
    const next = String(value);
    if (element.getAttribute(name) !== next) element.setAttribute(name, next);
  };

  class StudioApp {
    constructor() {
      this.bootstrap = JSON.parse(document.getElementById("bootstrapData").textContent);
      this.brandMarkUrl = document.getElementById("studioApp").dataset.brandMarkUrl;
      this.user = this.bootstrap.user;
      this.workspaces = this.bootstrap.workspaces;
      this.limits = this.bootstrap.runtime_settings || {
        max_workspaces_per_user: this.bootstrap.max_workspaces,
        max_assets_per_workspace: 8,
        max_message_characters: 12000,
        max_chat_attachments: 8,
        max_attachment_mb: 10,
        max_attachment_total_mb: 40,
        max_prompt_characters: 8000,
        max_batch_images: 20,
        max_animation_frames: 20,
        max_animation_fps: 24,
      };
      this.maxWorkspaces = this.limits.max_workspaces_per_user;
      this.historyRetentionDays = this.bootstrap.history_retention_days;
      this.channels = this.bootstrap.channels;
      this.chatModels = this.bootstrap.chat_models || [];
      this.creativeDirections = this.bootstrap.creative_directions || [];
      this.activeWorkspace = null;
      this.jobs = [];
      this.messages = [];
      this.conversationContext = null;
      this.referenceSelections = new Map();
      this.chatReferenceSelections = new Map();
      this.chatDrafts = new Map();
      this.chatOperations = new Map();
      this.outgoingMessages = new Map();
      this.saveTimer = null;
      this.promptCounterTimer = null;
      this.workspaceSkeletonTimer = null;
      this.composerCloseTimer = null;
      this.workspaceLoadSequence = 0;
      this.workspaceLoading = false;
      this.workspaceTransition = null;
      this.reducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)");
      this.loadingJobWorkspaces = new Map();
      this.loadingMessageWorkspaces = new Map();
      this.workspaceJobs = new Map();
      this.loadingWorkspaceJobs = null;
      this.referenceUploads = new Map();
      this.referenceUploadQueues = new Map();
      this.referenceUploadSequence = 0;
      this.dialogMode = "create";
      this.dialogWorkspaceKind = "image";
      this.workspaceDeleteId = null;
      this.draggedWorkspaceId = null;
      this.workspaceOrderSaving = false;
      this.uploadTarget = "generation";
      this.chatReferencePickerOpen = false;
      this.detailItemId = null;
      this.detailJobId = null;
      this.detailReviewSuggestion = "";
      this.sliceItemId = null;
      this.sliceAnalysis = null;
      this.sliceBoxes = [];
      this.sliceSelected = new Set();
      this.sliceBusy = false;
      this.libraryImages = null;
      this.libraryTarget = "chat";
      this.libraryLoading = false;
      this.libraryLoadError = "";
      this.libraryHasMore = false;
      this.libraryTotal = 0;
      this.libraryOffset = 0;
      this.libraryUploading = false;
      this.channelVersion = "";
      this.chatModelVersion = "";
      this.runtimeRevision = "";
      this.workspaceListSignature = "";
      this.workspaceElementCache = new WeakMap();
      this.jobElementCache = new WeakMap();
      this.polling = false;
      this.pollTimer = null;
      this.scrollFrame = null;
      this.workspaces.forEach((workspace) => {
        if (workspace.conversation_operation?.busy) {
          this.chatOperations.set(workspace.id, {
            ...workspace.conversation_operation,
            local: false,
          });
        }
      });
      this.cacheElements();
      this.renderCreativeDirectionOptions();
      this.applyRuntimeSettings(this.limits, this.historyRetentionDays);
      this.bindEvents();
      this.renderWorkspaceList();
      const lastWorkspaceId = this.loadLastWorkspaceId();
      const initialWorkspace = this.workspaces.find((workspace) => workspace.id === lastWorkspaceId)
        || this.workspaces[0];
      if (initialWorkspace) this.selectWorkspace(initialWorkspace.id);
      else this.showEmptyWorkspace();
      this.loadWorkspaceJobs().finally(() => this.schedulePoll());
      this.loadChannels(false);
      this.loadChatModels(false);
      this.countdownTimer = window.setInterval(() => {
        if (document.hidden) return;
        this.updateWorkspaceJobDisplays();
        this.updateEtaMetric();
      }, 1000);
      this.channelTimer = window.setInterval(() => {
        if (document.hidden) return;
        this.loadChannels(false);
        this.loadChatModels(false);
        this.loadRuntimeSettings(false);
      }, 15000);
    }

    cacheElements() {
      const byId = (id) => document.getElementById(id);
      this.el = {
        workspaceList: byId("workspaceList"),
        workspaceCount: byId("workspaceCount"),
        retentionSummary: byId("retentionSummary"),
        workspaceTitle: byId("workspaceTitle"),
        workspaceStatus: byId("workspaceStatus"),
        workspaceStateDot: byId("workspaceStateDot"),
        runningMetric: byId("runningMetric"),
        queueMetric: byId("queueMetric"),
        etaMetric: byId("etaMetric"),
        etaRemainingMetric: byId("etaRemainingMetric"),
        newWorkspaceButton: byId("newWorkspaceButton"),
        libraryButton: byId("libraryButton"),
        clearWorkspaceButton: byId("clearWorkspaceButton"),
        workspaceDialog: byId("workspaceDialog"),
        workspaceForm: byId("workspaceForm"),
        workspaceDialogTitle: byId("workspaceDialogTitle"),
        workspaceNameInput: byId("workspaceNameInput"),
        workspaceKindControl: byId("workspaceKindControl"),
        workspaceKindSwitch: byId("workspaceKindSwitch"),
        workspaceClearDialog: byId("workspaceClearDialog"),
        workspaceClearForm: byId("workspaceClearForm"),
        workspaceClearName: byId("workspaceClearName"),
        workspaceDeleteDialog: byId("workspaceDeleteDialog"),
        workspaceDeleteForm: byId("workspaceDeleteForm"),
        workspaceDeleteName: byId("workspaceDeleteName"),
        conversationView: byId("conversationView"),
        conversationScroll: byId("conversationScroll"),
        conversationLoading: byId("conversationLoading"),
        conversationEmpty: byId("conversationEmpty"),
        conversationEmptyLabel: byId("conversationEmptyLabel"),
        messageList: byId("messageList"),
        chatForm: byId("chatForm"),
        chatModelSelect: byId("chatModelSelect"),
        creativeDirectionSelect: byId("creativeDirectionSelect"),
        translatePrompt: byId("translatePrompt"),
        contextStatus: byId("contextStatus"),
        directGenerationButton: byId("directGenerationButton"),
        animationParametersButton: byId("animationParametersButton"),
        chatReferenceStrip: byId("chatReferenceStrip"),
        chatReferenceList: byId("chatReferenceList"),
        chatReferenceButton: byId("chatReferenceButton"),
        chatReferenceCount: byId("chatReferenceCount"),
        chatInput: byId("chatInput"),
        chatSendButton: byId("chatSendButton"),
        generationBackdrop: byId("generationBackdrop"),
        generationForm: byId("generationForm"),
        generationBackButton: byId("generationBackButton"),
        generationHeadingTitle: byId("generationHeadingTitle"),
        generationHeadingSubtitle: byId("generationHeadingSubtitle"),
        promptReviewStatus: byId("promptReviewStatus"),
        modeSwitch: byId("modeSwitch"),
        channelSelect: byId("channelSelect"),
        modelSelect: byId("modelSelect"),
        qualityStageSwitch: byId("qualityStageSwitch"),
        sizeInput: byId("sizeInput"),
        sizeOptions: byId("sizeOptions"),
        formatSelect: byId("formatSelect"),
        transparentBackground: byId("transparentBackground"),
        transparentBackgroundControl: byId("transparentBackgroundControl"),
        frameFormatLabel: byId("frameFormatLabel"),
        imageCountControl: byId("imageCountControl"),
        batchCount: byId("batchCount"),
        animationControls: [...document.querySelectorAll(".animation-control")],
        animationFrameCount: byId("animationFrameCount"),
        animationFps: byId("animationFps"),
        animationFormat: byId("animationFormat"),
        animationLoop: byId("animationLoop"),
        referenceStrip: byId("referenceStrip"),
        referenceInput: byId("referenceInput"),
        referenceAdd: byId("referenceAdd"),
        referenceLibrary: byId("referenceLibrary"),
        referenceAddLabel: byId("referenceAddLabel"),
        referenceList: byId("referenceList"),
        referenceLimit: byId("referenceLimit"),
        promptInput: byId("promptInput"),
        promptCounter: byId("promptCounter"),
        priceEstimateLabel: byId("priceEstimateLabel"),
        priceEstimate: byId("priceEstimate"),
        saveState: byId("saveState"),
        generateButton: byId("generateButton"),
        generateButtonLabel: byId("generateButtonLabel"),
        imageDialog: byId("imageDialog"),
        detailImage: byId("detailImage"),
        detailList: byId("detailList"),
        detailPrompt: byId("detailPrompt"),
        detailReferences: byId("detailReferences"),
        detailReview: byId("detailReview"),
        detailReviewVerdict: byId("detailReviewVerdict"),
        detailReviewScores: byId("detailReviewScores"),
        detailReviewChecks: byId("detailReviewChecks"),
        detailReviewSuggestion: byId("detailReviewSuggestion"),
        detailSlice: byId("detailSlice"),
        detailSaveLibrary: byId("detailSaveLibrary"),
        detailRunReview: byId("detailRunReview"),
        detailApplyReview: byId("detailApplyReview"),
        detailReuse: byId("detailReuse"),
        detailReuseLabel: byId("detailReuseLabel"),
        detailDownload: byId("detailDownload"),
        sliceDialog: byId("sliceDialog"),
        slicePreviewTitle: byId("slicePreviewTitle"),
        sliceConfidence: byId("sliceConfidence"),
        sliceCanvas: byId("sliceCanvas"),
        sliceImage: byId("sliceImage"),
        sliceOverlay: byId("sliceOverlay"),
        sliceLayoutFields: byId("sliceLayoutFields"),
        sliceRows: byId("sliceRows"),
        sliceColumns: byId("sliceColumns"),
        sliceSelectionSummary: byId("sliceSelectionSummary"),
        sliceReset: byId("sliceReset"),
        sliceSelectAll: byId("sliceSelectAll"),
        sliceClearSelection: byId("sliceClearSelection"),
        sliceList: byId("sliceList"),
        sliceSaveLibrary: byId("sliceSaveLibrary"),
        sliceReuse: byId("sliceReuse"),
        sliceDownload: byId("sliceDownload"),
        libraryDialog: byId("libraryDialog"),
        libraryTargetLabel: byId("libraryTargetLabel"),
        libraryUploadButton: byId("libraryUploadButton"),
        libraryInput: byId("libraryInput"),
        libraryDropArea: byId("libraryDropArea"),
        libraryLoading: byId("libraryLoading"),
        libraryError: byId("libraryError"),
        libraryRetryButton: byId("libraryRetryButton"),
        libraryEmpty: byId("libraryEmpty"),
        libraryGrid: byId("libraryGrid"),
        libraryPagination: byId("libraryPagination"),
        libraryLoadMoreButton: byId("libraryLoadMoreButton"),
      };
    }

    applyRuntimeSettings(settings, historyRetentionDays) {
      this.limits = { ...this.limits, ...(settings || {}) };
      this.maxWorkspaces = this.limits.max_workspaces_per_user;
      if (Number.isFinite(Number(historyRetentionDays))) {
        this.historyRetentionDays = Number(historyRetentionDays);
      }
      this.el.retentionSummary.textContent = `生成记录保留 ${this.historyRetentionDays} 天`;
      this.el.chatInput.maxLength = this.limits.max_message_characters;
      this.el.promptInput.maxLength = this.limits.max_prompt_characters;
      this.el.batchCount.max = this.limits.max_batch_images;
      this.el.animationFrameCount.max = this.limits.max_animation_frames;
      this.el.animationFps.max = this.limits.max_animation_fps;
      for (const selection of this.chatReferenceSelections.values()) {
        this.trimReferenceSelection(selection, this.limits.max_chat_attachments);
      }
      this.renderWorkspaceList();
      if (this.activeWorkspace) {
        this.renderReferences();
        this.renderChatReferences();
        this.updateInteractionState();
      }
    }

    async loadRuntimeSettings(notify = true) {
      try {
        const data = await UI.api("/api/runtime-settings");
        const revision = data.revision || "";
        if (revision && revision === this.runtimeRevision) return;
        this.runtimeRevision = revision;
        this.applyRuntimeSettings(data.settings, data.history_retention_days);
      } catch (error) {
        if (notify) UI.toast(error.message, "error");
      }
    }

    bindEvents() {
      this.el.newWorkspaceButton.addEventListener("click", () => this.showWorkspaceDialog("create"));
      this.el.libraryButton.addEventListener("click", () => this.openLibrary());
      this.el.clearWorkspaceButton.addEventListener("click", () => this.requestClearWorkspace());
      this.el.workspaceForm.addEventListener("submit", (event) => this.saveWorkspaceName(event));
      this.el.workspaceClearForm.addEventListener("submit", (event) => this.clearWorkspace(event));
      this.el.workspaceDeleteForm.addEventListener("submit", (event) => this.deleteWorkspace(event));
      this.el.workspaceDeleteDialog.addEventListener("close", () => {
        this.workspaceDeleteId = null;
      });
      this.el.workspaceKindSwitch.addEventListener("click", (event) => {
        const button = event.target.closest("[data-workspace-kind]");
        if (button) this.setDialogWorkspaceKind(button.dataset.workspaceKind);
      });
      this.el.workspaceList.addEventListener("click", (event) => this.handleWorkspaceListClick(event));
      this.el.workspaceList.addEventListener("dblclick", (event) => this.handleWorkspaceDoubleClick(event));
      this.el.workspaceList.addEventListener("dragstart", (event) => this.handleWorkspaceDragStart(event));
      this.el.workspaceList.addEventListener("dragover", (event) => this.handleWorkspaceDragOver(event));
      this.el.workspaceList.addEventListener("drop", (event) => this.handleWorkspaceDrop(event));
      this.el.workspaceList.addEventListener("dragend", () => this.clearWorkspaceDragState());
      document.addEventListener("keydown", (event) => this.handleWorkspaceShortcut(event));
      document.addEventListener("keydown", (event) => {
        if (event.key === "Escape" && !this.el.generationForm.hidden
          && !document.querySelector("dialog[open]")) this.setComposerMode("chat");
      });
      this.el.chatForm.addEventListener("submit", (event) => this.sendChatMessage(event));
      this.el.chatInput.addEventListener("keydown", (event) => {
        if (event.key === "Enter" && !event.shiftKey && !event.isComposing) {
          event.preventDefault();
          this.el.chatForm.requestSubmit();
        }
      });
      this.el.chatInput.addEventListener("paste", (event) => this.handleChatPaste(event));
      this.el.chatForm.addEventListener("dragenter", (event) => this.handleChatDrag(event));
      this.el.chatForm.addEventListener("dragover", (event) => this.handleChatDrag(event));
      this.el.chatForm.addEventListener("dragleave", (event) => this.handleChatDragLeave(event));
      this.el.chatForm.addEventListener("drop", (event) => this.handleChatDrop(event));
      this.el.chatModelSelect.addEventListener("change", () => this.settingChanged());
      this.el.creativeDirectionSelect.addEventListener("change", () => {
        this.settingChanged();
        this.updatePromptReviewState();
      });
      this.el.translatePrompt.addEventListener("change", () => this.settingChanged());
      this.el.directGenerationButton.addEventListener("click", () => this.openGenerationComposer());
      this.el.animationParametersButton.addEventListener("click", () => this.openGenerationComposer());
      this.el.chatReferenceButton.addEventListener("click", () => this.toggleChatReferences());
      this.el.chatReferenceList.addEventListener("click", (event) => this.handleChatReferenceClick(event));
      this.el.messageList.addEventListener("click", (event) => {
        const retrySendButton = event.target.closest("[data-retry-send]");
        if (retrySendButton) {
          this.retryFailedChatMessage(retrySendButton.dataset.retrySend);
          return;
        }
        const retryButton = event.target.closest("[data-retry-message]");
        if (retryButton) {
          this.retryChatMessage(retryButton.dataset.retryMessage);
          return;
        }
        const button = event.target.closest("[data-use-prompt-draft]");
        if (button) {
          this.applyPromptDraft(button.dataset.usePromptDraft);
          return;
        }
        this.handleJobClick(event);
      });
      this.el.generationBackdrop.addEventListener("click", () => this.setComposerMode("chat"));
      this.el.generationBackButton.addEventListener("click", () => this.setComposerMode("chat"));
      this.el.generationForm.addEventListener("animationend", (event) => {
        this.finishComposerClose(event);
      });
      this.el.generationForm.addEventListener("animationcancel", (event) => {
        this.finishComposerClose(event);
      });
      this.el.modeSwitch.addEventListener("click", (event) => {
        const button = event.target.closest("[data-mode]");
        if (button && !button.disabled) this.setMode(button.dataset.mode, true);
      });
      this.el.qualityStageSwitch.addEventListener("click", (event) => {
        const button = event.target.closest("[data-generation-stage]");
        if (button) this.setGenerationStage(button.dataset.generationStage, true);
      });
      this.el.channelSelect.addEventListener("change", () => {
        this.applyChannel(null, true);
      });
      this.el.modelSelect.addEventListener("change", () => this.settingChanged());
      this.el.formatSelect.addEventListener("change", () => {
        this.updateTransparentBackgroundState();
        this.settingChanged();
      });
      this.el.transparentBackground.addEventListener("change", () => this.settingChanged());
      this.el.sizeInput.addEventListener("input", () => this.el.sizeInput.setCustomValidity(""));
      this.el.sizeInput.addEventListener("change", () => {
        if (this.validateSizeInput(true)) this.settingChanged();
      });
      this.el.batchCount.addEventListener("input", () => {
        this.updatePrice();
        this.settingChanged();
      });
      [this.el.animationFrameCount, this.el.animationFps].forEach((field) => {
        field.addEventListener("input", () => {
          this.updatePrice();
          this.settingChanged();
        });
      });
      [this.el.animationFormat, this.el.animationLoop].forEach((field) => {
        field.addEventListener("change", () => this.settingChanged());
      });
      this.el.promptInput.addEventListener("input", () => {
        window.clearTimeout(this.promptCounterTimer);
        this.promptCounterTimer = window.setTimeout(() => {
          this.updatePromptCounter();
        }, 120);
        this.settingChanged();
        this.updatePromptReviewState();
      });
      this.el.referenceAdd.addEventListener("click", () => this.openReferencePicker("generation"));
      this.el.referenceLibrary.addEventListener("click", () => this.openLibrary("generation"));
      this.el.referenceInput.addEventListener("change", () => {
        this.uploadReferences([...this.el.referenceInput.files], this.uploadTarget);
      });
      this.el.referenceList.addEventListener("click", (event) => this.handleReferenceClick(event));
      this.el.generationForm.addEventListener("submit", (event) => this.submitGeneration(event));
      this.el.detailSlice.addEventListener("click", () => this.openSliceTool());
      this.el.detailSaveLibrary.addEventListener("click", () => this.saveDetailToLibrary());
      this.el.detailRunReview.addEventListener("click", () => this.runDetailReview());
      this.el.detailApplyReview.addEventListener("click", () => {
        this.reuseDetailImage(this.detailReviewSuggestion);
      });
      this.el.detailReuse.addEventListener("click", () => this.reuseDetailImage());
      this.el.sliceLayoutFields.addEventListener("input", () => this.rebuildSliceGrid());
      this.el.sliceOverlay.addEventListener("click", (event) => this.handleSliceSelection(event));
      this.el.sliceList.addEventListener("click", (event) => this.handleSliceSelection(event));
      this.el.sliceReset.addEventListener("click", () => this.applySliceAnalysis());
      this.el.sliceSelectAll.addEventListener("click", () => {
        this.sliceSelected = new Set(this.sliceBoxes.map((_box, index) => index));
        this.renderSlices();
      });
      this.el.sliceClearSelection.addEventListener("click", () => {
        this.sliceSelected.clear();
        this.renderSlices();
      });
      this.el.sliceSaveLibrary.addEventListener("click", () => this.exportSlices("library"));
      this.el.sliceReuse.addEventListener("click", () => this.exportSlices("reference"));
      this.el.sliceDownload.addEventListener("click", () => this.exportSlices("download"));
      this.el.sliceDialog.addEventListener("close", () => {
        this.sliceItemId = null;
        this.sliceAnalysis = null;
        this.sliceBoxes = [];
        this.sliceSelected.clear();
        this.el.sliceImage.removeAttribute("src");
      });
      this.el.libraryUploadButton.addEventListener("click", () => this.el.libraryInput.click());
      this.el.libraryInput.addEventListener("change", () => {
        this.uploadLibraryImages([...this.el.libraryInput.files]);
      });
      this.el.libraryGrid.addEventListener("click", (event) => this.handleLibraryClick(event));
      this.el.libraryRetryButton.addEventListener("click", () => this.loadLibraryImages());
      this.el.libraryLoadMoreButton.addEventListener("click", () => {
        this.loadLibraryImages({ append: true });
      });
      this.el.libraryDropArea.addEventListener("dragover", (event) => this.handleLibraryDrag(event));
      this.el.libraryDropArea.addEventListener("drop", (event) => this.handleLibraryDrop(event));
      document.addEventListener("visibilitychange", () => {
        if (document.hidden) return;
        this.updateWorkspaceJobDisplays();
        this.updateEtaMetric();
        this.schedulePoll(0);
        this.loadChannels(false);
        this.loadChatModels(false);
        this.loadRuntimeSettings(false);
      });
    }

  }

  window.ImageGenStudio = {
    UI,
    STATUS,
    TERMINAL,
    ACTIVE_POLL_INTERVAL,
    IDLE_POLL_INTERVAL,
    COMPOSER_CLOSE_TIMEOUT,
    IMAGE_SIZE_PATTERN,
    IMAGE_DIMENSION_MIN,
    IMAGE_DIMENSION_MAX,
    REFERENCE_IMAGE_TYPES,
    REFERENCE_IMAGE_EXTENSION,
    JOB_ELEMENT_SELECTOR,
    setText,
    setHidden,
    setDisabled,
    setAttribute,
    StudioApp,
  };
})();
