/* Statistics page: one shared tooltip for every chart mark that carries a
   data-tip. Tooltips only repeat what the page already says in text or in
   a "Show the numbers" table, so nothing depends on hovering. Delegated
   from document, so marks swapped in by htmx need no wiring. */
(() => {
  const tip = document.querySelector(".chart-tip");
  if (!tip) return;
  let current = null;

  const place = (x, y) => {
    const pad = 12;
    const { width, height } = tip.getBoundingClientRect();
    let left = x - width / 2;
    left = Math.max(pad, Math.min(left, window.innerWidth - width - pad));
    let top = y - height - 14;
    if (top < pad) top = y + 18;
    tip.style.transform = `translate(${Math.round(left)}px, ${Math.round(top)}px)`;
  };

  const show = (el, x, y) => {
    if (current && current !== el) current.classList.remove("is-hot");
    current = el;
    el.classList.add("is-hot");
    tip.textContent = el.dataset.tip; // labels are data: never innerHTML
    tip.hidden = false;
    place(x, y);
  };

  const hide = () => {
    if (current) current.classList.remove("is-hot");
    current = null;
    tip.hidden = true;
  };

  document.addEventListener("pointermove", (e) => {
    const el = e.target.closest?.("[data-tip]");
    if (el) show(el, e.clientX, e.clientY);
    else if (current) hide();
  });
  document.addEventListener("pointerleave", hide);
  window.addEventListener("scroll", hide, { passive: true });
  document.addEventListener("htmx:beforeSwap", hide);
})();

/* Contents bar: mark the section in view, and keep its chip scrolled into
   sight in the bar. Re-armed after htmx swaps the body. */
(() => {
  let observer = null;
  const arm = () => {
    observer?.disconnect();
    const links = new Map(
      [...document.querySelectorAll(".stats-nav a")]
        .filter((a) => document.getElementById(a.hash.slice(1)))
        .map((a) => [a.hash.slice(1), a])
    );
    if (!links.size) return;
    const visible = new Set();
    observer = new IntersectionObserver(
      (entries) => {
        for (const e of entries) e.isIntersecting ? visible.add(e.target.id) : visible.delete(e.target.id);
        const first = [...links.keys()].find((id) => visible.has(id));
        for (const [id, a] of links) {
          const on = id === first;
          a.classList.toggle("is-current", on);
          if (on) a.setAttribute("aria-current", "true");
          else a.removeAttribute("aria-current");
          if (on) {
            // Scroll the bar sideways only, never the page, keeping the chip
            // clear of the part's label pinned at the left. The first chip
            // takes the bar right back to its start.
            const bar = a.parentElement;
            const label = bar.querySelector(".nav-part")?.offsetWidth ?? 0;
            const first = a === bar.querySelector(".nav-chip");
            const left = a.offsetLeft - bar.offsetLeft;
            const target = first ? 0 : left - label - 12;
            if (first ? bar.scrollLeft > 0 : left < bar.scrollLeft + label || left + a.offsetWidth > bar.scrollLeft + bar.clientWidth) {
              bar.scrollTo({ left: target, behavior: "smooth" });
            }
          }
        }
      },
      { rootMargin: "-30% 0px -60% 0px" }
    );
    for (const id of links.keys()) {
      const el = document.getElementById(id);
      if (el) observer.observe(el);
    }
  };
  arm();
  document.addEventListener("htmx:afterSettle", arm);
})();

/* Sections play their entrance once, the first time they come into view,
   and headline numbers count up as they do. Re-armed after htmx swaps. */
(() => {
  const still = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  const countUp = (el) => {
    const text = el.textContent.trim();
    const m = text.match(/^(\d+(?:\.\d+)?)(.*)$/);
    if (still || !m) return;
    const target = parseFloat(m[1]);
    const places = (m[1].split(".")[1] || "").length;
    const start = performance.now();
    const step = (now) => {
      const t = Math.min(1, (now - start) / 900);
      const eased = 1 - Math.pow(1 - t, 3);
      el.textContent = (target * eased).toFixed(places) + m[2];
      if (t < 1) requestAnimationFrame(step);
    };
    requestAnimationFrame(step);
  };
  let observer = null;
  const arm = () => {
    observer?.disconnect();
    observer = new IntersectionObserver(
      (entries) => {
        for (const e of entries) {
          if (!e.isIntersecting) continue;
          e.target.classList.add("in-view");
          e.target.querySelectorAll(".headline .num").forEach(countUp);
          observer.unobserve(e.target);
        }
      },
      { threshold: 0.12 }
    );
    document.querySelectorAll(".story:not(.in-view)").forEach((s) => observer.observe(s));
  };
  arm();
  document.addEventListener("htmx:afterSettle", (e) => {
    // A swapped-in fragment inside a section that has already played shows at once.
    e.target.closest?.(".story")?.classList.add("in-view");
    arm();
  });
})();
