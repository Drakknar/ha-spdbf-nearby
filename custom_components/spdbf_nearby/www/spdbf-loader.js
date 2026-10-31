// spdbf Public Transport: loads the frontend core and the cards, and keeps
// retrying until they load. The integration registers only this file with
// add_extra_js_url.
//
// Home Assistant imports extra modules once, when the page opens, and never
// again. If that fetch fails (the companion app often opens before the
// network or VPN is up), the cards would show "Custom element doesn't exist"
// until the app reloads. Retried here instead; Home Assistant rebuilds those
// cards by itself once their element is defined. An import can also hang
// instead of failing, which reports nothing, so each one gets a time limit.
//
// The cards must not load before Home Assistant's app does. Its app.js replaces
// window.customElements with a new registry, and an element defined earlier
// stays in the old one, where the frontend never finds it. From the browser
// cache this module easily runs first: "Configuration error" on every reload,
// fine again after clearing the cache. home-assistant is defined after that
// swap, so the cards wait for it.
(() => {
  if (window.spdbfLoader) return;
  window.spdbfLoader = true;

  const FILES = ["dbf-core.js", "spdbf-stop-search-card.js", "spdbf-connection-card.js"];
  const RETRY_MS = [2000, 5000, 15000, 30000, 60000];
  const IMPORT_LIMIT_MS = 10000;
  const APP_LIMIT_MS = 30000; // load anyway, should the element ever be renamed
  const here = new URL(import.meta.url);
  const version = here.searchParams.get("v") ?? "";

  const importWithLimit = (href) => {
    let timer;
    const limit = new Promise((_, reject) => {
      timer = setTimeout(() => reject(new Error("timed out")), IMPORT_LIMIT_MS);
    });
    return Promise.race([import(href), limit]).finally(() => clearTimeout(timer));
  };

  // A retry gets its own URL, since the browser may keep a failed module
  // import for the page's lifetime. The files guard against loading twice,
  // so a timed out import that still finishes later does no harm.
  const load = (file, attempt = 0) => {
    const url = new URL(file, here);
    url.searchParams.set("v", version);
    if (attempt) url.searchParams.set("retry", String(attempt));
    return importWithLimit(url.href).catch((err) => {
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

  const appLoaded = Promise.race([
    customElements.whenDefined("home-assistant"),
    new Promise((resolve) => setTimeout(resolve, APP_LIMIT_MS)),
  ]);
  appLoaded.then(() => FILES.forEach((file) => load(file)));
})();
