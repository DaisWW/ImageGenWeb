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
  }

  async function changeKey(method) {
    submit.disabled = true;
    revoke.disabled = true;
    try {
      const payload = await api(form.action, { method });
      updateKey(method === "POST", payload?.api_key);
    } catch (error) {
      toast(error.message, "error");
    } finally {
      submit.disabled = false;
      revoke.disabled = false;
    }
  }

  form.addEventListener("submit", (event) => {
    event.preventDefault();
    changeKey("POST");
  });
  revoke.addEventListener("click", () => changeKey("DELETE"));

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
