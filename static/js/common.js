(() => {
  "use strict";

  const csrfToken = document.querySelector('meta[name="csrf-token"]')?.content || "";

  async function api(url, options = {}) {
    const request = { credentials: "same-origin", ...options };
    const headers = new Headers(request.headers || {});
    headers.set("Accept", "application/json");
    if (csrfToken && !["GET", "HEAD", "OPTIONS"].includes((request.method || "GET").toUpperCase())) {
      headers.set("X-CSRFToken", csrfToken);
    }
    if (request.body && !(request.body instanceof FormData) && typeof request.body !== "string") {
      headers.set("Content-Type", "application/json");
      request.body = JSON.stringify(request.body);
    }
    request.headers = headers;

    const response = await fetch(url, request);
    const contentType = response.headers.get("content-type") || "";
    const payload = contentType.includes("application/json") ? await response.json() : null;
    if (!response.ok) {
      const error = new Error(payload?.error || `请求失败（HTTP ${response.status}）`);
      error.code = payload?.code || "request_failed";
      error.status = response.status;
      throw error;
    }
    return payload;
  }

  function toast(message, tone = "info") {
    const region = document.getElementById("toastRegion");
    if (!region || !message) return;
    const node = document.createElement("div");
    node.className = `toast ${tone}`;
    const icon = document.createElement("i");
    icon.dataset.lucide = tone === "error" ? "circle-alert" : tone === "success" ? "circle-check" : "info";
    const text = document.createElement("span");
    text.textContent = message;
    node.append(icon, text);
    region.append(node);
    icons(node);
    requestAnimationFrame(() => node.classList.add("visible"));
    window.setTimeout(() => {
      node.classList.remove("visible");
      window.setTimeout(() => node.remove(), 220);
    }, tone === "error" ? 5200 : 3200);
  }

  function icons(root = document) {
    if (window.lucide) window.lucide.createIcons({ root });
  }

  function amount(value) {
    const numeric = Number(value || 0);
    if (!Number.isFinite(numeric)) return "0.00";
    const [whole, fraction] = numeric.toFixed(4).split(".");
    return `${whole}.${fraction.replace(/0+$/, "").padEnd(2, "0")}`;
  }

  function money(value) {
    return `¥${amount(value)}`;
  }

  function updateWallet(user, spending = {}) {
    const values = {
      "[data-wallet-balance]": user?.available_balance_rmb,
      "[data-wallet-today]": spending.today_rmb,
      "[data-wallet-total]": spending.total_rmb,
    };
    Object.entries(values).forEach(([selector, value]) => {
      document.querySelectorAll(selector).forEach((node) => {
        node.textContent = money(value);
      });
    });
  }

  function dateTime(value) {
    if (!value) return "--";
    const date = new Date(value);
    return Number.isNaN(date.getTime())
      ? "--"
      : new Intl.DateTimeFormat("zh-CN", {
          month: "2-digit",
          day: "2-digit",
          hour: "2-digit",
          minute: "2-digit",
          second: "2-digit",
          hour12: false,
        }).format(date);
  }

  function timeOnly(value) {
    if (!value) return "--:--";
    const date = new Date(value);
    return Number.isNaN(date.getTime())
      ? "--:--"
      : new Intl.DateTimeFormat("zh-CN", { hour: "2-digit", minute: "2-digit", hour12: false }).format(date);
  }

  function formatBytes(value) {
    const bytes = Number(value || 0);
    if (bytes >= 1024 * 1024) return `${(bytes / 1024 / 1024).toFixed(1)} MiB`;
    if (bytes >= 1024) return `${Math.round(bytes / 1024)} KiB`;
    return `${bytes} B`;
  }

  function escapeHtml(value) {
    return String(value ?? "")
      .replaceAll("&", "&amp;")
      .replaceAll("<", "&lt;")
      .replaceAll(">", "&gt;")
      .replaceAll('"', "&quot;")
      .replaceAll("'", "&#039;");
  }

  function openDialog(id) {
    const dialog = typeof id === "string" ? document.getElementById(id) : id;
    if (dialog && !dialog.open) dialog.showModal();
  }

  function closeDialog(id) {
    const dialog = typeof id === "string" ? document.getElementById(id) : id;
    if (dialog?.open) dialog.close();
  }

  window.ImageGen = {
    amount,
    api,
    closeDialog,
    dateTime,
    escapeHtml,
    formatBytes,
    icons,
    money,
    openDialog,
    timeOnly,
    toast,
    updateWallet,
  };

  document.addEventListener("DOMContentLoaded", () => {
    icons();

    document.querySelectorAll("[data-close-dialog]").forEach((button) => {
      button.addEventListener("click", () => closeDialog(button.dataset.closeDialog));
    });
    document.querySelectorAll("dialog").forEach((dialog) => {
      dialog.addEventListener("click", (event) => {
        if (event.target === dialog && !dialog.hasAttribute("data-explicit-close")) {
          closeDialog(dialog);
        }
      });
      dialog.addEventListener("cancel", (event) => {
        if (dialog.hasAttribute("data-explicit-close")) event.preventDefault();
      });
    });

    const passwordButton = document.getElementById("passwordButton");
    const passwordForm = document.getElementById("passwordForm");
    passwordButton?.addEventListener("click", () => openDialog("passwordDialog"));
    passwordForm?.addEventListener("submit", async (event) => {
      event.preventDefault();
      const submit = passwordForm.querySelector('[type="submit"]');
      submit.disabled = true;
      try {
        const form = new FormData(passwordForm);
        await api("/account/password", {
          method: "POST",
          body: {
            current_password: form.get("current_password"),
            new_password: form.get("new_password"),
          },
        });
        passwordForm.reset();
        closeDialog("passwordDialog");
        toast("密码已更新", "success");
      } catch (error) {
        toast(error.message, "error");
      } finally {
        submit.disabled = false;
      }
    });
  });
})();
