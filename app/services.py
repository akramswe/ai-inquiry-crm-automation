import json
import logging
import smtplib
from datetime import timedelta
from email.message import EmailMessage
from sqlalchemy import select
from sqlalchemy.orm import Session
from .ai import extract
from .config import Settings
from .models import AuditEvent, Draft, Inquiry, Lead, OutboxMessage, utc_now
from .schemas import InquiryCreate

log = logging.getLogger(__name__)


def audit(db: Session, inquiry_id: int | None, action: str, details: str = "") -> None:
    db.add(AuditEvent(inquiry_id=inquiry_id, action=action, details=details[:1000]))


def create_inquiry(db: Session, data: InquiryCreate) -> Inquiry:
    if data.idempotency_key:
        existing = db.scalar(select(Inquiry).where(Inquiry.idempotency_key == data.idempotency_key))
        if existing:
            # The same event ID must refer to the same *whole* request, not just
            # the email and message. Consent and source affect business behavior.
            incoming = data.model_dump(mode="json")
            fields = (
                "name", "email", "company", "subject", "message",
                "source", "consent_to_follow_up",
            )
            if any(getattr(existing, field) != incoming[field] for field in fields):
                raise ValueError("Idempotency key already used for a different inquiry")
            return existing
    inquiry = Inquiry(**data.model_dump(mode="json"))
    db.add(inquiry)
    db.flush()
    audit(db, inquiry.id, "inquiry.received", f"source={inquiry.source}")
    db.commit()
    db.refresh(inquiry)
    return inquiry


def process_inquiry(db: Session, inquiry: Inquiry, settings: Settings) -> Inquiry:
    if inquiry.status in {"classified", "pending_review", "sent", "followup_review", "followup_sent", "closed"}:
        return inquiry
    try:
        result = extract(inquiry, settings)
        lead = db.scalar(select(Lead).where(Lead.email == inquiry.email))
        if lead is None:
            lead = Lead(email=inquiry.email, name=inquiry.name, company=inquiry.company)
            db.add(lead)
            db.flush()
        lead.interest = result.intent
        if inquiry.company:
            lead.company = inquiry.company
        inquiry.lead_id = lead.id
        inquiry.intent = result.intent
        inquiry.priority = result.priority
        inquiry.summary = result.summary
        inquiry.requirements_json = json.dumps(result.requirements)
        inquiry.last_error = ""
        inquiry.status = "classified"
        audit(db, inquiry.id, "inquiry.classified", f"intent={result.intent}; priority={result.priority}")
        db.commit()
    except Exception as error:
        db.rollback()
        fresh = db.get(Inquiry, inquiry.id)
        fresh.status = "error"
        # Never store sensitive API response bodies in errors.
        fresh.last_error = type(error).__name__
        audit(db, fresh.id, "inquiry.error", type(error).__name__)
        db.commit()
        log.exception("Failed to process inquiry %s", inquiry.id)
        raise
    return db.get(Inquiry, inquiry.id)


def compose_draft(inquiry: Inquiry, kind: str) -> tuple[str, str]:
    """Create a cautious, intent-aware starting point for human editing.

    The message is a template, *not* an autonomous answer to the customer.
    No price, availability, SLA, or service commitment is invented.
    """
    first_name = inquiry.name.strip().split()[0]
    if kind == "followup":
        return (
            f"Following up: {inquiry.subject or 'your inquiry'}",
            f"Hi {first_name},\n\nI'm checking whether you still need help with your request. "
            "If you'd like to continue, just reply to this email.\n\n"
            "Kind regards,\nCustomer Success Team",
        )

    responses = {
        "pricing": (
            "Your pricing request",
            "Thanks for asking about pricing. We'll review the scope of your request "
            "before recommending an approach. If we need more details, we'll ask first.",
        ),
        "appointment": (
            "Your appointment request",
            "Thanks for getting in touch about scheduling. We'll review your request "
            "and follow up to agree on a suitable time. No appointment is confirmed yet.",
        ),
        "support": (
            "Your support request",
            "Thanks for reporting this issue. We'll review the information you shared. "
            "If anything is missing, we'll ask for the details needed to investigate.",
        ),
        "general": (
            "Thanks for contacting us",
            "Thanks for reaching out. We've received your message and will review "
            "it before responding with the appropriate next steps.",
        ),
    }
    subject, message = responses.get(inquiry.intent, responses["general"])
    body = f"Hi {first_name},\n\n{message}\n\nKind regards,\nCustomer Success Team"
    return subject, body


def create_draft(db: Session, inquiry: Inquiry, kind: str = "initial") -> Draft:
    existing = db.scalar(select(Draft).where(Draft.inquiry_id == inquiry.id, Draft.kind == kind))
    if existing:
        return existing
    if kind == "initial" and inquiry.status != "classified":
        raise ValueError("Inquiry must be classified before drafting")
    if kind == "followup" and (inquiry.status != "sent" or not inquiry.consent_to_follow_up):
        raise ValueError("Follow-up requires a sent initial reply and customer consent")
    subject, body = compose_draft(inquiry, kind)
    draft = Draft(inquiry_id=inquiry.id, kind=kind, subject=subject, body=body)
    db.add(draft)
    inquiry.status = "pending_review" if kind == "initial" else "followup_review"
    if kind == "followup":
        inquiry.followup_due_at = None
    db.flush()
    audit(db, inquiry.id, "draft.created", f"kind={kind}; draft={draft.id}")
    db.commit()
    db.refresh(draft)
    return draft


def prepare_followups(db: Session) -> int:
    due = db.scalars(
        select(Inquiry).where(
            Inquiry.status == "sent",
            Inquiry.consent_to_follow_up.is_(True),
            Inquiry.followup_due_at.is_not(None),
            Inquiry.followup_due_at <= utc_now(),
        )
    ).all()
    count = 0
    for inquiry in due:
        if db.scalar(select(Draft.id).where(Draft.inquiry_id == inquiry.id, Draft.kind == "followup")) is None:
            create_draft(db, inquiry, "followup")
            count += 1
    return count


def deliver(db: Session, draft: Draft, subject: str, body: str, settings: Settings) -> None:
    """An empty SMTP_HOST stores a message in an inspectable local outbox."""
    if not settings.smtp_host:
        db.add(OutboxMessage(recipient=draft.inquiry.email, subject=subject, body=body))
        return
    message = EmailMessage()
    message["From"] = settings.mail_from
    message["To"] = draft.inquiry.email
    message["Subject"] = subject
    message.set_content(body)
    with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=10) as smtp:
        if settings.smtp_starttls:
            smtp.starttls()
        if settings.smtp_username:
            smtp.login(settings.smtp_username, settings.smtp_password)
        smtp.send_message(message)


def approve_draft(db: Session, draft: Draft, subject: str, body: str, reviewer: str, settings: Settings) -> None:
    if draft.status != "pending":
        raise ValueError("Only pending drafts can be approved")
    subject, body, reviewer = subject.strip(), body.strip(), reviewer.strip()
    if not subject or len(subject) > 200 or not body or len(body) > 20000 or not reviewer or len(reviewer) > 120:
        raise ValueError("Subject, body, and reviewer are required and must fit limits")
    inquiry = draft.inquiry
    # Approval is explicit. Email never sends during AI processing or drafting.
    deliver(db, draft, subject, body, settings)
    draft.subject = subject
    draft.body = body
    draft.reviewer = reviewer
    draft.status = "sent"
    draft.sent_at = utc_now()
    if draft.kind == "initial":
        inquiry.status = "sent"
        if inquiry.consent_to_follow_up:
            inquiry.followup_due_at = utc_now() + timedelta(hours=settings.followup_delay_hours)
    else:
        inquiry.status = "followup_sent"
    if inquiry.lead_id:
        lead = db.get(Lead, inquiry.lead_id)
        if lead and lead.status == "new":
            # Preserve an explicit qualification/archival decision made by a reviewer.
            lead.status = "contacted"
    audit(db, inquiry.id, "draft.sent", f"kind={draft.kind}; reviewer={reviewer}")
    db.commit()


def reject_draft(db: Session, draft: Draft, reviewer: str) -> None:
    if draft.status != "pending":
        raise ValueError("Only pending drafts can be rejected")
    draft.status = "rejected"
    draft.reviewer = reviewer[:120]
    draft.inquiry.status = "closed" if draft.kind == "initial" else "sent"
    audit(db, draft.inquiry_id, "draft.rejected", f"kind={draft.kind}")
    db.commit()


ALLOWED_LEAD_STATUSES = frozenset({"new", "qualified", "contacted", "archived"})


def update_lead_status(db: Session, lead: Lead, new_status: str, reviewer: str) -> None:
    """A reviewer can triage a CRM lead without changing inquiry state."""
    if new_status not in ALLOWED_LEAD_STATUSES:
        raise ValueError("Unsupported lead status")
    reviewer = reviewer.strip()
    if not reviewer or len(reviewer) > 120:
        raise ValueError("Reviewer name required (max 120 characters)")
    if lead.status == new_status:
        return
    old_status = lead.status
    lead.status = new_status
    latest_inquiry = db.scalar(
        select(Inquiry).where(Inquiry.lead_id == lead.id).order_by(Inquiry.id.desc()).limit(1)
    )
    audit(
        db, latest_inquiry.id if latest_inquiry else None, "lead.status_changed",
        f"lead={lead.id}; {old_status}->{new_status}; reviewer={reviewer}",
    )
    db.commit()
