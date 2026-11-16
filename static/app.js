const csrf = document.querySelector('meta[name="csrf-token"]').content;
const systemTheme = matchMedia("(prefers-color-scheme: dark)");
const theme = document.querySelector("[data-theme-toggle]");

function currentTheme() {
  return document.documentElement.dataset.theme || (systemTheme.matches ? "dark" : "light");
}

function labelTheme() {
  if (theme) theme.querySelector("[data-theme-label]").textContent = currentTheme() === "dark" ? "Light mode" : "Dark mode";
}

theme?.addEventListener("click", () => {
  const next = currentTheme() === "dark" ? "light" : "dark";
  document.documentElement.dataset.theme = next;
  try { localStorage.setItem("shelfshuttle-theme", next); } catch { /* Session only. */ }
  labelTheme();
});
systemTheme.addEventListener("change", labelTheme);
labelTheme();

const form = document.querySelector("[data-add-form]");
if (form) {
  const controls = form.elements;
  const preview = form.querySelector("[data-preview]");
  const uploadStatus = form.querySelector("#upload-status");
  const suggest = form.querySelector("[data-apply-suggestion]");
  let timer;
  let suggestions;
  let previewRequest;
  let uploadRequest;

  function text(tag, value) {
    const node = document.createElement(tag);
    node.textContent = value;
    return node;
  }

  async function updatePreview() {
    previewRequest?.abort();
    const author = controls.author.value.trim();
    const title = controls.title.value.trim();
    if (!author || !title) {
      preview.replaceChildren(text("p", "Enter an author and title to preview the library folders."));
      return;
    }
    previewRequest = new AbortController();
    const params = new URLSearchParams({ author, title, format: controls.format.value });
    try {
      const response = await fetch("/api/destinations?" + params, { signal: previewRequest.signal });
      if (!response.ok) throw new Error();
      const paths = await response.json();
      const list = document.createElement("dl");
      for (const [target, path] of Object.entries(paths)) {
        list.append(text("dt", target === "audio" ? "Audiobook library" : "Ebook library"));
        const value = document.createElement("dd");
        value.append(text("code", path));
        list.append(value);
      }
      preview.replaceChildren(list);
    } catch (error) {
      if (error.name !== "AbortError") preview.replaceChildren(text("p", "The destination preview is unavailable."));
    }
  }

  function updateFormat() {
    const ebook = controls.format.value !== "audio";
    const audio = controls.format.value !== "ebook";
    form.querySelector("[data-ebook-field]").hidden = !ebook;
    form.querySelector("[data-audio-field]").hidden = !audio;
    controls.epub.disabled = !ebook;
    controls.epub.required = ebook;
    controls.torrent.disabled = !audio;
    controls.torrent.required = audio;
    updatePreview();
  }

  form.addEventListener("change", (event) => {
    if (event.target.name === "format") updateFormat();
  });
  form.addEventListener("input", (event) => {
    if (event.target.name !== "author" && event.target.name !== "title") return;
    clearTimeout(timer);
    timer = setTimeout(updatePreview, 160);
  });
  controls.epub.addEventListener("change", async () => {
    uploadRequest?.abort();
    suggestions = null;
    suggest.hidden = true;
    const file = controls.epub.files[0];
    uploadStatus.hidden = !file;
    if (!file) return;
    uploadStatus.textContent = "Reading EPUB metadata…";
    uploadRequest = new AbortController();
    const body = new FormData();
    body.append("epub", file);
    const selected = uploadRequest;
    try {
      const response = await fetch("/api/epub/inspect", {
        method: "POST", headers: { "X-CSRF-Token": csrf }, body, signal: selected.signal,
      });
      if (response.status === 413) throw new Error("The EPUB exceeds the upload limit.");
      const data = await response.json();
      if (!response.ok) throw new Error(typeof data.detail === "string" ? data.detail : "The EPUB could not be read.");
      if (selected.signal.aborted) return;
      suggestions = data;
      for (const key of ["author", "title"]) {
        if (!controls[key].value.trim() && suggestions[key]) controls[key].value = suggestions[key];
      }
      const hasDetails = Boolean(data.author || data.title);
      suggest.hidden = !hasDetails || ["author", "title"].every((key) => !data[key] || controls[key].value === data[key]);
      uploadStatus.textContent = hasDetails ? "EPUB details found. Confirm the author and title before adding." : "The EPUB has no author or title tags.";
      updatePreview();
    } catch (error) {
      if (error.name !== "AbortError") uploadStatus.textContent = error.message;
    }
  });
  suggest.addEventListener("click", () => {
    for (const key of ["author", "title"]) {
      if (suggestions?.[key]) controls[key].value = suggestions[key];
    }
    suggest.hidden = true;
    updatePreview();
  });
  updateFormat();
}

const dashboard = document.querySelector("[data-dashboard]");
if (dashboard) {
  const refresh = async () => {
    const editing = () => document.hidden || dashboard.contains(document.activeElement) ||
      Boolean(dashboard.querySelector("details[open]"));
    if (editing()) return;
    try {
      const response = await fetch("/dashboard/content" + location.search);
      if (response.ok) {
        const html = await response.text();
        if (!editing()) dashboard.innerHTML = html;
      }
    } catch {
      // Keep the last visible queue when the app is temporarily unreachable.
    }
  };
  setInterval(refresh, 5000);
}

const testQbit = document.querySelector("[data-test-qbit]");
testQbit?.addEventListener("click", async () => {
  const status = document.querySelector("[data-qbit-status]");
  testQbit.disabled = true;
  status.textContent = "Testing connection…";
  try {
    const response = await fetch("/api/settings/test-qbittorrent", {
      method: "POST", headers: { "X-CSRF-Token": csrf },
    });
    const data = await response.json();
    status.textContent = response.ok ? "Connected to Web API " + data.api_version : data.detail;
  } catch {
    status.textContent = "Connection test unavailable.";
  } finally {
    testQbit.disabled = false;
  }
});
