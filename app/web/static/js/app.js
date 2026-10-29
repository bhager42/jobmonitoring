// Progressive enhancements. Every page works without this file.
"use strict";

if ("serviceWorker" in navigator) {
  navigator.serviceWorker.register("/sw.js").catch(() => {});
}

// Links with data-dialog-open="<id>" open that <dialog> instead of navigating to the
// no-JavaScript confirmation page they point at.
document.addEventListener("click", (event) => {
  const opener = event.target.closest("[data-dialog-open]");
  if (opener) {
    const dialog = document.getElementById(opener.dataset.dialogOpen);
    if (dialog && typeof dialog.showModal === "function") {
      event.preventDefault();
      dialog.showModal();
    }
    return;
  }
  const closer = event.target.closest("[data-dialog-close]");
  if (closer) {
    closer.closest("dialog")?.close();
  }
});

// After htmx swaps in content that asks for focus (e.g. a new error summary), move focus there
// so keyboard and screen reader users notice the change.
document.addEventListener("htmx:afterSettle", (event) => {
  const target = event.detail.target?.querySelector?.("[data-focus-on-load]");
  if (target) {
    target.focus();
  }
});
