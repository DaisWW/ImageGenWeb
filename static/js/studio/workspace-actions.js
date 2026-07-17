(() => {
  "use strict";

  const {
    StudioApp,
    UI,
    setAttribute,
  } = window.ImageGenStudio;

  Object.assign(StudioApp.prototype, {
    showWorkspaceDialog(mode) {
      this.dialogMode = mode;
      this.el.workspaceDialogTitle.textContent = mode === "create" ? "新建工作站" : "重命名工作站";
      this.el.workspaceNameInput.value = mode === "rename"
        ? this.activeWorkspace?.name || ""
        : this.nextWorkspaceName();
      this.el.workspaceKindControl.hidden = mode !== "create";
      if (mode === "create") this.setDialogWorkspaceKind("image");
      UI.openDialog(this.el.workspaceDialog);
      this.el.workspaceNameInput.focus();
      if (mode === "create") this.el.workspaceNameInput.select();
    },

    nextWorkspaceName() {
      const now = new Date();
      const pad = (value) => String(value).padStart(2, "0");
      const base = `工作站-${now.getFullYear()}-${pad(now.getMonth() + 1)}-${pad(now.getDate())}`;
      const names = new Set(this.workspaces.map((workspace) => workspace.name));
      if (!names.has(base)) return base;
      let index = 2;
      while (names.has(`${base} ${index}`)) index += 1;
      return `${base} ${index}`;
    },

    setDialogWorkspaceKind(kind) {
      this.dialogWorkspaceKind = kind === "animation" ? "animation" : "image";
      this.el.workspaceKindSwitch.dataset.kind = this.dialogWorkspaceKind;
      this.el.workspaceKindSwitch.querySelectorAll("[data-workspace-kind]").forEach((button) => {
        const active = button.dataset.workspaceKind === this.dialogWorkspaceKind;
        button.classList.toggle("active", active);
        button.setAttribute("aria-pressed", String(active));
      });
    },

    async saveWorkspaceName(event) {
      event.preventDefault();
      const name = this.el.workspaceNameInput.value.trim();
      const submit = this.el.workspaceForm.querySelector('[type="submit"]');
      submit.disabled = true;
      try {
        if (this.dialogMode === "create") {
          const data = await UI.api("/api/workspaces", {
            method: "POST",
            body: { name, kind: this.dialogWorkspaceKind },
          });
          this.workspaces.unshift(data.workspace);
          UI.closeDialog(this.el.workspaceDialog);
          this.activeWorkspace = null;
          await this.selectWorkspace(data.workspace.id, { knownEmpty: true });
          UI.toast("工作站已创建", "success");
        } else {
          const data = await UI.api(`/api/workspaces/${this.activeWorkspace.id}`, {
            method: "PATCH", body: { name },
          });
          Object.assign(this.activeWorkspace, data.workspace);
          this.el.workspaceTitle.textContent = data.workspace.name;
          this.renderWorkspaceList();
          UI.closeDialog(this.el.workspaceDialog);
          UI.toast("工作站已重命名", "success");
        }
      } catch (error) {
        UI.toast(error.message, "error");
      } finally {
        submit.disabled = false;
      }
    },

    async requestDeleteWorkspace(id) {
      const workspace = this.workspaces.find((item) => item.id === id);
      if (!workspace) return;
      await this.selectWorkspace(id);
      if (this.activeWorkspace?.id !== id) return;
      if (this.referenceUploadPending) {
        UI.toast("请先等待图片上传完成或取消上传", "error");
        return;
      }
      if (this.workspaceHasActiveJob() || this.workspaceChatBusy(id)) {
        UI.toast("请等待当前任务完成后再删除工作站", "error");
        return;
      }
      this.workspaceDeleteId = id;
      this.el.workspaceDeleteName.textContent = workspace.name;
      UI.openDialog(this.el.workspaceDeleteDialog);
    },

    async deleteWorkspace(event) {
      event.preventDefault();
      const workspaceId = this.workspaceDeleteId;
      const index = this.workspaces.findIndex((item) => item.id === workspaceId);
      if (index < 0) {
        UI.closeDialog(this.el.workspaceDeleteDialog);
        return;
      }
      const submit = this.el.workspaceDeleteForm.querySelector('[type="submit"]');
      submit.disabled = true;
      try {
        await this.flushSettings();
        await UI.api(`/api/workspaces/${workspaceId}`, { method: "DELETE" });
        this.referenceSelections.delete(workspaceId);
        this.chatReferenceSelections.delete(workspaceId);
        this.chatDrafts.delete(workspaceId);
        this.chatOperations.delete(workspaceId);
        this.workspaceJobs.delete(workspaceId);
        this.clearOutgoingMessages(workspaceId);
        this.workspaces.splice(index, 1);
        this.activeWorkspace = null;
        UI.closeDialog(this.el.workspaceDeleteDialog);
        const nextWorkspace = this.workspaces[Math.max(0, index - 1)];
        if (nextWorkspace) await this.selectWorkspace(nextWorkspace.id);
        else this.showEmptyWorkspace();
        UI.toast("工作站已删除", "success");
      } catch (error) {
        UI.toast(error.message, "error");
      } finally {
        submit.disabled = false;
      }
    },

    requestClearWorkspace() {
      if (!this.activeWorkspace || this.workspaceHasActiveJob()
        || this.workspaceChatBusy() || this.referenceUploadPending) {
        UI.toast("当前任务完成前不能清空会话", "error");
        return;
      }
      this.el.workspaceClearName.textContent = this.activeWorkspace.name;
      UI.openDialog(this.el.workspaceClearDialog);
    },

    async clearWorkspace(event) {
      event.preventDefault();
      const workspace = this.activeWorkspace;
      if (!workspace || this.workspaceHasActiveJob()
        || this.workspaceChatBusy() || this.referenceUploadPending) {
        UI.closeDialog(this.el.workspaceClearDialog);
        UI.toast("当前任务完成前不能清空会话", "error");
        return;
      }
      const submit = this.el.workspaceClearForm.querySelector('[type="submit"]');
      submit.disabled = true;
      try {
        const data = await UI.api(`/api/workspaces/${workspace.id}/clear`, {
          method: "POST",
        });
        Object.assign(workspace, data.workspace);
        this.messages = [];
        this.jobs = [];
        this.conversationContext = null;
        this.referenceSelections.set(workspace.id, new Set());
        this.chatReferenceSelections.set(workspace.id, new Set());
        this.chatDrafts.set(workspace.id, "");
        this.clearOutgoingMessages(workspace.id);
        this.chatReferencePickerOpen = false;
        this.applyWorkspaceSettings();
        this.renderReferences();
        this.renderChatReferences();
        this.renderWorkspaceList();
        this.renderMessages();
        this.updateMetrics();
        this.setComposerMode("chat");
        UI.closeDialog(this.el.workspaceClearDialog);
        UI.toast("当前会话已清空", "success");
      } catch (error) {
        UI.toast(error.message, "error");
      } finally {
        submit.disabled = false;
        this.updateInteractionState();
      }
    },

  });
})();
