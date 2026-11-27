(() => {
  "use strict";

  const form = document.getElementById("generationApiForm");
  if (!form) return;

  const { api, toast } = window.ImageGen;
  const submit = form.querySelector('[type="submit"]');
  const revoke = document.getElementById("revokeGenerationApi");
  const status = document.getElementById("generationApiStatus");
  const rotationNote = document.getElementById("generationApiRotationNote");
  const unavailable = document.getElementById("generationApiUnavailable");
  const secret = document.getElementById("generationApiSecret");
  const token = document.getElementById("generationApiToken");
  const copy = document.getElementById("copyGenerationApiKey");
  const skillForm = document.getElementById("generationApiSkillForm");
  const download = skillForm.querySelector('[type="submit"]');
  const configuredSkill = document.getElementById("generationApiConfiguredSkill");
  const genericSkill = document.getElementById("generationApiGenericSkill");

  function setActionsDisabled(disabled) {
    submit.disabled = disabled;
    revoke.disabled = disabled;
    download.disabled = disabled;
  }

  function updateKey(enabled, value = "") {
    status.dataset.enabled = String(enabled);
    status.textContent = enabled ? "已开通" : "未开通";
    submit.textContent = enabled ? "更换密钥" : "开通并生成密钥";
    revoke.hidden = !enabled;
    rotationNote.hidden = !enabled;
    unavailable.hidden = !enabled || Boolean(value);
    copy.hidden = false;
    token.removeAttribute("value");
    token.value = value;
    secret.hidden = !value;
    configuredSkill.hidden = !value;
    genericSkill.hidden = Boolean(value);
  }

  async function changeKey(method) {
    setActionsDisabled(true);
    try {
      const payload = await api(form.action, { method });
      updateKey(method === "POST", payload?.api_key);
    } catch (error) {
      toast(error.message, "error");
    } finally {
      setActionsDisabled(false);
    }
  }

  async function downloadSkill(event) {
    event.preventDefault();
    setActionsDisabled(true);
    toast("正在准备 Skill ZIP");
    try {
      const response = await fetch(skillForm.action, {
        cache: "no-store",
        redirect: "manual",
        signal: AbortSignal.timeout(30_000),
      });
      if (response.type === "opaqueredirect" || response.status === 401) {
        toast("登录已过期，请重新登录后下载", "error");
        return;
      }
      const filename = response.headers.get("content-disposition")?.match(/\bfilename="?([^";]+)"?/i)?.[1];
      if (!response.ok || response.headers.get("content-type") !== "application/zip" || !filename) {
        throw new Error("Invalid Skill ZIP response");
      }
      const url = URL.createObjectURL(await response.blob());
      const link = document.createElement("a");
      link.href = url;
      link.download = filename;
      document.body.append(link);
      link.click();
      link.remove();
      window.setTimeout(() => URL.revokeObjectURL(url), 1000);
      toast(`已发起下载 ${filename}，请在浏览器下载列表查看`, "success");
    } catch (_error) {
      toast("Skill 下载失败，请重试", "error");
    } finally {
      setActionsDisabled(false);
    }
  }

  form.addEventListener("submit", (event) => {
    event.preventDefault();
    changeKey("POST");
  });
  revoke.addEventListener("click", () => changeKey("DELETE"));
  skillForm.addEventListener("submit", downloadSkill);

  copy.addEventListener("click", async () => {
    const value = token.value;
    if (!value) return;
    try {
      await navigator.clipboard.writeText(value);
    } catch (_error) {
      if (token.value !== value) return;
      token.focus();
      token.select();
      let copied = false;
      try {
        copied = document.execCommand("copy");
      } catch (_legacyError) {
        // Both clipboard methods can be blocked by the browser.
      }
      if (!copied) {
        copy.hidden = true;
        toast("请手动复制选中的密钥", "error");
        return;
      }
    }
    toast("已复制密钥", "success");
  });

  window.addEventListener("pagehide", () => {
    token.removeAttribute("value");
    token.value = "";
    secret.hidden = true;
  });

  window.addEventListener("pageshow", (event) => {
    if (event.persisted) window.location.reload();
  });
})();
