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

/* Two columns of different lengths on a wide screen (the order pages): the
   longer column scrolls with the page, and the shorter one scrolls with it
   until an end comes into view, then holds there. Scrolling down, it holds
   once its bottom is in view; scrolling back up, it moves straight away and
   holds once its top is in view. Neither column needs a scroll box.

   The holding is done by the browser with position: sticky (see .dash-left
   and .order-side in coffee.css), so a held column never lags the scroll.
   This script only acts when the scroll direction changes: it places the
   column where it is now, so that nothing moves, and sets the sticky edge
   for the new direction. On the way down the column is placed from the top
   of its grid area with a top margin, and a negative top catches its bottom.
   On the way up it is placed from the bottom of the area with a bottom
   margin, and a negative bottom catches its top. (A sticky element may not
   move its margin outside its grid area, so the margin always sits on the
   side the column is moving away from.) */
(() => {
  const wide = window.matchMedia("(min-width: 1000px)");
  const GAP = 16; // px kept between a column held by its bottom and the window edge
  let lastY = window.scrollY;
  let held = null; // the column being held
  let mode = "top"; // which end it is held by: "top" or "bottom"
  let frame = 0;
  let lastOffset = null; // the held column's offset at the previous update

  const columns = () => {
    const layout = document.querySelector(".dash-layout");
    const left = layout && layout.querySelector(":scope > .dash-left");
    const right = layout && layout.querySelector(":scope > .order-side");
    return left && right ? { layout, left, right } : null;
  };
  const tall = (el) => el.getBoundingClientRect().height;
  const reset = (el) => {
    el.style.top = el.style.bottom = el.style.marginTop = el.style.marginBottom = el.style.alignSelf = "";
  };
  // Where a column held by its top sits: just as far below the header as
  // the columns start at the top of the page (the header's own bottom
  // margin), so that nothing shifts when a held column meets the top again.
  const topEdge = () => {
    const brand = document.querySelector("header.brand");
    if (!brand) return GAP;
    return brand.getBoundingClientRect().bottom + parseFloat(getComputedStyle(brand).marginBottom);
  };

  // How far a column sits below the top of its grid row.
  const offsetOf = (el) => el.getBoundingClientRect().top - el.parentElement.getBoundingClientRect().top;

  // Hold `el` for the given direction, from the offset `from` (where it is
  // now, if not given). On a turn, `from` is where the column was at the
  // previous update: the browser has already drawn this frame's scroll with
  // the old sticky edge, so a column held by that edge has not moved with it,
  // and starting from where it is now would lose the first step of the turn.
  const hold = (el, longHeight, nextMode, from) => {
    const view = window.innerHeight;
    const top = topEdge();
    const height = el.getBoundingClientRect().height; // unrounded, like the grid row
    const room = Math.max(0, longHeight - height);
    const offset = Math.min(Math.max(0, from ?? offsetOf(el)), room);
    reset(el);
    if (height <= view - top - GAP) {
      // Short enough to fit: simply held at the top.
      el.style.top = `${top}px`;
    } else if (nextMode === "bottom") {
      el.style.marginTop = `${offset}px`;
      el.style.top = `${view - GAP - height}px`;
    } else {
      el.style.alignSelf = "end";
      el.style.marginBottom = `${room - offset}px`;
      el.style.bottom = `${view - height - top}px`;
    }
    mode = nextMode;
  };

  const update = (force) => {
    frame = 0;
    const cols = columns();
    const y = window.scrollY;
    const down = y > lastY;
    const turned = y !== lastY && down !== (mode === "bottom");
    lastY = y;
    if (!cols) return;
    if (!wide.matches) {
      reset(cols.left);
      reset(cols.right);
      held = null;
      lastOffset = null;
      return;
    }
    const short = tall(cols.left) <= tall(cols.right) ? cols.left : cols.right;
    const long = short === cols.left ? cols.right : cols.left;
    if (held !== short) {
      reset(long);
      reset(short);
      held = short;
      lastOffset = null;
      force = true;
    }
    if (y <= 0) {
      // Back at the top: line the columns up again, ready to scroll down.
      reset(short);
      hold(short, tall(long), "bottom", 0);
    } else if (turned) hold(short, tall(long), down ? "bottom" : "top", lastOffset ?? undefined);
    else if (force) hold(short, tall(long), mode);
    lastOffset = offsetOf(short);
  };
  const schedule = () => {
    if (!frame) frame = requestAnimationFrame(() => update(false));
  };
  const refresh = () => requestAnimationFrame(() => update(true));

  window.addEventListener("scroll", schedule, { passive: true });
  window.addEventListener("resize", refresh);
  wide.addEventListener("change", refresh);
  // A column's height changes when htmx swaps part of it, or Alpine shows or
  // hides something; the right-hand column is replaced by some swaps.
  if ("ResizeObserver" in window) {
    const watcher = new ResizeObserver(refresh);
    const watch = () => {
      watcher.disconnect();
      held = null;
      const cols = columns();
      if (cols) [cols.left, cols.right].forEach((el) => watcher.observe(el));
      refresh();
    };
    document.addEventListener("htmx:afterSettle", watch);
    watch();
  }
  refresh();
})();

/* The buyer's shared order: telling the buyer when colleagues add drinks
   by code. The sharing panel carries the drinks added by code (in
   data-joins) and comes back with every change the poll finds, so each new
   panel is compared with the drinks this tab has already seen. While the
   buyer is looking elsewhere, new drinks are counted in the tab title and,
   with the browser's permission, shown as a system notification. Nothing
   is announced for the drinks already there when the page loads. */
(() => {
  const seen = new Map(); // order (its "ob" path) -> Set of line ids
  let unseen = 0;
  let baseTitle = null;

  const away = () => document.visibilityState !== "visible" || !document.hasFocus();
  const showTitle = () => {
    if (baseTitle === null) baseTitle = document.title.replace(/^\(\d+\) /, "");
    document.title = unseen ? `(${unseen}) ${baseTitle}` : baseTitle;
  };
  const back = () => {
    if (!unseen || away()) return;
    unseen = 0;
    showTitle();
  };
  document.addEventListener("visibilitychange", back);
  window.addEventListener("focus", back);

  const capitalise = (s) => (s ? s[0].toUpperCase() + s.slice(1) : s);

  const notify = (added) => {
    if (!("Notification" in window) || Notification.permission !== "granted") return;
    const [title, body] = added.length === 1
      ? [`${added[0].name} has added a drink`, capitalise(added[0].drink)]
      : [`${added.length} colleagues have added drinks`, undefined];
    const options = { body, tag: "caffeine-joins", icon: "/static/icon-192.png" };
    /* Phones show notifications only through a service worker (/sw.js);
       a desktop browser without one falls back to a plain notification. */
    if (navigator.serviceWorker) {
      navigator.serviceWorker.getRegistration("/").then((reg) => {
        if (reg) return reg.showNotification(title, options);
        new Notification(title, options);
      }).catch(() => {});
    } else {
      try { new Notification(title, options); } catch (_) { /* the title still shows the count */ }
    }
  };

  const check = () => {
    const panel = document.querySelector("#share-panel[data-joins]");
    if (!panel) return;
    let joins;
    try { joins = JSON.parse(panel.dataset.joins); } catch (_) { return; }
    const key = panel.dataset.order || "";
    const ids = new Set(joins.map((j) => j.id));
    const before = seen.get(key);
    seen.set(key, ids);
    if (!before) return;
    const added = joins.filter((j) => !before.has(j.id));
    if (!added.length || !away()) return;
    unseen += added.length;
    showTitle();
    notify(added);
  };
  document.addEventListener("htmx:afterSettle", check);
  document.addEventListener("DOMContentLoaded", check);

  /* The notify control in the sharing panel. */
  window.caffeineNotify = () => ({
    supported: "Notification" in window,
    permission: "Notification" in window ? Notification.permission : "denied",
    init() {
      if (this.supported && this.permission === "granted") registerWorker();
    },
    ask() {
      Promise.resolve(Notification.requestPermission()).then((p) => {
        this.permission = p;
        if (p === "granted") registerWorker();
      });
    },
  });
  const registerWorker = () => {
    if (navigator.serviceWorker) navigator.serviceWorker.register("/sw.js").catch(() => {});
  };
})();

/* The dashboard's way-finding.

   On a phone or tablet (one column), the section bar (.dash-sections)
   marks the section in view, the way the statistics page's contents bar
   does, and carries the order's drink count and a dot while the shared
   order is taking drinks.

   On a wide screen, the panel last clicked or typed in wears a ring, so
   it is clear which one is working. Swaps replace panels, so the ring is
   put back on the new element after each one. */
(() => {
  const bar = () => document.querySelector(".dash-sections");

  const mark = (id) => {
    for (const a of bar()?.querySelectorAll("a") ?? []) {
      const on = a.hash === "#" + id;
      a.classList.toggle("is-current", on);
      if (on) a.setAttribute("aria-current", "location");
      else a.removeAttribute("aria-current");
    }
  };

  const facts = () => {
    const nav = bar();
    if (!nav) return;
    const list = document.getElementById("order-list");
    const count = nav.querySelector("[data-drink-count]");
    if (list && count) {
      const n = Number(list.dataset.drinks || 0);
      count.textContent = n;
      count.hidden = !n;
    }
    const panel = document.getElementById("share-panel");
    const live = nav.querySelector(".dash-live");
    if (live) {
      live.hidden = !(panel && panel.classList.contains("share-panel")
        && !panel.matches(".is-closed, .is-placed, .is-complete"));
    }
  };

  /* The section in view: the last one whose top has passed a line a
     little below the bar, or the last section once the page can scroll
     no further. A tapped label is marked at once and keeps its mark
     until the reader scrolls by hand (a wheel, a touch or a key) or taps
     something else, even when the page cannot scroll that section all
     the way up. */
  let pinned = false;
  const current = () => {
    const nav = bar();
    if (!nav || pinned || getComputedStyle(nav).display === "none") return;
    const ids = [...nav.querySelectorAll("a")].map((a) => a.hash.slice(1));
    const line = nav.getBoundingClientRect().bottom + 80;
    const atEnd = window.scrollY + window.innerHeight >= document.documentElement.scrollHeight - 4;
    let id = ids[0];
    for (const candidate of ids) {
      const el = document.getElementById(candidate);
      if (el && el.getBoundingClientRect().top <= line) id = candidate;
    }
    if (atEnd) id = ids[ids.length - 1];
    mark(id);
  };
  let ticking = false;
  window.addEventListener("scroll", () => {
    if (ticking) return;
    ticking = true;
    requestAnimationFrame(() => { ticking = false; current(); });
  }, { passive: true });
  document.addEventListener("click", (event) => {
    const a = event.target.closest?.(".dash-sections a");
    // Any other tap is the reader moving on: the mark follows the page
    // again, including a scroll that the tap's response makes.
    if (!a) { pinned = false; return; }
    mark(a.hash.slice(1));
    pinned = true;
  });
  for (const type of ["wheel", "touchstart", "keydown"]) {
    window.addEventListener(type, () => { pinned = false; }, { passive: true });
  }
  const arm = current;

  const PANELS = "#dash-you, #order-list, #till-summary, #share-panel, #advance-orders, #roster-picker";
  let active = null;
  const ring = () => {
    document.querySelectorAll(".dash-layout .is-active").forEach((el) => el.classList.remove("is-active"));
    const el = active && document.getElementById(active);
    if (el && el.tagName === "ARTICLE") el.classList.add("is-active");
  };
  const choose = (event) => {
    const panel = event.target.closest?.(PANELS);
    if (!panel || !panel.closest(".dash-layout")) return;
    active = panel.id;
    ring();
  };
  document.addEventListener("pointerdown", choose);
  document.addEventListener("focusin", choose);

  const refresh = () => { arm(); facts(); ring(); };
  document.addEventListener("htmx:afterSettle", refresh);
  document.addEventListener("DOMContentLoaded", refresh);
})();

/* An advance order's page: the slim strip takes the band's place at the top
   once the band has scrolled out of view, and both carry the panel's
   closing time as it changes. */
(() => {
  const band = document.getElementById("advance-band");
  if (!band) return;
  if ("IntersectionObserver" in window) {
    new IntersectionObserver(([entry]) => {
      document.documentElement.classList.toggle("band-gone", !entry.isIntersecting);
    }).observe(band);
  }
  const closes = () => {
    const text = document.getElementById("share-panel")?.dataset.closes;
    if (!text) return;
    document.querySelectorAll("[data-advance-closes]").forEach((el) => { el.textContent = text; });
  };
  document.addEventListener("htmx:afterSettle", closes);
})();
