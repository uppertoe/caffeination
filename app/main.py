from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, Form, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlmodel import Session
from starlette.middleware.base import BaseHTTPMiddleware

from app.config import get_settings
from app.db import get_session, init_db
from app.identity import apply_fresh_identity
from app.models import User
from app.users import get_current_user

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


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(title=settings.app_name, lifespan=lifespan)
    app.add_middleware(IdentityMiddleware)
    app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")

    @app.get("/healthz", include_in_schema=False)
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/", response_class=HTMLResponse)
    def index(request: Request, user: User = Depends(get_current_user)):
        return templates.TemplateResponse(
            request,
            "index.html",
            {"app_name": settings.app_name, "user": user},
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
                "_name_form.html",
                {"error": "Name must be 1–40 characters.", "submitted": name},
                status_code=422,
            )
        user.display_name = name
        session.add(user)
        session.commit()
        session.refresh(user)
        return templates.TemplateResponse(
            request, "_dashboard.html", {"user": user}
        )

    return app


app = create_app()
