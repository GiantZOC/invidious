// Feedback buttons, filter auto-submit, and the feed-menu restore control.
// Loaded as /x/static/app.js under CSP script-src 'self': no inline handlers.
(function () {
  "use strict";

  function note(element, text, failed) {
    if (!element) return;
    element.textContent = text;
    element.classList.toggle("sidecar-error", Boolean(failed));
  }

  async function postJSON(url, body) {
    const response = await fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json", "Accept": "application/json" },
      body: JSON.stringify(body)
    });
    let data = {};
    try { data = await response.json(); } catch (err) { /* empty body */ }
    return { ok: response.ok && data.ok !== false, data: data };
  }

  document.addEventListener("click", async function (event) {
    const button = event.target.closest(".sidecar-action");
    if (!button) return;
    event.preventDefault();

    const payload = {
      kind: button.dataset.action,
      target_type: button.dataset.targetType,
      target_id: button.dataset.targetId
    };
    button.disabled = true;

    let result;
    try {
      result = await postJSON("/x/api/feedback", payload);
    } catch (err) {
      note(button.parentElement, "Request failed.", true);
      button.disabled = false;
      return;
    }

    if (!result.ok) {
      note(button.parentElement, (result.data && result.data.note) || "Rejected.", true);
      button.disabled = false;
      return;
    }

    const card = button.closest("[data-card]");
    if (!card) return;

    if (payload.kind === "watched") {
      card.querySelectorAll('[data-action="watched"]').forEach(function (el) {
        el.classList.add("sidecar-done");
      });
      return;
    }
    // dismiss / block / dislike / hide: the next render drops this card.
    card.classList.add("sidecar-removed");
  });

  document.addEventListener("change", function (event) {
    const field = event.target.closest("select.auto-submit");
    if (!field || !field.form) return;
    field.form.submit();
  });

  const restore = document.getElementById("restore-menu");
  if (restore) {
    restore.addEventListener("click", async function () {
      const target = document.getElementById("restore-menu-note");
      note(target, "Posting…", false);
      restore.disabled = true;
      let result;
      try {
        result = await postJSON("/x/settings/restore-menu", {});
      } catch (err) {
        note(target, "Request failed.", true);
        restore.disabled = false;
        return;
      }
      note(target, (result.data && result.data.note) || "", !result.ok);
      restore.disabled = false;
    });
  }
})();
