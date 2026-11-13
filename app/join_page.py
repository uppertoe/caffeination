"""The joiner's page: /join to type a code, /j/<code> to add a drink and
follow the order. States J0 to J18 in the join-by-code plan.

Every form on the page posts back to a route here and gets the page body
(#join-body) back, so the joiner never leaves the page. The rules live in
app.joining; this module decides which state to show and renders it.
"""

from __future__ import annotations

import hashlib
from typing import Callable, Optional

from fastapi import Depends, FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from sqlmodel import Session

from app import joining, sharing
from app.config import get_settings
from app.db import get_session
from app.drinks import HISTORY_FIELDS, format_line, normalize, upsert_saved_drink
from app.identity import read_identity, set_identity
from app.joining import Joiner
from app.models import Order, SavedDrink, User
from app.users import create_named_user, find_user_by_display_name

MODES = ("usual", "oneoff", "guest", "new_usual", "colleague", "change", "location")
STATUS_TITLES = {"ordered": "Ordered", "on_the_way": "On the way", "ready": "Ready", "cancelled": "Cancelled"}


def _possessive(name: str) -> str:
    return f"{name}'s"


def _line(drink: Optional[SavedDrink]) -> str:
    if drink is None:
        return "(no drink saved yet)"
    return format_line(
        drink.base_id, drink.length, drink.size, drink.temp, drink.strength, drink.milk,
        drink.sweetener, drink.notes, drink.shots, drink.own_cup,
    )


def _version(ctx: dict) -> str:
    """A fingerprint of what the joiner's page shows, so a poll that finds
    nothing new swaps nothing. On the landing page, where the joiner may be
    filling in a form, only a change of state counts: the form is replaced
    only when it could no longer be accepted (J5, J6, J7)."""
    if ctx["state"] == "landing":
        return hashlib.sha1(repr(("landing", ctx["accepting"], ctx["join_path"])).encode()).hexdigest()[:12]
    facts = (
        ctx["state"], ctx["lines"], ctx["count"], ctx["max_drinks"], ctx["until"],
        ctx["accepting"], ctx["editable"], ctx["order_location"], ctx["joiner_max_reached"],
        ctx["status"], ctx["status_message"], ctx["order_name"], ctx["owner_name"],
        ctx["join_path"],  # a replaced code changes every URL on the page
    )
    return hashlib.sha1(repr(facts).encode()).hexdigest()[:12]


def register(app: FastAPI, templates: Jinja2Templates, drink_form_ctx: Callable[[Optional[SavedDrink]], dict]) -> None:
    attempts = joining.CodeAttempts()
    settings = get_settings()

    def _client_key(request: Request) -> str:
        return request.client.host if request.client else "unknown"

    def _base_ctx() -> dict:
        return {"app_name": settings.app_name, "tagline": settings.tagline}

    # ---- /join: typing a code (J0 to J2) ------------------------------------

    def _entry(request: Request, state: str = "entry", typed: str = "", status: int = 200, dialog: bool = False):
        ctx = {**_base_ctx(), "state": state, "typed": typed, "page_title": "Join an order"}
        if dialog:
            name = "_join_dialog.html"
        else:
            name = "join.html" if request.headers.get("HX-Request") != "true" else "_join_body.html"
        return templates.TemplateResponse(request, name, ctx, status_code=status)

    @app.get("/join", response_class=HTMLResponse)
    def join_entry(request: Request, code: str = "", dialog: str = "", session: Session = Depends(get_session)):
        # dialog=1: the home page's join dialog, which wants the dialog back
        # and, for a code that matches, an HX-Redirect rather than a 303.
        in_dialog = dialog == "1" and request.headers.get("HX-Request") == "true"
        if not code:
            return _entry(request, dialog=in_dialog)
        key = _client_key(request)
        normal = sharing.normalise_code(code)
        shown = sharing.format_code(normal) if normal else code
        known = frozenset(joining.read_join_cookie(request).orders)
        order = joining.find_order_by_code(session, normal, known=known) if normal else None
        # As in _load: a device that joined this order is never refused.
        if attempts.blocked(key) and (order is None or joining.order_key(order) not in known):
            return _entry(request, "limited", shown, 429, in_dialog)
        if order is None:
            attempts.miss(key)
            return _entry(request, "unknown", shown, 404, in_dialog)
        if in_dialog:
            return HTMLResponse("", headers={"HX-Redirect": sharing.join_path(normal)})
        return RedirectResponse(sharing.join_path(normal), status_code=303)

    # ---- /j/<code>: the order ---------------------------------------------

    def _load(request: Request, session: Session, code: str):
        """(order, joiner, cookie), or a response to send instead."""
        key = _client_key(request)
        normal = sharing.normalise_code(code)
        shown = sharing.format_code(normal) if normal else code
        cookie = joining.read_join_cookie(request)
        order = (
            joining.find_order_by_code(session, normal, known=frozenset(cookie.orders))
            if normal else None
        )
        # The limit is per address, and a department shares one address on
        # the hospital network, so a device that has joined this order is
        # never refused because someone else mistyped a code.
        if attempts.blocked(key) and (order is None or joining.order_key(order) not in cookie.orders):
            return _entry(request, "limited", shown, 429)
        if order is None:
            attempts.miss(key)
            return _entry(request, "unknown", shown, 404)
        user = joining.identity_user(session, read_identity(request))
        if user is not None and user.id == order.owner_id:
            # J17: the buyer's own page for this order.
            home = "/" if order.kind == "everyday" else f"/orders/{order.id}"
            return RedirectResponse(home, status_code=303)
        return order, Joiner(user, cookie.joiner_for(order), cookie.self_for(order)), cookie

    def _page(
        request: Request,
        session: Session,
        order: Order,
        joiner: Joiner,
        cookie: joining.JoinCookie,
        *,
        mode: Optional[str] = None,
        error: Optional[str] = None,
        offline_retry: bool = False,
        status: int = 200,
        form: Optional[dict] = None,
        poll_version: Optional[str] = None,
        removed_note: bool = False,
    ):
        owner = session.get(User, order.owner_id)
        owner_name = owner.display_name if owner and owner.display_name else "The buyer"
        lines = joining.own_lines(session, order, joiner)
        # J16: the buyer added this person from the roster. The device
        # remembers the order, so that they are told if it is cancelled (J15).
        remember = (
            joiner.user is not None
            and bool(lines)
            and order.status in sharing.LIVE_STATUSES
            and joining.order_key(order) not in cookie.orders
        )
        if remember:
            cookie.orders[joining.order_key(order)] = joiner.user.id
        count = joining.drink_count(session, order)
        state = joining.join_state(order, count)
        usual = session.get(SavedDrink, joiner.user.id) if joiner.user else None

        recent = joining.recently_closed(order)
        if state == joining.FINISHED:
            # For an hour, someone with a drink in the order is told how it
            # ended (J13, J15); after that, and for everyone else, J7.
            if order.status == "ready" and lines and recent:
                page_state = "ready"
            elif order.status == "cancelled" and joiner.joiner_id and recent:
                page_state = "cancelled"
            else:
                page_state = "finished"
        elif lines:
            page_state = "joined"
        elif joiner.joiner_id is not None:
            page_state = "removed"  # J14: the buyer removed what this device added
        elif state in (joining.CLOSED, joining.FULL):
            page_state = state
        else:
            page_state = "landing"

        if mode not in MODES:
            mode = None
        if page_state == "landing" and mode not in ("usual", "oneoff", "guest", "new_usual"):
            if joiner.user is None:
                mode = "guest"
            elif usual is None:
                mode = "new_usual"
            else:
                mode = "usual"
        if page_state == "landing" and joiner.user is None and mode != "guest":
            mode = "guest"

        joined_lines = [
            {
                "target_id": line.item.target_user_id,
                "name": line.person.display_name,
                "drink": _line(line.drink),
                "location": order.location or line.item.location,
                "is_self": line.is_self,
                "guest": line.person.one_off,
                "one_off_drink": line.item.base_id is not None and not line.item.drink_fixed,
                "added_by_buyer": line.item.joined_by is None,
            }
            for line in lines
        ]
        added_here = sum(1 for line in lines if line.item.joined_by is not None)
        builder_drink = usual
        if mode == "change" and lines and lines[0].is_self:
            builder_drink = lines[0].drink
        # The tab title leads with the status, so a joiner with the page in a
        # background tab sees it change.
        title = f"{_possessive(owner_name)} order"
        title_status = None
        if page_state == "joined" and order.status in STATUS_TITLES:
            title_status = STATUS_TITLES[order.status]
        elif page_state in ("ready", "cancelled"):
            title_status = STATUS_TITLES[page_state]
        if title_status:
            title = f"{title_status} · {title}"
        ctx = {
            **_base_ctx(),
            **drink_form_ctx(builder_drink),
            "page_title": title,
            "status": order.status,
            "status_message": order.status_message if order.status != "cancelled" else None,
            "is_partial": request.headers.get("HX-Request") == "true",
            "state": page_state,
            "mode": mode,
            "error": error,
            "offline_retry": offline_retry,
            "form": form or {},
            "code": order.code,
            "join_path": sharing.join_path(order.code) if order.code else "",
            "owner_name": owner_name,
            "owner_possessive": _possessive(owner_name),
            "order_name": order.name,
            "order_location": order.location,
            "until": sharing.format_until(order.joins_close_at) if order.joins_close_at else "",
            "until_short": sharing.format_time(order.joins_close_at) if order.joins_close_at else "",
            "count": count,
            "max_drinks": order.max_drinks,
            "default_cap": order.max_drinks == sharing.DEFAULT_MAX_DRINKS,
            "me": joiner.user,
            "usual_line": _line(usual) if usual else None,
            "lines": joined_lines,
            "editable": joining.lines_editable(order),
            "accepting": state == joining.ACCEPTING,
            "full": state == joining.FULL,
            "removed_note": removed_note,
            "joiner_max_reached": added_here >= joining.JOINER_MAX,
            "joiner_max": joining.JOINER_MAX,
            "suggestions": joining.suggested_places(session),
            "last_location": cookie.location or "",
            "number_word": sharing.number_word,
            # The guest form's joiner id, for a device that has none yet.
            "joiner_token": joining.joiner_token(joining.new_joiner_id()) if joiner.key is None else None,
        }
        # Polled only where nothing is being typed: the joiner's own lines,
        # or a closed or full order that may open up again.
        ctx["live"] = order.status in sharing.LIVE_STATUSES and (
            page_state == "landing"
            or (mode in (None, "usual") and page_state in ("joined", "closed", "full"))
        )
        ctx["live_version"] = _version(ctx)
        if poll_version is not None and poll_version == ctx["live_version"]:
            response = Response(status_code=204)
        else:
            name = "join.html" if request.headers.get("HX-Request") != "true" else "_join_body.html"
            response = templates.TemplateResponse(request, name, ctx, status_code=status)
        if remember:
            joining.write_join_cookie(request, response, cookie)
        return response

    @app.get("/j/{code}", response_class=HTMLResponse)
    def join_order(
        request: Request, code: str, mode: str = "", v: Optional[str] = None,
        session: Session = Depends(get_session),
    ):
        loaded = _load(request, session, code)
        if not isinstance(loaded, tuple):
            return loaded
        order, joiner, cookie = loaded
        return _page(request, session, order, joiner, cookie, mode=mode or None, poll_version=v)

    # ---- /join/save/<code>: a guest keeps their drink as their usual ---------
    # Behind the gateway off the hospital network (unlike /j/), so only staff
    # join the roster. The join link itself never gives an app identity.

    def _saving_guest(request: Request, session: Session, code: str):
        """(guest, their drink) for this device's own guest line in the
        order, or None. None too once the device is signed in."""
        if joining.identity_user(session, read_identity(request)) is not None:
            return None
        normal = sharing.normalise_code(code)
        cookie = joining.read_join_cookie(request)
        order = (
            joining.find_order_by_code(session, normal, known=frozenset(cookie.orders))
            if normal else None
        )
        self_id = cookie.self_for(order) if order is not None else None
        guest = session.get(User, self_id) if self_id else None
        drink = session.get(SavedDrink, self_id) if self_id else None
        if guest is None or not guest.one_off or not guest.display_name or drink is None:
            return None
        return guest, drink

    def _save_as(request: Request, session: Session, person: User, drink: SavedDrink):
        upsert_saved_drink(session, person.id, {f: getattr(drink, f) for f in HISTORY_FIELDS})
        set_identity(request, person.id)
        return RedirectResponse("/", status_code=303)

    def _name_taken(request, session, code, guest, drink, existing, *, typed=None, error=None, status=200):
        """The guest's name is already on the roster: the existing person's
        drink and the guest's side by side, to claim the name or choose
        another."""
        theirs = session.get(SavedDrink, existing.id)
        same = theirs is not None and all(getattr(theirs, f) == getattr(drink, f) for f in HISTORY_FIELDS)
        ctx = {
            **_base_ctx(),
            "page_title": "Save your usual drink",
            "code": code,
            "name": existing.display_name,
            "old_drink": _line(theirs) if theirs else None,
            "new_drink": _line(drink),
            "same": same,
            "typed": guest.display_name if typed is None else typed,
            "error": error,
        }
        return templates.TemplateResponse(request, "join_save.html", ctx, status_code=status)

    @app.get("/join/save/{code}")
    def join_save_usual(request: Request, code: str, session: Session = Depends(get_session)):
        found = _saving_guest(request, session, code)
        if found is None:
            return RedirectResponse("/", status_code=303)
        guest, drink = found
        existing = find_user_by_display_name(session, guest.display_name)
        if existing is not None:
            return _name_taken(request, session, code, guest, drink, existing)
        return _save_as(request, session, create_named_user(session, guest.display_name), drink)

    @app.post("/join/save/{code}/claim")
    def join_save_claim(request: Request, code: str, session: Session = Depends(get_session)):
        """"I am Tom": this device becomes the roster's Tom, whose usual drink
        becomes the guest's. The old drink stays in the drink history."""
        found = _saving_guest(request, session, code)
        if found is None:
            return RedirectResponse("/", status_code=303)
        guest, drink = found
        existing = find_user_by_display_name(session, guest.display_name)
        if existing is None:
            existing = create_named_user(session, guest.display_name)
        return _save_as(request, session, existing, drink)

    @app.post("/join/save/{code}/new")
    async def join_save_new(request: Request, code: str, session: Session = Depends(get_session)):
        found = _saving_guest(request, session, code)
        if found is None:
            return RedirectResponse("/", status_code=303)
        guest, drink = found
        typed = str((await request.form()).get("display_name", "")).strip()[:40]
        existing = find_user_by_display_name(session, guest.display_name)
        if not typed or find_user_by_display_name(session, typed) is not None:
            if existing is None:  # the guest's own name came free meanwhile
                return RedirectResponse(f"/join/save/{code}", status_code=303)
            error = "Please enter your name." if not typed else "That name is already on the list."
            return _name_taken(request, session, code, guest, drink, existing, typed=typed, error=error, status=422)
        return _save_as(request, session, create_named_user(session, typed), drink)

    _REFUSALS = {
        "joiner_max": "Each person can add up to two drinks.",
        "no_drink": "Please choose a drink.",
    }

    def _after_change(request, session, order, joiner, cookie, *, location=None, remember=True):
        if location is not None:
            cookie.location = location
        if remember and joiner.key is not None:
            cookie.orders[joining.order_key(order)] = joiner.key
        response = _page(request, session, order, joiner, cookie)
        joining.write_join_cookie(request, response, cookie)
        return response

    def _refused(request, session, order, joiner, cookie, exc: joining.JoinRefused, mode, form):
        if exc.reason in _REFUSALS:
            return _page(
                request, session, order, joiner, cookie,
                mode=mode, error=_REFUSALS[exc.reason], status=409, form=form,
            )
        # Closed, full, finished or already in: the page shows which.
        return _page(request, session, order, joiner, cookie, status=409)

    @app.post("/j/{code}/usual", response_class=HTMLResponse)
    async def join_usual(request: Request, code: str, session: Session = Depends(get_session)):
        loaded = _load(request, session, code)
        if not isinstance(loaded, tuple):
            return loaded
        order, joiner, cookie = loaded
        form = dict(await request.form())
        if joiner.user is None:
            return _page(request, session, order, joiner, cookie)
        location = joining.clean_location(form.get("location"))
        try:
            joining.join_as_self(session, order, joiner.user, location=location)
        except joining.JoinRefused as exc:
            return _refused(request, session, order, joiner, cookie, exc, "usual", form)
        return _after_change(request, session, order, joiner, cookie, location=location)

    @app.post("/j/{code}/one-off", response_class=HTMLResponse)
    async def join_one_off(request: Request, code: str, session: Session = Depends(get_session)):
        """The joiner's own drink from the builder: a one-off, or saved as
        their usual. Also swaps an existing line of theirs to a one-off."""
        loaded = _load(request, session, code)
        if not isinstance(loaded, tuple):
            return loaded
        order, joiner, cookie = loaded
        form = dict(await request.form())
        if joiner.user is None:
            return _page(request, session, order, joiner, cookie)
        drink = normalize(form.get("base_id", ""), form)
        mode = form.get("mode") or "oneoff"
        if drink is None:
            return _page(request, session, order, joiner, cookie, mode=mode, error="Please choose a drink.", status=422, form=form)
        location = joining.clean_location(form.get("location"))
        existing = [line for line in joining.own_lines(session, order, joiner) if line.is_self]
        if existing:
            joining.set_line_drink(session, order, joiner, joiner.user.id, drink)
            return _after_change(request, session, order, joiner, cookie)
        try:
            joining.join_as_self(
                session, order, joiner.user, location=location, drink=drink,
                save_as_usual=form.get("save_usual") == "1",
            )
        except joining.JoinRefused as exc:
            return _refused(request, session, order, joiner, cookie, exc, mode, form)
        return _after_change(request, session, order, joiner, cookie, location=location)

    @app.post("/j/{code}/guest", response_class=HTMLResponse)
    async def join_guest(request: Request, code: str, session: Session = Depends(get_session)):
        """A guest's drink, or a drink for a colleague: a named one-off person."""
        loaded = _load(request, session, code)
        if not isinstance(loaded, tuple):
            return loaded
        order, joiner, cookie = loaded
        form = dict(await request.form())
        mode = form.get("mode") or "guest"
        name = " ".join((form.get("display_name") or "").split())
        if not name:
            return _page(request, session, order, joiner, cookie, mode=mode, error="Please enter your colleague's name." if mode == "colleague" else "Please enter your name.", status=422, form=form)
        drink = normalize(form.get("base_id", ""), form)
        if drink is None:
            return _page(request, session, order, joiner, cookie, mode=mode, error="Please choose a drink.", status=422, form=form)
        if joiner.key is None:
            # A device with no joiner id yet uses the one its form was given.
            joiner = Joiner(
                None, joining.read_joiner_token(form.get("joiner")) or joining.new_joiner_id()
            )
        location = joining.clean_location(form.get("location"))
        try:
            guest = joining.join_as_guest(session, order, joiner.key, name=name, drink=drink, location=location)
        except joining.JoinRefused as exc:
            return _refused(request, session, order, joiner, cookie, exc, mode, form)
        if mode == "guest" and joiner.user is None:
            # The guest's own drink, as distinct from one for a colleague.
            cookie.selves[joining.order_key(order)] = guest.id
            joiner = Joiner(None, joiner.joiner_id, guest.id)
        return _after_change(request, session, order, joiner, cookie, location=location)

    @app.post("/j/{code}/line/{target_id}/location", response_class=HTMLResponse)
    async def line_location(request: Request, code: str, target_id: str, session: Session = Depends(get_session)):
        loaded = _load(request, session, code)
        if not isinstance(loaded, tuple):
            return loaded
        order, joiner, cookie = loaded
        form = dict(await request.form())
        location = joining.clean_location(form.get("location"))
        joining.set_line_location(session, order, joiner, target_id, location)
        return _after_change(request, session, order, joiner, cookie, location=location, remember=False)

    @app.post("/j/{code}/line/{target_id}/usual", response_class=HTMLResponse)
    def line_back_to_usual(request: Request, code: str, target_id: str, session: Session = Depends(get_session)):
        loaded = _load(request, session, code)
        if not isinstance(loaded, tuple):
            return loaded
        order, joiner, cookie = loaded
        joining.set_line_drink(session, order, joiner, target_id, None)
        return _page(request, session, order, joiner, cookie)

    @app.post("/j/{code}/line/{target_id}/remove", response_class=HTMLResponse)
    def line_remove(request: Request, code: str, target_id: str, session: Session = Depends(get_session)):
        loaded = _load(request, session, code)
        if not isinstance(loaded, tuple):
            return loaded
        order, joiner, cookie = loaded
        own = any(line.is_self and line.item.target_user_id == target_id
                  for line in joining.own_lines(session, order, joiner))
        removed = joining.remove_line(session, order, joiner, target_id)
        if target_id == joiner.self_id:
            cookie.selves.pop(joining.order_key(order), None)
            joiner = Joiner(joiner.user, joiner.joiner_id, None)
        if not joining.own_lines(session, order, joiner):
            # Nothing left from this device: back to the landing page, not
            # "the buyer removed your drink".
            cookie.orders.pop(joining.order_key(order), None)
            joiner = Joiner(joiner.user, None)
        response = _page(request, session, order, joiner, cookie, removed_note=removed and own)
        joining.write_join_cookie(request, response, cookie)
        return response
