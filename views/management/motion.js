/* Presentation-only feedback for the management page. */
const tabs = document.querySelector(".tabs");
const glide = document.querySelector(".tab-glide");
let glideFrame = 0;

function placeGlide() {
  glideFrame = 0;
  if (!tabs || !glide) return;
  const active = tabs.querySelector('button[aria-selected="true"]');
  if (!active) return;
  glide.style.width = `${active.offsetWidth}px`;
  glide.style.transform = `translate3d(${active.offsetLeft}px, 0, 0)`;
  glide.classList.add("is-visible");
  if (!tabs.classList.contains("has-glide")) {
    // Establish the initial position before enabling transitions.
    glide.getBoundingClientRect();
    tabs.classList.add("has-glide");
  }
}

function scheduleGlide() {
  if (glideFrame) return;
  glideFrame = window.requestAnimationFrame(placeGlide);
}

if (tabs && glide) {
  scheduleGlide();
  let previousTab = tabs.querySelector('button[aria-selected="true"]')?.id ?? "";
  window.addEventListener("resize", scheduleGlide, {passive: true});
  new MutationObserver(() => {
    scheduleGlide();
    const active = tabs.querySelector('button[aria-selected="true"]');
    if (!active || active.id === previousTab) return;
    previousTab = active.id;
    window.scrollTo(0, 0);
  }).observe(tabs, {
    subtree: true,
    attributes: true,
    attributeFilter: ["aria-selected"],
  });
  if (document.fonts?.ready) document.fonts.ready.then(scheduleGlide).catch(() => {});
  if (typeof ResizeObserver === "function") new ResizeObserver(scheduleGlide).observe(tabs);
}

/* The visible text remains the source of truth for loading and connection states. */
const loadingText = /^(?:正在|读取中|重连中)/;
const statusText = /(?:正在读取|正在加载|正在重连)/;

function updateTextNode(node) {
  const text = (node.textContent ?? "").trim();
  const loading = text.length > 0 && text.length <= 120 && (loadingText.test(text) || statusText.test(text));
  node.classList.toggle("is-loading", loading);
  if (node.id === "live-status") node.classList.toggle("is-live", text.includes("已连接"));
}

if (typeof MutationObserver === "function") {
  const selectors = [
    ".notice", ".empty", "#live-status", "#status", "#current-rules",
    "#groups-list", "#platforms-list", "#records-match-count",
  ];
  for (const node of document.querySelectorAll(selectors.join(", "))) {
    new MutationObserver(() => updateTextNode(node)).observe(node, {
      childList: true,
      characterData: true,
      subtree: true,
    });
    updateTextNode(node);
  }
}
