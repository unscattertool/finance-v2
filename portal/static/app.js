// Small behaviours that inline handlers can't do under the site's Content-Security-Policy.
document.addEventListener("change", function (ev) {
  var el = ev.target;
  if (el.matches && el.matches("select[data-autosubmit], input[data-autosubmit]")) { el.form.submit(); return; }
  if (el.matches && el.matches("select[data-import-type]")) {
    var opt = el.options[el.selectedIndex], form = el.form;
    var file = form.querySelector("input[type=file]");
    if (file) { file.accept = opt.getAttribute("data-accept") || ""; file.value = ""; }
    var hint = (form.closest("details") || form).querySelector("[data-import-hint]");
    if (hint) hint.textContent = opt.getAttribute("data-hint") || "";
  }
});

// confirm before submitting forms marked data-confirm
document.addEventListener("submit", function (ev) {
  var f = ev.target;
  if (f.matches && f.matches("form[data-confirm]") && !window.confirm(f.getAttribute("data-confirm"))) ev.preventDefault();
});
// open the collapsed section a link points at (#bills, #income …), and close dropdown menus on an outside click
(function () {
  function openHash() {
    var el = location.hash && document.getElementById(location.hash.slice(1));
    if (el && el.tagName === "DETAILS") { el.open = true; el.scrollIntoView(); }
  }
  window.addEventListener("hashchange", openHash);
  document.addEventListener("DOMContentLoaded", openHash);
  document.addEventListener("click", function (e) {
    document.querySelectorAll("details.menu[open]").forEach(function (m) { if (!m.contains(e.target)) m.open = false; });
  });
})();

// drag & drop files onto an upload box (<input type=file data-drop> inside a label.dropzone). On a page with one such
// box, a file dropped anywhere on the page goes to it too.
(function () {
  function okType(f, accept) {
    if (!accept) return true;
    return accept.split(",").some(function (a) {
      a = a.trim().toLowerCase();
      if (a.endsWith("/*")) return (f.type || "").toLowerCase().indexOf(a.slice(0, -1)) === 0;
      if (a.charAt(0) === ".") return f.name.toLowerCase().endsWith(a);
      return (f.type || "").toLowerCase() === a;
    }) || /\.(pdf|jpe?g|png|heic|heif|webp|gif)$/i.test(f.name);
  }
  function show(input) {
    var zone = input.closest(".dropzone");
    var list = zone && zone.querySelector("[data-drop-list]");
    if (!list) return;
    var names = Array.prototype.map.call(input.files || [], function (f) { return f.name; });
    list.textContent = names.length ? names.length + " file" + (names.length === 1 ? "" : "s") + ": " + names.join(", ") : "";
  }
  function put(input, files) {
    var dt = new DataTransfer(), skipped = 0;
    Array.prototype.forEach.call(files, function (f) { if (okType(f, input.accept)) dt.items.add(f); else skipped++; });
    if (!input.multiple && dt.files.length > 1) { var one = new DataTransfer(); one.items.add(dt.files[0]); dt = one; }
    input.files = dt.files;
    show(input);
    if (window.receiptNamePreview && input.form) window.receiptNamePreview(input.form);
    if (skipped) {
      var zone = input.closest(".dropzone"), list = zone && zone.querySelector("[data-drop-list]");
      if (list) list.textContent += (list.textContent ? " · " : "") + skipped + " skipped (only PDFs and photos)";
    }
  }
  // a row upload (form[data-upload-on-pick]) sends as soon as a file is picked or dropped, named the way the main
  // upload form is set (Rename / Keep)
  function sendRow(input) {
    var f = input.form;
    if (!f || !f.hasAttribute("data-upload-on-pick") || !input.files || !input.files.length) return;
    var pick = document.querySelector("input[name=naming]:checked"), n = f.querySelector("input[name=naming]");
    if (pick && n) n.value = pick.value;
    var row = input.closest(".droprow");
    if (row) row.classList.add("over");
    f.submit();
  }
  document.addEventListener("change", function (ev) {
    if (ev.target.matches && ev.target.matches("input[data-drop]")) { show(ev.target); sendRow(ev.target); }
  });
  function zoneFor(ev) {
    var z = ev.target.closest && ev.target.closest(".dropzone, .droprow");
    if (z) return z;
    var all = document.querySelectorAll(".dropzone");
    return document.querySelector(".dropzone[data-drop-default]") || (all.length === 1 ? all[0] : null);
  }
  ["dragenter", "dragover"].forEach(function (t) {
    document.addEventListener(t, function (ev) {
      if (!ev.dataTransfer || Array.prototype.indexOf.call(ev.dataTransfer.types || [], "Files") < 0) return;
      ev.preventDefault();
      var z = zoneFor(ev);
      document.querySelectorAll(".dropzone.over").forEach(function (x) { if (x !== z) x.classList.remove("over"); });
      if (z) { z.classList.add("over"); ev.dataTransfer.dropEffect = "copy"; } else ev.dataTransfer.dropEffect = "none";
    });
  });
  document.addEventListener("dragleave", function (ev) {
    if (!ev.relatedTarget) document.querySelectorAll(".dropzone.over").forEach(function (x) { x.classList.remove("over"); });
  });
  document.addEventListener("drop", function (ev) {
    if (!ev.dataTransfer || !ev.dataTransfer.files || !ev.dataTransfer.files.length) return;
    ev.preventDefault();  // never let the browser open the file instead
    var z = zoneFor(ev);
    document.querySelectorAll(".dropzone.over").forEach(function (x) { x.classList.remove("over"); });
    if (!z) return;
    var input = z.querySelector("input[data-drop]");
    if (input) { put(input, ev.dataTransfer.files); if (input.form && input.form.hasAttribute("data-upload-on-pick")) sendRow(input); else z.scrollIntoView({ block: "nearest" }); }
  });
})();

// live preview of the receipt's file name (VendorName_YYYY-MM-DD_Reference#) as the form is filled in
(function () {
  function vendorPart(v) {
    return ((v || "").replace(/'/g, "").match(/[A-Za-z0-9]+/g) || []).map(function (w) { return w.charAt(0).toUpperCase() + w.slice(1); }).join("").slice(0, 40);
  }
  function refPart(r) { return (r || "").replace(/[^A-Za-z0-9-]+/g, "-").replace(/^-+|-+$/g, "").slice(0, 40); }
  function today() { var d = new Date(); return d.getFullYear() + "-" + ("0" + (d.getMonth() + 1)).slice(-2) + "-" + ("0" + d.getDate()).slice(-2); }
  function val(f, n) { var el = f.querySelector("[name=" + n + "]"); return el ? el.value.trim() : ""; }
  function update(f) {
    var box = f.querySelector("[data-name-preview]");
    if (!box) return;
    var out = box.querySelector("[data-name-out]"), note = box.querySelector("[data-name-note]");
    var file = f.querySelector("input[type=file]"), files = file && file.files ? file.files : [];
    var keep = f.querySelector("input[name=naming][value=keep]");
    var ext = ".pdf";
    if (files.length) { var m = /\.[A-Za-z0-9]+$/.exec(files[0].name); if (m) ext = m[0].toLowerCase().replace(".jpeg", ".jpg"); }
    var ref = refPart(val(f, "ref"));
    if (keep && keep.checked) {
      out.textContent = files.length ? files[0].name : "the file's own name";
      var b2 = box.getAttribute("data-base");
      if (b2) {
        var v2 = (val(f, "vendor") || "").replace(/[\\/:*?"<>|#%]+/g, " ").replace(/\s+/g, " ").replace(/^[\s.]+|[\s.]+$/g, "").slice(0, 60) || "Other";
        out.textContent = b2.split("/").join(" › ") + " › " + v2 + " › " + (val(f, "date") || today()).slice(0, 4) + " › " + out.textContent;
      }
      note.textContent = files.length > 1 ? " (and the other files' own names)" : "";
      return;
    }
    var name = [vendorPart(val(f, "vendor")) || "Receipt", val(f, "date") || today()];
    if (ref) name.push(ref);
    out.textContent = name.join("_") + (files.length ? ext : ".pdf");
    var base = box.getAttribute("data-base");
    if (base) {
      var v = (val(f, "vendor") || "").replace(/[\\/:*?"<>|#%]+/g, " ").replace(/\s+/g, " ").replace(/^[\s.]+|[\s.]+$/g, "").slice(0, 60) || "Other";
      out.textContent = base.split("/").join(" › ") + " › " + v + " › " + (val(f, "date") || today()).slice(0, 4) + " › " + out.textContent;
    }
    note.textContent = (ref ? "" : " · no Reference # entered — Finance looks for one in the PDF and the file's name") +
                       (files.length > 1 ? " · same name base for all " + files.length + " files" : "");
  }
  function onEvent(ev) { var f = ev.target && ev.target.form; if (f) update(f); }
  document.addEventListener("input", onEvent);
  document.addEventListener("change", onEvent);
  document.addEventListener("DOMContentLoaded", function () {
    document.querySelectorAll("[data-name-preview]").forEach(function (b) { var f = b.closest("form"); if (f) update(f); });
  });
  window.receiptNamePreview = update;  // the drop handler calls this after filling the file box
})();
