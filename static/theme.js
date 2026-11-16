try {
  const preference = localStorage.getItem("shelfshuttle-theme");
  if (preference === "light" || preference === "dark") {
    document.documentElement.dataset.theme = preference;
  }
} catch {
  // The system preference is used when local storage is blocked.
}
