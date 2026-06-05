from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlmodel import Session
from starlette.middleware.base import BaseHTTPMiddleware

from app.config import get_settings
from app.db import get_session, init_db
from app.drinks import (
    format_drink,
    get_saved_drink,
    normalize,
    upsert_saved_drink,
)
from app.identity import apply_fresh_identity
from app.menu import (
    DRINKS,
    MILK_LABELS,
    STRENGTH_LABELS,
    SWEETENER_LABELS,
    get_drink,
    rules_for_template,
)
from app.models import SavedDrink, User
from app.orders import (
    add_to_order,
    order_rows,
    remove_from_order,
    roster_candidates,
    till_summary,
)
from app.users import (
    can_edit_person,
    claim_user,
    create_named_user,
    existing_names_lower,
    find_user_by_display_name,
    get_current_user,
    named_users_with_lines,
)

BASE_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    yield


class IdentityMiddleware(BaseHTTPMiddleware):
    """Write the Set-Cookie header if `get_current_user` minted a fresh id."""

    async def dispatch(self, request, call_next):
        response = await call_next(request)
        apply_fresh_identity(request, response)
        return response


# ---------------------------------------------------------------------------
# Template context builders
# ---------------------------------------------------------------------------


def _onboard_ctx(session: Session) -> dict:
    return {
        "app_name": get_settings().app_name,
        "named_users": named_users_with_lines(session),
    }


def _drink_card_ctx(session: Session, user: User) -> dict:
    saved = get_saved_drink(session, user.id)
    till_line = None
    if saved is not None:
        drink = get_drink(saved.base_id)
        till_line = format_drink(drink, saved) if drink else saved.base_id
    return {"saved": saved, "till_line": till_line}


def _drink_form_ctx(saved: SavedDrink | None) -> dict:
    initial = {
        "base_id": saved.base_id if saved else DRINKS[0].id,
        "temp": saved.temp if saved else "hot",
        "size": saved.size if saved and saved.size else "small",
        "milk": saved.milk if saved and saved.milk else "full_cream",
        "strength": saved.strength if saved else "regular",
        "sweetener": saved.sweetener if saved else "none",
        "length": saved.length if saved and saved.length else "short",
        "notes": saved.notes if saved else "",
    }
    return {
        "rules": rules_for_template(),
        "initial": initial,
        "drinks": DRINKS,
        "milk_options": list(MILK_LABELS.items()),
        "strength_options": list(STRENGTH_LABELS.items()),
        "sweetener_options": list(SWEETENER_LABELS.items()),
    }


def _order_section_ctx(session: Session, user: User, *, oob: bool = False) -> dict:
    rows = order_rows(session, user.id)
    return {
        "rows": rows,
        "roster": roster_candidates(session, user.id),
        "till_lines": till_summary(rows),
        "oob": oob,
    }


def _new_person_ctx(session: Session) -> dict:
    """Context for the 'order for someone else' slot: a blank drink-builder
    plus the set of names already taken (for the client-side dup check)."""
    return {
        **_drink_form_ctx(None),
        "existing_names": existing_names_lower(session),
    }


def _dashboard_ctx(session: Session, user: User) -> dict:
    return {
        "user": user,
        **_drink_card_ctx(session, user),
        **_order_section_ctx(session, user),
        **_new_person_ctx(session),
    }


def _render(name: str, ctx: dict) -> str:
    return templates.get_template(name).render(ctx)


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(title=settings.app_name, lifespan=lifespan)
    app.add_middleware(IdentityMiddleware)
    app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")

    @app.get("/healthz", include_in_schema=False)
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/site.webmanifest", include_in_schema=False)
    def site_webmanifest() -> JSONResponse:
        """PWA manifest. Served from a route (not /static) so the install name
        tracks the per-deployment APP_NAME. Icons/colours stay shared."""
        return JSONResponse(
            {
                "name": settings.app_name,
                "short_name": settings.app_name,
                "description": "Sort the office coffee run.",
                "start_url": "/",
                "display": "standalone",
                "background_color": "#231c07",
                "theme_color": "#231c07",
                "icons": [
                    {"src": "/static/icon-192.png", "sizes": "192x192", "type": "image/png", "purpose": "any maskable"},
                    {"src": "/static/icon-512.png", "sizes": "512x512", "type": "image/png", "purpose": "any maskable"},
                    {"src": "/static/favicon.svg", "type": "image/svg+xml", "sizes": "any"},
                ],
            },
            media_type="application/manifest+json",
        )

    @app.get("/", response_class=HTMLResponse)
    def index(
        request: Request,
        user: User = Depends(get_current_user),
        session: Session = Depends(get_session),
    ):
        ctx: dict = {
            "app_name": settings.app_name,
            "tagline": settings.tagline,
            "user": user,
        }
        if user.display_name:
            ctx.update(_dashboard_ctx(session, user))
        else:
            ctx.update(_onboard_ctx(session))
        return templates.TemplateResponse(request, "index.html", ctx)

    @app.post("/logout", response_class=HTMLResponse)
    def logout(
        request: Request,
        user: User = Depends(get_current_user),
        session: Session = Depends(get_session),
    ):
        """Unbind the identity cookie and drop back to onboarding. The user
        row (and their saved drink) stays put so they can claim it again."""
        response = templates.TemplateResponse(
            request, "_onboard.html", _onboard_ctx(session)
        )
        response.delete_cookie(settings.cookie_name, path="/")
        return response

    @app.post("/onboard/claim/{user_id}", response_class=HTMLResponse)
    def onboard_claim(
        user_id: str,
        request: Request,
        user: User = Depends(get_current_user),
        session: Session = Depends(get_session),
    ):
        claimed = claim_user(session, request, user, user_id)
        if claimed is None:
            return templates.TemplateResponse(
                request,
                "_onboard.html",
                {**_onboard_ctx(session), "error": "That user doesn't exist."},
                status_code=404,
            )
        return templates.TemplateResponse(
            request, "_dashboard.html", _dashboard_ctx(session, claimed)
        )

    @app.post("/me/name", response_class=HTMLResponse)
    def set_name(
        request: Request,
        display_name: str = Form(...),
        user: User = Depends(get_current_user),
        session: Session = Depends(get_session),
    ):
        name = display_name.strip()
        if not (1 <= len(name) <= 40):
            return templates.TemplateResponse(
                request,
                "_onboard.html",
                {
                    **_onboard_ctx(session),
                    "error": "Name must be 1–40 characters.",
                    "submitted": name,
                },
                status_code=422,
            )
        existing = find_user_by_display_name(session, name)
        if existing is not None and existing.id != user.id:
            return templates.TemplateResponse(
                request,
                "_onboard.html",
                {
                    **_onboard_ctx(session),
                    "error": f"That name's already taken. Pick '{existing.display_name}' from the list to claim it.",
                    "submitted": name,
                },
                status_code=409,
            )
        user.display_name = name
        session.add(user)
        session.commit()
        session.refresh(user)
        return templates.TemplateResponse(
            request, "_dashboard.html", _dashboard_ctx(session, user)
        )

    @app.get("/me/drink", response_class=HTMLResponse)
    def drink_card(
        request: Request,
        user: User = Depends(get_current_user),
        session: Session = Depends(get_session),
    ):
        return templates.TemplateResponse(
            request, "_drink_card.html", _drink_card_ctx(session, user)
        )

    @app.get("/me/drink/cancel", response_class=HTMLResponse)
    def drink_card_cancel(
        request: Request,
        user: User = Depends(get_current_user),
        session: Session = Depends(get_session),
    ):
        return templates.TemplateResponse(
            request, "_drink_card.html", _drink_card_ctx(session, user)
        )

    @app.get("/me/drink/edit", response_class=HTMLResponse)
    def drink_form(
        request: Request,
        user: User = Depends(get_current_user),
        session: Session = Depends(get_session),
    ):
        saved = get_saved_drink(session, user.id)
        return templates.TemplateResponse(
            request, "_drink_form.html", _drink_form_ctx(saved)
        )

    @app.post("/me/drink", response_class=HTMLResponse)
    async def save_drink(
        request: Request,
        user: User = Depends(get_current_user),
        session: Session = Depends(get_session),
    ):
        form = await request.form()
        base_id = form.get("base_id", "")
        normalized = normalize(base_id, dict(form))
        if normalized is None:
            saved = get_saved_drink(session, user.id)
            return templates.TemplateResponse(
                request,
                "_drink_form.html",
                {**_drink_form_ctx(saved), "error": "Unknown drink."},
                status_code=422,
            )
        upsert_saved_drink(session, user.id, normalized)
        card_html = _render("_drink_card.html", _drink_card_ctx(session, user))
        order_html = _render(
            "_order_section.html", _order_section_ctx(session, user, oob=True)
        )
        return HTMLResponse(card_html + order_html)

    @app.post("/people", response_class=HTMLResponse)
    async def create_person(
        request: Request,
        user: User = Depends(get_current_user),
        session: Session = Depends(get_session),
    ):
        """Create a person + their usual on someone else's behalf, then add
        them to the current user's order. Roster people need a globally unique
        name; one-off ("guest") entries are ephemeral and may share names."""
        form = await request.form()
        name = (form.get("display_name") or "").strip()
        one_off = bool(form.get("one_off"))

        def _err(message: str, status: int):
            ctx = {
                **_new_person_ctx(session),
                "error": message,
                "open": True,
                "submitted_name": name,
                "submitted_one_off": one_off,
            }
            return templates.TemplateResponse(
                request, "_new_person_slot.html", ctx, status_code=status
            )

        if not (1 <= len(name) <= 40):
            return _err("Name must be 1–40 characters.", 422)
        if not one_off and find_user_by_display_name(session, name) is not None:
            return _err(f"“{name}” is already on the list — pick another name.", 409)

        normalized = normalize(form.get("base_id", ""), dict(form))
        if normalized is None:
            return _err("Unknown drink.", 422)

        new_user = create_named_user(
            session, name, created_by=user.id, one_off=one_off
        )
        upsert_saved_drink(session, new_user.id, normalized)
        add_to_order(session, user.id, new_user.id)

        slot_html = _render("_new_person_slot.html", _new_person_ctx(session))
        order_html = _render(
            "_order_section.html", _order_section_ctx(session, user, oob=True)
        )
        return HTMLResponse(slot_html + order_html)

    @app.get("/order", response_class=HTMLResponse)
    def order_section(
        request: Request,
        user: User = Depends(get_current_user),
        session: Session = Depends(get_session),
    ):
        """Re-render the order section in-band (used to cancel an inline edit)."""
        return templates.TemplateResponse(
            request, "_order_section.html", _order_section_ctx(session, user)
        )

    @app.get("/people/{person_id}/edit", response_class=HTMLResponse)
    def edit_person_form(
        person_id: str,
        request: Request,
        user: User = Depends(get_current_user),
        session: Session = Depends(get_session),
    ):
        target = session.get(User, person_id)
        if not can_edit_person(user.id, target):
            return templates.TemplateResponse(
                request, "_order_section.html", _order_section_ctx(session, user)
            )
        saved = get_saved_drink(session, person_id)
        return templates.TemplateResponse(
            request, "_person_edit.html", {**_drink_form_ctx(saved), "person": target}
        )

    @app.post("/people/{person_id}/drink", response_class=HTMLResponse)
    async def edit_person_drink(
        person_id: str,
        request: Request,
        user: User = Depends(get_current_user),
        session: Session = Depends(get_session),
    ):
        target = session.get(User, person_id)
        if not can_edit_person(user.id, target):
            return templates.TemplateResponse(
                request,
                "_order_section.html",
                _order_section_ctx(session, user),
                status_code=403,
            )
        form = await request.form()
        normalized = normalize(form.get("base_id", ""), dict(form))
        if normalized is None:
            saved = get_saved_drink(session, person_id)
            return templates.TemplateResponse(
                request,
                "_person_edit.html",
                {**_drink_form_ctx(saved), "person": target, "error": "Unknown drink."},
                status_code=422,
            )
        upsert_saved_drink(session, person_id, normalized)
        return templates.TemplateResponse(
            request, "_order_section.html", _order_section_ctx(session, user)
        )

    @app.post("/order/add/{target_id}", response_class=HTMLResponse)
    def order_add(
        target_id: str,
        request: Request,
        user: User = Depends(get_current_user),
        session: Session = Depends(get_session),
    ):
        add_to_order(session, user.id, target_id)
        return templates.TemplateResponse(
            request, "_order_section.html", _order_section_ctx(session, user)
        )

    @app.post("/order/remove/{target_id}", response_class=HTMLResponse)
    def order_remove(
        target_id: str,
        request: Request,
        user: User = Depends(get_current_user),
        session: Session = Depends(get_session),
    ):
        remove_from_order(session, user.id, target_id)
        return templates.TemplateResponse(
            request, "_order_section.html", _order_section_ctx(session, user)
        )

    return app


app = create_app()
