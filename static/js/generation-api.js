(() => {
  "use strict";

  const form = document.getElementById("generationApiForm");
  if (!form) return;

  const { api, toast } = window.ImageGen;
  const submit = form.querySelector('[type="submit"]');
  const revoke = document.getElementById("revokeGenerationApi");
  const status = document.getElementById("generationApiStatus");
  const rotationNote = document.getElementById("generationApiRotationNote");
  const secret = document.getElementById("generationApiSecret");
  const token = document.getElementById("generationApiToken");

  function updateKey(enabled, value = "") {
    status.dataset.enabled = String(enabled);
    status.textContent = enabled ? "已开通" : "未开通";
    submit.textContent = enabled ? "更换密钥" : "开通并生成密钥";
    revoke.hidden = !enabled;
    rotationNote.hidden = !enabled;
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

  document.getElementById("copyGenerationApiKey").addEventListener("click", async () => {
    try {
      await navigator.clipboard.writeText(token.value);
      toast("已复制密钥", "success");
    } catch (_error) {
      token.select();
      toast("密钥已选中，请手动复制");
    }
  });

  window.addEventListener("pagehide", () => {
    token.value = "";
    secret.hidden = true;
  });
})();
