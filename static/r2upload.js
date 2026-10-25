/* HERE — direct-to-R2 uploads for BIG video (Build U).
 *
 * Progressive enhancement, inert unless the server offers it:
 * a file input tagged data-r2up="post" or data-r2up="story" whose
 * chosen file is a VIDEO gets, on form submit:
 *   1. POST /api/uploads/presign  -> {direct:true, url, key} or
 *      {direct:false} when direct upload isn't available (disk mode,
 *      image file, unsupported type) — then the form submits exactly
 *      as it always has.
 *   2. XHR PUT of the file straight to R2 with a % progress readout.
 *   3. The returned key goes into a hidden media_key field, the file
 *      input is cleared (so the big file does NOT also ride through
 *      the app server), and the form submits normally.
 * Any failure along the way falls back to a plain form submit.
 */
(function () {
  "use strict";

  function statusEl(input) {
    var el = input.parentNode.querySelector(".r2up-status");
    if (!el) {
      el = document.createElement("div");
      el.className = "r2up-status";
      el.style.cssText = "font-size:.85em;opacity:.85;margin:.35em 0;";
      input.parentNode.insertBefore(el, input.nextSibling);
    }
    return el;
  }

  function presign(surface, file) {
    return fetch("/api/uploads/presign", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      credentials: "same-origin",
      body: JSON.stringify({
        surface: surface,
        filename: file.name || "",
        content_type: file.type || ""
      })
    }).then(function (r) { return r.json(); })
      .catch(function () { return null; });
  }

  function putWithProgress(url, file, onPct) {
    return new Promise(function (resolve, reject) {
      var xhr = new XMLHttpRequest();
      xhr.open("PUT", url, true);
      xhr.upload.onprogress = function (ev) {
        if (ev.lengthComputable && onPct) {
          onPct(Math.round((ev.loaded / ev.total) * 100));
        }
      };
      xhr.onload = function () {
        if (xhr.status >= 200 && xhr.status < 300) resolve();
        else reject(new Error("PUT " + xhr.status));
      };
      xhr.onerror = function () { reject(new Error("network")); };
      xhr.send(file);
    });
  }

  function wire(input) {
    var surface = input.getAttribute("data-r2up");
    var form = input.form;
    if (!surface || !form || form.__r2upWired) return;
    form.__r2upWired = true;
    var busy = false;
    form.addEventListener("submit", function (ev) {
      if (busy) return;                       // our own resubmit
      var file = input.files && input.files[0];
      if (!file || !/^video\//.test(file.type || "")) return;  // normal path
      ev.preventDefault();
      busy = true;
      var status = statusEl(input);
      var btns = form.querySelectorAll("button[type=submit],input[type=submit]");
      btns.forEach(function (b) { b.disabled = true; });
      function fallback(msg) {
        status.textContent = msg || "";
        busy = false;
        btns.forEach(function (b) { b.disabled = false; });
        form.submit();
      }
      status.textContent = "Preparing upload…";
      presign(surface, file).then(function (info) {
        if (!info || !info.direct || !info.url || !info.key) {
          fallback("");                       // server says: normal path
          return;
        }
        putWithProgress(info.url, file, function (pct) {
          status.textContent = "Uploading video… " + pct + "%";
        }).then(function () {
          var hidden = form.querySelector("input[name='media_key']");
          if (!hidden) {
            hidden = document.createElement("input");
            hidden.type = "hidden";
            hidden.name = "media_key";
            form.appendChild(hidden);
          }
          hidden.value = info.key;
          try { input.value = ""; } catch (e) { /* file already sent */ }
          status.textContent = "Uploaded — posting…";
          busy = false;
          form.submit();
        }).catch(function () {
          fallback("Upload hiccup — sending the regular way…");
        });
      });
    });
  }

  document.addEventListener("DOMContentLoaded", function () {
    document.querySelectorAll("input[type=file][data-r2up]")
      .forEach(wire);
  });
})();
