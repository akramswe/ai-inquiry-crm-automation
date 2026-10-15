import hmac
import logging
from contextlib import asynccontextmanager
from pathlib import Path
import httpx
from fastapi import Depends, FastAPI, Form, HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import case, func, select
from sqlalchemy.orm import Session, selectinload

from .config import Settings, get_settings
from .database import Base, engine, get_db
from .models import AuditEvent, Draft, Inquiry, Lead, OutboxMessage
from .schemas import InquiryCreate, WorkflowResult
from .services import (
    approve_draft, create_draft, create_inquiry, prepare_followups,
    process_inquiry, reject_draft, update_lead_status, ALLOWED_LEAD_STATUSES,
)

logging.basicConfig(level=get_settings().log_level)
logger = logging.getLogger(__name__)
HERE = Path(__file__).resolve().parent
basic = HTTPBasic()
templates = Jinja2Templates(directory=str(HERE / "templates"))


@asynccontextmanager
async def lifespan(_app: FastAPI):
    Base.metadata.create_all(bind=engine)
    yield


app = FastAPI(title="AI Inquiry & CRM Automation", version="1.0.0", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(HERE / "static")), name="static")


def require_internal(request: Request, settings: Settings = Depends(get_settings)) -> None:
    supplied = request.headers.get("X-Internal-Token", "")
    if not hmac.compare_digest(supplied, settings.internal_api_token):
        raise HTTPException(status_code=401, detail="Invalid internal API token")


def require_admin(credentials: HTTPBasicCredentials = Depends(basic), settings: Settings = Depends(get_settings)) -> str:
    correct_user = hmac.compare_digest(credentials.username, settings.admin_username)
    correct_password = hmac.compare_digest(credentials.password, settings.admin_password)
    if not (correct_user and correct_password):
        raise HTTPException(status_code=401, detail="Incorrect credentials", headers={"WWW-Authenticate": "Basic"})
    return credentials.username


def guard_admin_post(request: Request, _user: str = Depends(require_admin)) -> None:
    # Basic CSRF defense for localhost demo. Use a full CSRF/session solution in production.
    origin = request.headers.get("origin")
    if origin and origin.rstrip("/") != str(request.base_url).rstrip("/"):
        raise HTTPException(status_code=403, detail="Cross-site admin submission blocked")
    fetch_site = request.headers.get("sec-fetch-site", "")
    if fetch_site and fetch_site not in {"same-origin", "none"}:
        raise HTTPException(status_code=403, detail="Cross-site admin submission blocked")


def get_inquiry_or_404(db: Session, inquiry_id: int) -> Inquiry:
    inquiry = db.get(Inquiry, inquiry_id)
    if inquiry is None:
        raise HTTPException(status_code=404, detail="Inquiry not found")
    return inquiry


@app.get("/api/health")
def health():
    return {"status": "ok", "service": "ai-inquiry-crm-automation"}


@app.post("/api/inquiries", status_code=status.HTTP_201_CREATED, dependencies=[Depends(require_internal)])
def intake(payload: InquiryCreate, db: Session = Depends(get_db)):
    try:
        inquiry = create_inquiry(db, payload)
    except ValueError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    return {"id": inquiry.id, "status": inquiry.status}


@app.post("/api/inquiries/{inquiry_id}/process", dependencies=[Depends(require_internal)])
def classify(inquiry_id: int, db: Session = Depends(get_db), settings: Settings = Depends(get_settings)):
    inquiry = get_inquiry_or_404(db, inquiry_id)
    try:
        inquiry = process_inquiry(db, inquiry, settings)
    except Exception as error:
        raise HTTPException(status_code=502, detail=f"AI classification failed: {type(error).__name__}") from error
    return {"id": inquiry.id, "status": inquiry.status, "intent": inquiry.intent, "priority": inquiry.priority, "lead_id": inquiry.lead_id}


@app.post("/api/inquiries/{inquiry_id}/draft", dependencies=[Depends(require_internal)], response_model=WorkflowResult)
def draft_initial(inquiry_id: int, db: Session = Depends(get_db)):
    inquiry = get_inquiry_or_404(db, inquiry_id)
    try:
        draft = create_draft(db, inquiry)
    except ValueError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    return WorkflowResult(inquiry_id=inquiry.id, draft_id=draft.id, status=draft.status)


@app.get("/api/inquiries/{inquiry_id}", dependencies=[Depends(require_internal)])
def get_inquiry(inquiry_id: int, db: Session = Depends(get_db)):
    inquiry = get_inquiry_or_404(db, inquiry_id)
    return {"id": inquiry.id, "status": inquiry.status, "intent": inquiry.intent,
            "priority": inquiry.priority, "summary": inquiry.summary, "lead_id": inquiry.lead_id}


@app.post("/api/followups/prepare", dependencies=[Depends(require_internal)])
def run_followups(db: Session = Depends(get_db)):
    return {"created_review_drafts": prepare_followups(db)}


@app.get("/", response_class=HTMLResponse)
def demo(request: Request):
    return templates.TemplateResponse(request, "demo.html", {"result": None, "error": None})


@app.post("/demo/submit", response_class=HTMLResponse)
def demo_submit(
    request: Request,
    name: str = Form(min_length=1, max_length=160),
    email: str = Form(),
    company: str = Form(default=""),
    subject: str = Form(default=""),
    message: str = Form(min_length=5),
    consent_to_follow_up: bool = Form(default=False),
):
    try:
        data = InquiryCreate(name=name, email=email, company=company, subject=subject,
                             message=message, consent_to_follow_up=consent_to_follow_up)
        # n8n is the orchestrator: browser -> backend form proxy -> n8n -> backend API.
        import os
        webhook_url = os.getenv("N8N_WEBHOOK_URL", "http://localhost:5678/webhook/customer-inquiry")
        response = httpx.post(webhook_url, json=data.model_dump(mode="json"), timeout=40)
        response.raise_for_status()
        result = response.json()
        if isinstance(result, list):
            result = result[0] if result else {}
        return templates.TemplateResponse(request, "demo.html", {"result": result, "error": None})
    except Exception as error:
        logger.warning("Demo form could not complete: %s", type(error).__name__)
        return templates.TemplateResponse(request, "demo.html", {
            "result": None, "error": "Workflow not available. Ensure n8n is running and the imported intake workflow is active."
        }, status_code=503)


@app.get("/admin", response_class=HTMLResponse)
def admin_dashboard(request: Request, _admin: str = Depends(require_admin), db: Session = Depends(get_db)):
    # Put urgent customer inquiries first; show oldest items first within a tier.
    pending = db.scalars(
        select(Draft).join(Draft.inquiry).options(selectinload(Draft.inquiry))
        .where(Draft.status == "pending")
        .order_by(
            case((Inquiry.priority == "high", 0), (Inquiry.priority == "medium", 1), else_=2),
            Draft.created_at.asc(), Draft.id.asc(),
        )
    ).all()
    inquiries = db.scalars(select(Inquiry).order_by(Inquiry.created_at.desc()).limit(30)).all()
    events = db.scalars(select(AuditEvent).order_by(AuditEvent.id.desc()).limit(20)).all()
    leads = db.scalars(select(Lead).order_by(Lead.id.desc()).limit(20)).all()
    sent = db.scalars(select(Draft).where(Draft.status == "sent").order_by(Draft.id.desc()).limit(10)).all()
    outbox_count = db.scalar(select(func.count(OutboxMessage.id))) or 0
    return templates.TemplateResponse(request, "admin.html", {
        "pending": pending, "inquiries": inquiries, "events": events,
        "leads": leads, "sent": sent, "outbox_count": outbox_count,
        "lead_statuses": sorted(ALLOWED_LEAD_STATUSES),
    })


@app.post("/admin/drafts/{draft_id}/approve", dependencies=[Depends(guard_admin_post)])
def approve(
    draft_id: int,
    subject: str = Form(),
    body: str = Form(),
    reviewer: str = Form(),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    draft = db.scalar(select(Draft).options(selectinload(Draft.inquiry)).where(Draft.id == draft_id))
    if not draft:
        raise HTTPException(status_code=404, detail="Draft not found")
    try:
        approve_draft(db, draft, subject, body, reviewer, settings)
    except ValueError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    except Exception as error:
        db.rollback()
        logger.exception("Mail delivery failed for draft %s", draft_id)
        raise HTTPException(status_code=502, detail=f"Delivery failed: {type(error).__name__}") from error
    return RedirectResponse(url="/admin", status_code=303)


@app.post("/admin/drafts/{draft_id}/reject", dependencies=[Depends(guard_admin_post)])
def reject(draft_id: int, reviewer: str = Form(default="Reviewer"), db: Session = Depends(get_db)):
    draft = db.scalar(select(Draft).options(selectinload(Draft.inquiry)).where(Draft.id == draft_id))
    if not draft:
        raise HTTPException(status_code=404, detail="Draft not found")
    try:
        reject_draft(db, draft, reviewer)
    except ValueError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    return RedirectResponse(url="/admin", status_code=303)


@app.post("/admin/leads/{lead_id}/status", dependencies=[Depends(guard_admin_post)])
def change_lead_status(
    lead_id: int,
    status_value: str = Form(alias="status"),
    reviewer: str = Form(),
    db: Session = Depends(get_db),
):
    lead = db.get(Lead, lead_id)
    if lead is None:
        raise HTTPException(status_code=404, detail="Lead not found")
    try:
        update_lead_status(db, lead, status_value, reviewer)
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    return RedirectResponse(url="/admin", status_code=303)


@app.post("/admin/followups/prepare", dependencies=[Depends(guard_admin_post)])
def prepare_followups_from_ui(db: Session = Depends(get_db)):
    prepare_followups(db)
    return RedirectResponse(url="/admin", status_code=303)


@app.get("/admin/outbox", response_class=HTMLResponse)
def outbox(request: Request, _admin: str = Depends(require_admin), db: Session = Depends(get_db)):
    messages = db.scalars(select(OutboxMessage).order_by(OutboxMessage.id.desc())).all()
    return templates.TemplateResponse(request, "outbox.html", {"messages": messages})
