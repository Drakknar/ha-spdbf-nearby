// spdbf Public Transport: loads the frontend core and the cards, and keeps
// retrying until they load. The integration registers only this file with
// add_extra_js_url.
//
// Home Assistant imports extra modules once, when the page opens, and never
// again. If that fetch fails (the companion app often opens before the
// network or VPN is up), the cards would show "Custom element doesn't exist"
// until the app reloads. Retried here instead; Home Assistant rebuilds those
// cards by itself once their element is defined.
(() => {
  if (window.spdbfLoader) return;
  window.spdbfLoader = true;

  const FILES = ["dbf-core.js", "spdbf-stop-search-card.js", "spdbf-connection-card.js"];
  const RETRY_MS = [2000, 5000, 15000, 30000, 60000];
  const here = new URL(import.meta.url);
  const version = here.searchParams.get("v") ?? "";

  // A retry gets its own URL, since the browser may keep a failed module
  // import for the page's lifetime. The files guard against loading twice.
  const load = (file, attempt = 0) => {
    const url = new URL(file, here);
    url.searchParams.set("v", version);
    if (attempt) url.searchParams.set("retry", String(attempt));
    return import(url.href).catch((err) => {
      console.warn(`spdbf Public Transport: couldn't load ${file}, retrying`, err);
      return new Promise((resolve) => {
        const again = () => {
          clearTimeout(timer);
          window.removeEventListener("online", again);
          document.removeEventListener("visibilitychange", onVisible);
          resolve(load(file, attempt + 1));
        };
        const onVisible = () => {
          if (document.visibilityState === "visible") again();
        };
        const timer = setTimeout(again, RETRY_MS[Math.min(attempt, RETRY_MS.length - 1)]);
        window.addEventListener("online", again);
        document.addEventListener("visibilitychange", onVisible);
      });
    });
  };

  FILES.forEach((file) => load(file));
})();
