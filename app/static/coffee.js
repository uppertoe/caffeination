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
