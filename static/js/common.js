(() => {
  "use strict";

  const csrfToken = document.querySelector('meta[name="csrf-token"]')?.content || "";
  let dateTimeFormatter = null;
  let timeFormatter = null;

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
      const errorId = payload?.error_id || "";
      const message = payload?.error || `请求失败（HTTP ${response.status}）`;
      const error = new Error(errorId ? `${message}（错误 ID：${errorId}）` : message);
      error.code = payload?.code || "request_failed";
      error.status = response.status;
      error.errorId = errorId;
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
    const lucide = window.lucide;
    if (!lucide) return;
    const placeholders = [
      ...(root.matches?.("[data-lucide]:not(svg)") ? [root] : []),
      ...root.querySelectorAll("[data-lucide]:not(svg)"),
    ];
    placeholders.forEach((placeholder) => {
      const name = placeholder.dataset.lucide;
      const key = name?.replace(
        /(\w)(\w*)(_|-|\s*)/g,
        (_match, first, rest) => first.toUpperCase() + rest.toLowerCase(),
      );
      const icon = lucide.icons[key];
      if (!icon) return;
      const svg = lucide.createElement(icon);
      [...placeholder.attributes].forEach((attribute) => {
        if (!["class", "data-lucide"].includes(attribute.name)) {
          svg.setAttribute(attribute.name, attribute.value);
        }
      });
      svg.setAttribute("data-lucide", name);
      svg.setAttribute(
        "class",
        [...new Set(["lucide", `lucide-${name}`, ...placeholder.classList])].join(" "),
      );
      placeholder.replaceWith(svg);
    });
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
        const nextValue = money(value);
        if (node.textContent === nextValue) return;
        node.textContent = nextValue;
        if (!window.matchMedia("(prefers-reduced-motion: reduce)").matches && typeof node.animate === "function") {
          const styles = window.getComputedStyle(node);
          const accent = window.getComputedStyle(document.documentElement).getPropertyValue("--accent-strong").trim();
          node.animate(
            [
              { color: accent, transform: "translateY(-2px)" },
              { color: styles.color, transform: "translateY(0)" },
            ],
            { duration: 320, easing: "cubic-bezier(.22, 1, .36, 1)" },
          );
        }
      });
    });
  }

  function dateTime(value) {
    if (!value) return "--";
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return "--";
    dateTimeFormatter ||= new Intl.DateTimeFormat("zh-CN", {
      month: "2-digit",
      day: "2-digit",
      hour: "2-digit",
      minute: "2-digit",
      second: "2-digit",
      hour12: false,
    });
    return dateTimeFormatter.format(date);
  }

  function timeOnly(value) {
    if (!value) return "--:--";
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return "--:--";
    timeFormatter ||= new Intl.DateTimeFormat("zh-CN", {
      hour: "2-digit",
      minute: "2-digit",
      hour12: false,
    });
    return timeFormatter.format(date);
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
