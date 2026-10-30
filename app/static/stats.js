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
