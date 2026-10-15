from sqlalchemy import select
from app.models import AuditEvent, Draft, Inquiry, Lead, OutboxMessage


def create_draft(client, api_headers, sample):
    created = client.post("/api/inquiries", json=sample, headers=api_headers)
    assert created.status_code == 201, created.text
    inquiry_id = created.json()["id"]
    classified = client.post(f"/api/inquiries/{inquiry_id}/process", headers=api_headers)
    assert classified.status_code == 200, classified.text
    assert classified.json()["lead_id"] is not None
    drafted = client.post(f"/api/inquiries/{inquiry_id}/draft", headers=api_headers)
    assert drafted.status_code == 200, drafted.text
    return inquiry_id, drafted.json()["draft_id"]


def test_health(client):
    assert client.get("/api/health").json()["status"] == "ok"


def test_internal_api_requires_token(client, sample):
    assert client.post("/api/inquiries", json=sample).status_code == 401
    assert client.post("/api/inquiries", json=sample, headers={"X-Internal-Token": "wrong"}).status_code == 401


def test_validation_rejects_bad_emails(client, api_headers, sample):
    assert client.post("/api/inquiries", json={**sample, "email": "invalid"}, headers=api_headers).status_code == 422
    assert client.post("/api/inquiries", json={**sample, "message": "   "}, headers=api_headers).status_code == 422


def test_full_workflow_classifies_creates_crm_and_draft(client, api_headers, sample, admin_auth):
    inquiry_id, draft_id = create_draft(client, api_headers, sample)
    inquiry = client.get(f"/api/inquiries/{inquiry_id}", headers=api_headers).json()
    assert inquiry["status"] == "pending_review"
    assert inquiry["intent"] == "pricing"
    assert inquiry["summary"].startswith("Please")
    dashboard = client.get("/admin", auth=admin_auth)
    assert dashboard.status_code == 200
    assert "jamie@example.com" in dashboard.text
    assert "Approve &amp; send" in dashboard.text
    with client.app.state.test_session_factory() as db:
        assert len(db.scalars(select(Lead)).all()) == 1
        assert db.get(Draft, draft_id).status == "pending"
        assert db.scalars(select(OutboxMessage)).all() == []


def test_draft_creation_and_classification_idempotent(client, api_headers, sample):
    inquiry_id, draft_id = create_draft(client, api_headers, sample)
    assert client.post(f"/api/inquiries/{inquiry_id}/process", headers=api_headers).status_code == 200
    draft_second = client.post(f"/api/inquiries/{inquiry_id}/draft", headers=api_headers)
    assert draft_second.json()["draft_id"] == draft_id


def test_inquiry_idempotency_key_reuses_same_record(client, api_headers, sample):
    data = {**sample, "idempotency_key": "unique-event-123"}
    one = client.post("/api/inquiries", json=data, headers=api_headers)
    two = client.post("/api/inquiries", json=data, headers=api_headers)
    assert one.json()["id"] == two.json()["id"]
    bad = client.post("/api/inquiries", json={**data, "message": "Different message"}, headers=api_headers)
    assert bad.status_code == 409


def test_missing_classification_prevents_draft(client, api_headers, sample):
    response = client.post("/api/inquiries", json=sample, headers=api_headers)
    assert client.post(f"/api/inquiries/{response.json()['id']}/draft", headers=api_headers).status_code == 409


def test_human_approval_writes_one_outbox_message(client, api_headers, sample, admin_auth):
    inquiry_id, draft_id = create_draft(client, api_headers, sample)
    assert client.get("/admin").status_code == 401
    response = client.post(f"/admin/drafts/{draft_id}/approve", data={
        "subject": "Your proposal", "body": "Hi Jamie, we'd be happy to talk.", "reviewer": "Akram"
    }, auth=admin_auth, follow_redirects=False)
    assert response.status_code == 303
    with client.app.state.test_session_factory() as db:
        messages = db.scalars(select(OutboxMessage)).all()
        assert len(messages) == 1
        assert messages[0].subject == "Your proposal"
        assert db.get(Inquiry, inquiry_id).status == "sent"
        assert db.get(Draft, draft_id).reviewer == "Akram"
        assert db.scalars(select(Lead)).first().status == "contacted"
    second = client.post(f"/admin/drafts/{draft_id}/approve", data={
        "subject": "Double send", "body": "should not happen", "reviewer": "Akram"
    }, auth=admin_auth)
    assert second.status_code == 409
    assert "Your proposal" in client.get("/admin/outbox", auth=admin_auth).text


def test_followup_requires_consent_and_second_human_review(client, api_headers, sample, admin_auth):
    inquiry_id, draft_id = create_draft(client, api_headers, sample)
    client.post(f"/admin/drafts/{draft_id}/approve", data={
        "subject": "Your inquiry", "body": "Thanks for reaching out", "reviewer": "Akram"
    }, auth=admin_auth)
    result = client.post("/api/followups/prepare", headers=api_headers)
    assert result.json() == {"created_review_drafts": 1}
    assert client.post("/api/followups/prepare", headers=api_headers).json() == {"created_review_drafts": 0}
    with client.app.state.test_session_factory() as db:
        followup = db.scalar(select(Draft).where(Draft.inquiry_id == inquiry_id, Draft.kind == "followup"))
        assert followup.status == "pending"
        assert len(db.scalars(select(OutboxMessage)).all()) == 1
        followup_id = followup.id
    client.post(f"/admin/drafts/{followup_id}/approve", data={
        "subject": "Checking in", "body": "Would you like to continue?", "reviewer": "Akram"
    }, auth=admin_auth)
    with client.app.state.test_session_factory() as db:
        assert db.get(Inquiry, inquiry_id).status == "followup_sent"
        assert len(db.scalars(select(OutboxMessage)).all()) == 2


def test_no_followup_without_consent(client, api_headers, sample, admin_auth):
    _, draft_id = create_draft(client, api_headers, {**sample, "consent_to_follow_up": False})
    client.post(f"/admin/drafts/{draft_id}/approve", data={
        "subject": "Thanks", "body": "Hello", "reviewer": "Akram"
    }, auth=admin_auth)
    assert client.post("/api/followups/prepare", headers=api_headers).json()["created_review_drafts"] == 0


def test_rejection_never_sends(client, api_headers, sample, admin_auth):
    inquiry_id, draft_id = create_draft(client, api_headers, sample)
    response = client.post(f"/admin/drafts/{draft_id}/reject", auth=admin_auth)
    assert response.status_code == 200
    with client.app.state.test_session_factory() as db:
        assert db.get(Draft, draft_id).status == "rejected"
        assert db.get(Inquiry, inquiry_id).status == "closed"
        assert not db.scalars(select(OutboxMessage)).all()


def test_dashboard_escapes_untrusted_html(client, api_headers, sample, admin_auth):
    create_draft(client, api_headers, {**sample, "message": "Please quote <script>alert(1)</script> for my company"})
    html = client.get("/admin", auth=admin_auth).text
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html
    assert "<script>alert(1)</script>" not in html


def test_no_cross_site_admin_post(client, api_headers, sample, admin_auth):
    _, draft_id = create_draft(client, api_headers, sample)
    response = client.post(f"/admin/drafts/{draft_id}/approve", data={
        "subject": "Sample", "body": "Hello", "reviewer": "Akram"
    }, headers={"Origin": "https://malicious.example"}, auth=admin_auth)
    assert response.status_code == 403


def test_audit_events_created(client, api_headers, sample):
    create_draft(client, api_headers, sample)
    with client.app.state.test_session_factory() as db:
        events = db.scalars(select(AuditEvent)).all()
        assert [x.action for x in events] == ["inquiry.received", "inquiry.classified", "draft.created"]


def test_multiple_inquiries_upsert_one_crm_lead(client, api_headers, sample):
    create_draft(client, api_headers, sample)
    create_draft(client, api_headers, {**sample, "subject": "Need urgent support", "message": "Our appointment app is broken, urgent help!"})
    with client.app.state.test_session_factory() as db:
        leads = db.scalars(select(Lead)).all()
        assert len(leads) == 1
        assert leads[0].interest == "support"
        assert db.scalars(select(Inquiry)).all()[1].priority == "high"


def test_approval_invalid_inputs_cannot_send(client, api_headers, sample, admin_auth):
    _, draft_id = create_draft(client, api_headers, sample)
    response = client.post(f"/admin/drafts/{draft_id}/approve", data={
        "subject": "   ", "body": "Text", "reviewer": "Akram"
    }, auth=admin_auth)
    assert response.status_code == 409
    with client.app.state.test_session_factory() as db:
        assert db.get(Draft, draft_id).status == "pending"
        assert not db.scalars(select(OutboxMessage)).all()


def test_invalid_ai_configuration_surfaces_failure(client, api_headers, sample):
    from app.config import Settings, get_settings
    from app.main import app
    app.dependency_overrides[get_settings] = lambda: Settings(ai_mode="openai", openai_api_key="", _env_file=None)
    created = client.post("/api/inquiries", json=sample, headers={"X-Internal-Token": "local-dev-token-change-me"})
    inquiry_id = created.json()["id"]
    failed = client.post(f"/api/inquiries/{inquiry_id}/process", headers={"X-Internal-Token": "local-dev-token-change-me"})
    assert failed.status_code == 502
    with client.app.state.test_session_factory() as db:
        assert db.get(Inquiry, inquiry_id).status == "error"
        assert db.get(Inquiry, inquiry_id).last_error == "ValueError"
