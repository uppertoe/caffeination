"""The buyer's order: the everyday order on the home page (/order/...) and
advance orders on their own pages (/orders/<id>/...).

Both kinds use the same templates and the same handlers. Each handler is
registered twice, once under /order for the everyday order and once under
/orders/{order_id} for an advance order; under /order, order_id is None.
Templates build their URLs from `ob` (the order's base path) and `oq` (the
query string the /people routes need to know which order they are in).
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import Depends, FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from sqlmodel import Session

from app import joining, sharing
from app.db import get_session
from app.models import Order, User
from app.orders import (
    ADVANCE,
    add_to_order,
    clear_order,
    live_order,
    load_order_view,
    order_for_write,
    remove_from_order,
    till_summary,
)
from app.users import get_current_user

# Responses that change order data outside the order section carry this
# HX-Trigger header. The order section listens for the event and refetches
# itself — EXCEPT while an inline person-edit form has replaced it (the form
# deliberately carries no listener), so a drink save or rename can't clobber
# a half-finished edit. The section re-renders fresh on the edit's own
# save/cancel anyway.
ORDER_REFRESH = {"HX-Trigger": "order-refresh"}


def order_base(order_id: Optional[int]) -> str:
    return "/order" if order_id is None else f"/orders/{order_id}"


def order_query(order_id: Optional[int]) -> str:
    return "" if order_id is None else f"?order={order_id}"


def parse_order_id(raw) -> Optional[int]:
    try:
        return int(raw) if raw not in (None, "") else None
    except (TypeError, ValueError):
        return None


def gone(request: Request):
    """An advance order that is no longer live (finished in another tab, or
    expired): its page has nothing to show, so back to the home page."""
    if request.headers.get("HX-Request") == "true":
        return Response(status_code=204, headers={"HX-Redirect": "/"})
    return RedirectResponse("/", status_code=303)


def is_gone(ctx: dict) -> bool:
    """The context is for an advance order that is no longer live."""
    return ctx["order_id"] is not None and ctx["order"] is None


# ---------------------------------------------------------------------------
# Template context
# ---------------------------------------------------------------------------


def order_section_ctx(session: Session, user: User, order_id: Optional[int] = None) -> dict:
    view = load_order_view(session, user, order_id)
    drink_count = sum(1 for row in view.rows if row.saved is not None)
    share = sharing.share_view(
        view.order, drink_count, by_code=sum(1 for row in view.rows if row.via_code)
    )
    advance = [sharing.share_view(a.order, a.drink_count) for a in view.advance]
    advance = [(a.order.id, s) for a, s in zip(view.advance, advance) if s is not None]
    ctx = {
        "rows": view.rows,
        "user": user,
        # The "you're not in this order" row only makes sense once there is
        # a drink to opt back in with.
        "self_excluded": view.self_excluded and view.owner_drink is not None,
        "roster": view.roster,
        "roster_inactive": view.roster_inactive,
        "till_lines": till_summary(view.rows),
        # For the order bar pinned to the foot of the screen on a phone.
        "drink_count": drink_count,
        "share": share,
        # Drinks added by code, for the tab's "has added a drink" notices.
        "code_joins": [
            {"id": row.user.id, "name": row.user.display_name or "A colleague", "drink": row.line}
            for row in view.rows if row.via_code
        ],
        "row_groups": _row_groups(view.rows, share),
        # Polled while the order is shared and open, so drinks added by code
        # appear without a reload (see GET /order/live).
        "live": share is not None and view.order is not None and view.order.status == "open",
        "order_id": order_id,
        "order": view.order,
        "ob": order_base(order_id),
        "oq": order_query(order_id),
        # The everyday order's page lists the advance orders (O0), and
        # reminds the buyer of any whose joins have closed (O0b).
        "advance": advance,
        "reminders": [
            (oid, s) for oid, s in advance if s.closed and not s.placed
        ],
    }
    ctx["live_version"] = _live_version(ctx)
    return ctx


def _live_version(ctx: dict) -> str:
    """A short fingerprint of what the buyer's live parts show (the list,
    the summary and the sharing panel), so a poll that finds nothing new
    swaps nothing and leaves an open sheet or a hovered button alone."""
    share = ctx["share"]
    facts = (
        ctx["till_lines"],
        [(r.user.id, r.user.display_name, r.line, r.location, r.via_code, r.one_off_drink)
         for r in ctx["rows"]],
        ctx["self_excluded"],
        share and (share.code, share.closes_label, share.closed, share.full, share.drink_count,
                   share.max_drinks, share.by_code, share.location, share.name,
                   share.status, share.status_label, share.status_message),
    )
    return hashlib.sha1(repr(facts).encode()).hexdigest()[:12]


def _row_groups(rows: list, share) -> list[tuple]:
    """The order's rows, grouped by location for the delivery run when a
    shared order has drinks going to different places; otherwise one
    unlabelled group. Drinks with no location (collected from the buyer)
    are listed last."""
    if share is None or share.location or not any(row.location for row in rows):
        return [(None, rows)]
    groups: dict = {}
    for row in rows:
        groups.setdefault(row.location, []).append(row)
    located = [(place, members) for place, members in groups.items() if place]
    return located + ([("Collected from you", groups[None])] if None in groups else [])


def share_form_ctx(
    form: Optional[dict] = None, error: Optional[str] = None, *, advance: bool = False
) -> dict:
    """The sharing form (O1b), blank or as submitted. Planning an advance
    order uses the same form, with a closing time and a name required."""
    form = form or {}
    now = datetime.now(timezone.utc)
    return {
        "share_form": {
            "advance": advance,
            "when": "chosen" if advance or form.get("when") == "chosen" else "soon",
            "closes_at": form.get("closes_at")
            or sharing.local_input_value(now + timedelta(days=1)),
            "min": sharing.local_input_value(now),
            "max": sharing.local_input_value(now + sharing.MAX_AHEAD),
            "name": form.get("name", ""),
            "location": form.get("location", ""),
            "max_drinks": sharing.clamp_max_drinks(
                form.get("max_drinks", sharing.DEFAULT_MAX_DRINKS)
            ),
            "error": error,
        }
    }


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


def register(app: FastAPI, templates: Jinja2Templates, page_ctx) -> None:
    """page_ctx(session, user) gives the base page's context (app name and
    so on) for the advance order's own page."""

    def both(method: str, suffix: str):
        """Register a handler for the everyday order and an advance order."""

        def decorator(fn):
            route = getattr(app, method)
            route("/order" + suffix, response_class=HTMLResponse)(fn)
            route("/orders/{order_id}" + suffix, response_class=HTMLResponse)(fn)
            return fn

        return decorator

    def section(request: Request, session: Session, user: User, order_id, extra=None, status=200):
        ctx = {**order_section_ctx(session, user, order_id), **(extra or {})}
        if is_gone(ctx):
            return gone(request)
        return templates.TemplateResponse(request, "_order_section.html", ctx, status_code=status)

    def change_response(request: Request, session: Session, user: User, target_id: str, order_id):
        """After an add or remove: from a roster row, that row in its new
        state plus the order list and till summary out of band; from "Your
        order" (?from=list), the order list, till summary and the person's
        roster row, all out of band; otherwise, the whole order section."""
        ctx = order_section_ctx(session, user, order_id)
        if is_gone(ctx):
            return gone(request)
        entry, inactive = None, False
        for entries, is_inactive in ((ctx["roster"], False), (ctx["roster_inactive"], True)):
            found = next((p for p in entries if p.id == target_id), None)
            if found is not None:
                entry, inactive = found, is_inactive
        oob = {**ctx, "oob": True, "entry": entry, "entry_inactive": inactive}
        if request.query_params.get("from") == "list":
            return templates.TemplateResponse(request, "_order_list_change.html", oob)
        if entry is not None and request.headers.get("HX-Target", "") == f"roster-{target_id}":
            return templates.TemplateResponse(request, "_roster_toggle.html", oob)
        return templates.TemplateResponse(request, "_order_section.html", ctx)

    # ---- Planning an advance order (O1b) and its own page (O2b) -----------

    # The sharing and planning forms open in a dialog (_share_dialog.html),
    # loaded into #share-dialog; a refused form comes back into it.

    def share_dialog(request: Request, ctx: dict, status: int = 200):
        return templates.TemplateResponse(request, "_share_dialog.html", ctx, status_code=status)

    @app.get("/orders/new", response_class=HTMLResponse)
    def plan_form(request: Request, user: User = Depends(get_current_user)):
        return share_dialog(request, share_form_ctx(advance=True))

    @app.post("/orders", response_class=HTMLResponse)
    async def plan_submit(
        request: Request,
        user: User = Depends(get_current_user),
        session: Session = Depends(get_session),
    ):
        form = dict(await request.form())
        try:
            if not " ".join((form.get("name") or "").split()):
                raise sharing.ShareError("Please enter a name for the order.")
            closes_at = sharing.parse_closing_time(form.get("closes_at", ""))
        except sharing.ShareError as exc:
            return share_dialog(request, share_form_ctx(form, str(exc), advance=True), 422)
        order = Order(owner_id=user.id, kind=ADVANCE)
        session.add(order)
        session.flush()
        sharing.share_order(
            session, order, closes_at=closes_at, name=form.get("name"),
            location=form.get("location"),
            max_drinks=form.get("max_drinks", sharing.DEFAULT_MAX_DRINKS),
        )
        return Response(status_code=204, headers={"HX-Redirect": f"/orders/{order.id}"})

    @app.get("/orders/{order_id}", response_class=HTMLResponse)
    def advance_page(
        order_id: int,
        request: Request,
        user: User = Depends(get_current_user),
        session: Session = Depends(get_session),
    ):
        """An advance order's own page or, for htmx, just its order section
        (the order-refresh event and the edit form's Cancel)."""
        ctx = order_section_ctx(session, user, order_id)
        if is_gone(ctx):
            return gone(request)
        if request.headers.get("HX-Request") == "true":
            return templates.TemplateResponse(request, "_order_section.html", ctx)
        share = ctx["share"]
        # The band's closing line (kept current by coffee.js from the panel).
        closes = ("Locked" if share.closed else f"Closes {share.closes_label}") if share else ""
        return templates.TemplateResponse(
            request, "order_page.html", {**page_ctx(session, user), **ctx, "share_closes_text": closes}
        )

    # ---- The order section ------------------------------------------------

    @app.get("/order", response_class=HTMLResponse)
    def order_section(
        request: Request,
        user: User = Depends(get_current_user),
        session: Session = Depends(get_session),
    ):
        """Re-render the order section in-band (used to cancel an inline edit)."""
        return section(request, session, user, None)

    @both("post", "/add/{target_id}")
    def order_add(
        target_id: str,
        request: Request,
        order_id: Optional[int] = None,
        user: User = Depends(get_current_user),
        session: Session = Depends(get_session),
    ):
        """Add a person to the order.

        From a roster row (htmx reports the row's id as HX-Target) the row
        comes back selected, with the two articles that changed out of band.
        Every other caller — the "Add me" opt-in, tests, curl — gets the
        whole section.
        """
        add_to_order(session, user.id, target_id, order_id)
        return change_response(request, session, user, target_id, order_id)

    @both("post", "/remove/{target_id}")
    def order_remove(
        target_id: str,
        request: Request,
        order_id: Optional[int] = None,
        user: User = Depends(get_current_user),
        session: Session = Depends(get_session),
    ):
        remove_from_order(session, user.id, target_id, order_id)
        return change_response(request, session, user, target_id, order_id)

    @app.get("/orders/{order_id}/delete", response_class=HTMLResponse)
    def advance_delete_confirm(
        order_id: int,
        request: Request,
        user: User = Depends(get_current_user),
        session: Session = Depends(get_session),
    ):
        """The Delete dialog of an advance order's band, with its counts as
        they stand now."""
        ctx = order_section_ctx(session, user, order_id)
        if is_gone(ctx):
            return gone(request)
        return templates.TemplateResponse(request, "_advance_delete.html", ctx)

    @both("post", "/clear")
    def order_clear(
        request: Request,
        order_id: Optional[int] = None,
        user: User = Depends(get_current_user),
        session: Session = Depends(get_session),
    ):
        clear_order(session, user.id, order_id=order_id)
        if order_id is not None:
            return gone(request)  # an advance order's page has nothing left to show
        return section(request, session, user, None)

    @both("get", "/live")
    def order_live(
        request: Request,
        v: str = "",
        order_id: Optional[int] = None,
        user: User = Depends(get_current_user),
        session: Session = Depends(get_session),
    ):
        """The buyer's poll: nothing (204) if the order looks as it did,
        otherwise the order list, till summary and sharing panel out of band."""
        ctx = order_section_ctx(session, user, order_id)
        if is_gone(ctx):
            return gone(request)
        if v and v == ctx["live_version"]:
            return Response(status_code=204)
        return templates.TemplateResponse(request, "_order_live.html", {**ctx, "oob": True})

    # ---- Sharing (app.sharing) ----------------------------------------------
    # Each returns the sharing panel, which swaps itself (#share-panel).

    def share_panel_response(request, session, user, order_id, extra=None, status=200):
        ctx = {**order_section_ctx(session, user, order_id), **(extra or {})}
        if is_gone(ctx):
            return gone(request)
        return templates.TemplateResponse(request, "_share_panel.html", ctx, status_code=status)

    @app.get("/order/share", response_class=HTMLResponse)
    def share_form(
        request: Request,
        user: User = Depends(get_current_user),
        session: Session = Depends(get_session),
    ):
        return share_dialog(request, share_form_ctx())

    @both("get", "/share/panel")
    def share_panel(
        request: Request,
        order_id: Optional[int] = None,
        user: User = Depends(get_current_user),
        session: Session = Depends(get_session),
    ):
        return share_panel_response(request, session, user, order_id)

    @app.post("/order/share", response_class=HTMLResponse)
    async def share_submit(
        request: Request,
        user: User = Depends(get_current_user),
        session: Session = Depends(get_session),
    ):
        form = dict(await request.form())
        closes_at = None
        if form.get("when") == "chosen":
            try:
                closes_at = sharing.parse_closing_time(form.get("closes_at", ""))
            except sharing.ShareError as exc:
                return share_dialog(request, share_form_ctx(form, str(exc)), 422)
        order = order_for_write(session, user.id)
        if order.status != "open":
            # A stale form: an order already placed can't be shared again.
            return share_panel_response(request, session, user, None, {"oob": True})
        sharing.share_order(
            session,
            order,
            closes_at=closes_at,
            name=form.get("name"),
            location=form.get("location"),
            max_drinks=form.get("max_drinks", sharing.DEFAULT_MAX_DRINKS),
        )
        # Into the dialog's slot, which this empties; the sharing panel and
        # the advance orders (where planning now sits) swap in out of band.
        ctx = {**order_section_ctx(session, user, None), "oob": True}
        return templates.TemplateResponse(request, "_share_done.html", ctx)

    @app.post("/order/share/stop", response_class=HTMLResponse)
    def share_stop(
        request: Request,
        user: User = Depends(get_current_user),
        session: Session = Depends(get_session),
    ):
        """The × on the everyday order's sharing panel, once confirmed: stop
        sharing, taking out any drinks added by code. The whole section
        comes back, since the order list may have changed."""
        order = live_order(session, user.id, None)
        if order is not None and order.code is not None:
            joining.stop_sharing(session, order)
        return section(request, session, user, None)

    def shared_order_action(suffix: str, action):
        """A sharing control on one of the owner's shared, open orders; a
        no-op if the order isn't (a stale page), which re-renders the panel.
        action returns an error message to show, or None."""

        async def route(
            request: Request,
            order_id: Optional[int] = None,
            user: User = Depends(get_current_user),
            session: Session = Depends(get_session),
        ):
            order = live_order(session, user.id, order_id)
            extra = None
            if order is not None and order.code is not None and order.status == "open":
                form = dict(await request.form())
                error = action(session, order, form)
                if error:
                    extra = {"closing_error": error, "closing_value": form.get("closes_at", "")}
            return share_panel_response(
                request, session, user, order_id, extra, status=422 if extra else 200
            )

        route.__name__ = "order_" + suffix.strip("/").replace("/", "_")
        return both("post", suffix)(route)

    shared_order_action("/joins/extend", lambda s, o, f: sharing.extend_joins(s, o))
    shared_order_action("/joins/close", lambda s, o, f: sharing.close_joins(s, o))
    shared_order_action("/code/replace", lambda s, o, f: sharing.replace_code(s, o))
    shared_order_action("/cap", lambda s, o, f: sharing.set_max_drinks(s, o, f.get("max_drinks")))
    shared_order_action("/closing", lambda s, o, f: sharing.set_closing_time(s, o, f.get("closes_at", "")))

    @both("post", "/status")
    async def order_status(
        request: Request,
        order_id: Optional[int] = None,
        user: User = Depends(get_current_user),
        session: Session = Depends(get_session),
    ):
        """O5: send Ordered, On the way or Ready to everyone who joined.
        Returns the whole order section, because Ready completes the order
        and empties it (O7)."""
        form = dict(await request.form())
        order = live_order(session, user.id, order_id)
        status = form.get("status", "")
        # The order as it stands, kept on screen once Ready completes it.
        before = order_section_ctx(session, user, order_id) if order is not None else None
        sent = order is not None and order.code is not None and sharing.send_update(
            session, order, status, form.get("message")
        )
        completed = sent and status == "ready"
        if completed and order_id is not None:
            return templates.TemplateResponse(request, "_order_done.html", {})
        if completed and request.headers.get("HX-Target") == "share-panel":
            # The panel says the order is complete and the list stays as it
            # was, without its buttons, so nothing on the page moves.
            return templates.TemplateResponse(request, "_ready_done.html", {
                **before, "ready_now": True, "ready_message": (form.get("message") or "").strip(),
            })
        return section(request, session, user, order_id, {"completed": completed})
