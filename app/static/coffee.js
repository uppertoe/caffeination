/* coffee-rch — small Alpine additions. Loaded (deferred) before Alpine so the
   alpine:init registrations below run before Alpine walks the DOM. */
document.addEventListener("alpine:init", () => {
  /* x-keyboard-fit — keep a filterable list visible above the on-screen
     keyboard.

     Put it on the <ul> that a type="search" input in the same <article>
     filters. On touch devices, while that input has focus:

       - the page scrolls so the input sits at the top of the visible area
         (only when the focus came from a tap — a focus restored by htmx
         after a swap must not yank the page around);
       - the list's max-height shrinks to whatever is left between the
         input and the top of the keyboard, so the filtered names and the
         "scroll for more" pill sit above it rather than under it;
       - the page gains bottom padding equal to the keyboard's height, so
         a list near the foot of the page can still scroll up clear of it.

     The visual viewport is the only reliable signal: iOS keeps the layout
     viewport the same height and shrinks the visual one, Android (with the
     interactive-widget=resizes-content viewport hint) shrinks both. Reading
     visualViewport on each of its resize events covers both. Everything is
     undone on blur, and on desktop (fine pointer) the directive is inert. */
  Alpine.directive("keyboard-fit", (list, _binding, { cleanup }) => {
    const vv = window.visualViewport;
    const article = list.closest("article");
    const input = article && article.querySelector('input[type="search"]');
    if (!vv || !input || !window.matchMedia("(pointer: coarse)").matches) return;

    const MARGIN = 8;
    const MIN_HEIGHT = 128;
    const cssMax = parseFloat(getComputedStyle(list).maxHeight) || Infinity;
    let tapped = false;
    let timers = [];

    const focused = () => document.activeElement === input;
    const notifyScroll = () => list.dispatchEvent(new Event("scroll"));

    const reset = () => {
      list.style.maxHeight = "";
      document.body.style.paddingBottom = "";
      notifyScroll();
    };

    const fit = () => {
      if (!focused()) { reset(); return; }
      // Height the keyboard is covering (0 where the layout viewport shrank).
      const covered = Math.max(0, window.innerHeight - vv.height - vv.offsetTop);
      document.body.style.paddingBottom = covered ? covered + "px" : "";
      if (tapped) {
        const delta = input.getBoundingClientRect().top - vv.offsetTop - MARGIN;
        if (Math.abs(delta) > 2) window.scrollBy(0, delta);
      }
      const visibleBottom = vv.offsetTop + vv.height;
      const room = visibleBottom - list.getBoundingClientRect().top - MARGIN;
      list.style.maxHeight = Math.min(cssMax, Math.max(MIN_HEIGHT, room)) + "px";
      notifyScroll();
    };

    const schedule = () => {
      timers.forEach(clearTimeout);
      // Keyboards animate in; measure once soon and again when it has settled.
      timers = [setTimeout(fit, 60), setTimeout(fit, 400)];
    };

    const onPointerDown = () => { tapped = true; };
    const onFocus = () => schedule();
    const onBlur = () => { tapped = false; timers.forEach(clearTimeout); setTimeout(fit, 0); };
    const onViewport = () => { if (focused()) fit(); };

    input.addEventListener("pointerdown", onPointerDown);
    input.addEventListener("focus", onFocus);
    input.addEventListener("blur", onBlur);
    vv.addEventListener("resize", onViewport);
    vv.addEventListener("scroll", onViewport);

    cleanup(() => {
      timers.forEach(clearTimeout);
      input.removeEventListener("pointerdown", onPointerDown);
      input.removeEventListener("focus", onFocus);
      input.removeEventListener("blur", onBlur);
      vv.removeEventListener("resize", onViewport);
      vv.removeEventListener("scroll", onViewport);
      document.body.style.paddingBottom = "";
    });
  });
});

/* Optimistic order changes: a roster row turns selected (or back) and a row
   in "Your order" fades out the moment its button is tapped, rather than
   after the round trip, which is noticeable on a slow network. The server's
   response then confirms the state as before.

   If the server can't be reached (no response, or a 5xx while it restarts),
   the change is kept rather than undone: the row is marked unsaved and the
   change is retried until the server answers again. While anything is
   unsaved, every further tap is queued the same way, so tapping a row twice
   simply cancels its change. A person's saved state is which URL their
   roster button posts to, since that is what the server last rendered; the
   wanted state is kept here and put back on the rows after any re-render.
   After about a minute without a response the rows are put back and the
   till summary says so. A refusal from the server (4xx) is undone straight
   away, as before. */
(() => {
  const GIVE_UP_MS = 60000;
  const rosterButton = (el) => el.closest?.(".roster-list li button[hx-post]");
  const orderButton = (el) => el.closest?.('#order-list li button[hx-post^="/order/remove/"]');
  const rosterRow = (id) => document.getElementById(`roster-${id}`);
  const listRow = (id) =>
    document.querySelector(`#order-list li button[hx-post^="/order/remove/${id}?"]`)?.closest("li");
  const idOf = (button) => button.getAttribute("hx-post").match(/\/order\/(?:add|remove)\/([^/?]+)/)[1];

  const unsaved = new Map(); // person id -> wanted in the order
  let since = 0;
  let timer = null;
  let delay = 0;
  let flushing = false;
  let sending = null; // the person whose change is on its way to the server

  const savedIn = (li) => !!li.querySelector('button[hx-post^="/order/remove/"]');
  const wantedIn = (li) => li.classList.contains("is-selected");

  // Show a roster row as selected or not, without touching what it posts.
  const show = (li, selected) => {
    li.classList.toggle("is-selected", selected);
    const button = li.querySelector("button[hx-post]");
    if (button) button.textContent = selected ? "Remove" : "Add";
  };

  const note = () => document.documentElement.classList.toggle("has-unsaved", unsaved.size > 0);

  // Put the unsaved changes back on the rows, after anything re-rendered them.
  const mark = () => {
    for (const [id, wanted] of unsaved) {
      const li = rosterRow(id);
      if (li) { show(li, wanted); li.classList.add("is-unsaved"); }
      listRow(id)?.classList.toggle("is-leaving", !wanted);
    }
    note();
  };

  // A row now shows the state wanted for it; remember it if the server
  // hasn't got that state yet, or forget it if the server already has.
  const track = (id) => {
    const li = rosterRow(id);
    if (!li) return;
    li.classList.remove("is-pending");
    const wanted = wantedIn(li);
    // While their change is out, the saved state is about to flip, so a
    // second tap must be kept even though it matches the row as rendered.
    if (wanted !== savedIn(li) || id === sending) {
      if (!unsaved.size) { since = Date.now(); delay = 0; }
      unsaved.set(id, wanted);
    } else {
      unsaved.delete(id);
      li.classList.remove("is-unsaved");
      listRow(id)?.classList.remove("is-leaving");
    }
    mark();
    schedule();
  };

  // Retry after a second, backing off to eight while the server stays away.
  const schedule = (failed = false) => {
    clearTimeout(timer);
    if (!unsaved.size || flushing) return;
    if (failed || !delay) delay = Math.min(delay ? delay * 2 : 1000, 8000);
    timer = setTimeout(flush, delay);
  };

  const giveUp = () => {
    for (const id of unsaved.keys()) {
      const li = rosterRow(id);
      if (li) { show(li, savedIn(li)); li.classList.remove("is-unsaved"); }
      listRow(id)?.classList.remove("is-leaving");
    }
    unsaved.clear();
    note();
    document.documentElement.classList.add("save-failed");
  };

  // Send each unsaved change, one at a time, in the shape a roster tap sends.
  const flush = async () => {
    flushing = true;
    let failed = false;
    try {
      for (const [id, wanted] of [...unsaved]) {
        const li = rosterRow(id);
        if (!li || wanted === savedIn(li)) { li?.classList.remove("is-unsaved"); unsaved.delete(id); continue; }
        let response;
        sending = id;
        try {
          response = await fetch(`/order/${wanted ? "add" : "remove"}/${id}`, {
            method: "POST",
            headers: { "HX-Request": "true", "HX-Target": `roster-${id}` },
          });
        } catch { response = null; }
        sending = null;
        if (!response || response.status >= 500) { failed = true; break; }
        delay = 0;
        const html = await response.text();
        // Tapped again while the request was out: keep that newer change.
        if (unsaved.get(id) === wanted) unsaved.delete(id);
        if (response.ok) htmx.swap(`#roster-${id}`, html, { swapStyle: "outerHTML" });
        mark();
      }
    } finally {
      flushing = false;
    }
    if (unsaved.size && Date.now() - since > GIVE_UP_MS) giveUp();
    note();
    schedule(failed);
  };

  document.addEventListener("htmx:afterSettle", () => { if (unsaved.size) mark(); });
  window.addEventListener("online", () => { if (unsaved.size) { delay = 0; clearTimeout(timer); flush(); } });

  document.addEventListener("htmx:beforeRequest", (e) => {
    const roster = rosterButton(e.target);
    const order = orderButton(e.target);
    if (!roster && !order) return;
    document.documentElement.classList.remove("save-failed");
    if (roster) {
      const li = roster.closest("li");
      show(li, !wantedIn(li));
      if (unsaved.size) { e.preventDefault(); track(idOf(roster)); return; }
      li.classList.add("is-pending");
      return;
    }
    const id = idOf(order);
    if (unsaved.size && rosterRow(id)) { e.preventDefault(); show(rosterRow(id), false); track(id); return; }
    order.closest("li").classList.add("is-leaving");
  });

  document.addEventListener("htmx:afterRequest", (e) => {
    if (e.detail.successful) return;
    const roster = rosterButton(e.target);
    const order = orderButton(e.target);
    if (!roster && !order) return;
    const status = e.detail.xhr?.status ?? 0;
    const unreachable = status === 0 || status >= 500;
    const id = idOf(roster || order);
    if (unreachable && rosterRow(id)) {
      if (order) show(rosterRow(id), false);
      track(id);
      return;
    }
    if (roster) {
      const li = roster.closest("li");
      show(li, savedIn(li));
      li.classList.remove("is-pending");
      return;
    }
    order.closest("li").classList.remove("is-leaving");
  });
})();

/* A tap anywhere on a roster row works its Add or Remove button. */
document.addEventListener("click", (e) => {
  const li = e.target.closest?.(".roster-list li");
  if (!li || li.classList.contains("roster-expand") || e.target.closest("button, a, input")) return;
  li.querySelector("button[hx-post]")?.click();
});

/* Keep a tapped roster row exactly where it was on screen. On a phone the
   order list above the roster grows or shrinks by a row with each add or
   remove; Chrome's scroll anchoring absorbs that, but Safari has none, so
   the roster would jump under the finger. After the swap, any movement of
   the tapped row is scrolled back. */
(() => {
  let anchor = null;
  document.addEventListener("htmx:beforeRequest", (e) => {
    const li = e.target.closest?.(".roster-list li[id]");
    anchor = li ? { id: li.id, top: li.getBoundingClientRect().top } : null;
  });
  document.addEventListener("htmx:afterSettle", () => {
    if (!anchor) return;
    const li = document.getElementById(anchor.id);
    if (li) {
      const shift = li.getBoundingClientRect().top - anchor.top;
      if (Math.abs(shift) > 1) window.scrollBy({ top: shift, behavior: "instant" });
    }
    anchor = null;
  });
})();

/* The site header's height, for the other sticky parts to sit beneath it.
   Only while the header is sticky (a wide screen); 0 otherwise. */
(() => {
  const header = document.querySelector("header.brand");
  if (!header) return;
  const wide = window.matchMedia("(min-width: 1000px)");
  const measure = () => {
    const h = wide.matches ? header.getBoundingClientRect().height : 0;
    document.documentElement.style.setProperty("--brand-h", `${Math.ceil(h)}px`);
  };
  measure();
  wide.addEventListener("change", measure);
  if ("ResizeObserver" in window) new ResizeObserver(measure).observe(header);
})();
