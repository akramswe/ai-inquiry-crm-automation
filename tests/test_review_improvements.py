"""Regression tests for the reviewed CRM and triage improvements."""
from sqlalchemy import select
from app.models import AuditEvent, Draft, Inquiry, Lead


def process_and_draft(client, headers, data):
    resp = client.post('/api/inquiries', json=data, headers=headers)
    assert resp.status_code == 201, resp.text
    inquiry_id = resp.json()['id']
    assert client.post(f'/api/inquiries/{inquiry_id}/process', headers=headers).status_code == 200
    resp = client.post(f'/api/inquiries/{inquiry_id}/draft', headers=headers)
    assert resp.status_code == 200, resp.text
    return inquiry_id, resp.json()['draft_id']


def test_idempotency_rejects_consent_or_subject_change(client, api_headers, sample):
    payload = {**sample, 'idempotency_key': 'review-event-001'}
    first = client.post('/api/inquiries', json=payload, headers=api_headers)
    assert first.status_code == 201
    for changed in (
        {**payload, 'consent_to_follow_up': False},
        {**payload, 'subject': 'Different subject'},
        {**payload, 'source': 'email'},
        {**payload, 'company': 'Another company'},
    ):
        resp = client.post('/api/inquiries', json=changed, headers=api_headers)
        assert resp.status_code == 409
    repeated = client.post('/api/inquiries', json=payload, headers=api_headers)
    assert repeated.status_code == 201
    assert repeated.json()['id'] == first.json()['id']


def test_keyword_boundaries_and_german_terms(client, api_headers, sample):
    cases = [
        ('I was surprised by how simple the setup was', 'general', 'medium'),
        ('Bitte senden Sie ein Angebot für die Integration', 'pricing', 'medium'),
        ('Dringend: Der Support hat ein Problem mit den Kosten', 'support', 'high'),
        ('We would like to book a demo', 'appointment', 'medium'),
    ]
    for index, (message, intent, priority) in enumerate(cases):
        resp = client.post('/api/inquiries', json={**sample, 'subject': f'Request {index}', 'message': message}, headers=api_headers)
        inquiry_id = resp.json()['id']
        classified = client.post(f'/api/inquiries/{inquiry_id}/process', headers=api_headers)
        assert classified.json()['intent'] == intent
        assert classified.json()['priority'] == priority


def test_mock_extracts_customer_bullet_points(client, api_headers, sample):
    message = 'We need help with:\n- Connect our website\n- Answer common questions\n3. Collect leads'
    resp = client.post('/api/inquiries', json={**sample, 'message': message}, headers=api_headers)
    inquiry_id = resp.json()['id']
    client.post(f'/api/inquiries/{inquiry_id}/process', headers=api_headers)
    with client.app.state.test_session_factory() as db:
        import json
        details = json.loads(db.get(Inquiry, inquiry_id).requirements_json)
        assert details == ['Connect our website', 'Answer common questions', 'Collect leads']


def test_intent_specific_reply_does_not_promise_booking(client, api_headers, sample):
    _, draft_id = process_and_draft(client, api_headers, {**sample, 'subject': 'Book a demo', 'message': 'Can we book a demo tomorrow?'})
    with client.app.state.test_session_factory() as db:
        draft = db.get(Draft, draft_id)
        assert 'appointment' in draft.subject.lower()
        assert 'No appointment is confirmed yet' in draft.body


def test_pending_high_priority_shown_before_normal(client, api_headers, sample, admin_auth):
    process_and_draft(client, api_headers, {**sample, 'email':'normal@example.com', 'subject':'General request', 'message':'Please contact me about your services.'})
    process_and_draft(client, api_headers, {**sample, 'email':'priority@example.com', 'subject':'Urgent support request', 'message':'Urgent, our service is broken.'})
    page = client.get('/admin', auth=admin_auth)
    assert page.status_code == 200
    assert page.text.index('priority@example.com') < page.text.index('normal@example.com')
    assert 'high priority' in page.text


def test_reviewer_can_qualify_lead_and_audit(client, api_headers, sample, admin_auth):
    inquiry_id, draft_id = process_and_draft(client, api_headers, sample)
    with client.app.state.test_session_factory() as db:
        lead_id = db.scalar(select(Lead.id))
    response = client.post(f'/admin/leads/{lead_id}/status', data={
        'status': 'qualified', 'reviewer': 'Alex'
    }, auth=admin_auth, follow_redirects=False)
    assert response.status_code == 303
    with client.app.state.test_session_factory() as db:
        assert db.get(Lead, lead_id).status == 'qualified'
        audit = db.scalar(select(AuditEvent).where(AuditEvent.action == 'lead.status_changed'))
        assert audit.inquiry_id == inquiry_id
        assert 'reviewer=Alex' in audit.details
    # Sending an email must not erase manual qualification.
    response = client.post(f'/admin/drafts/{draft_id}/approve', data={
        'subject': 'Hello', 'body': 'Thanks for reaching out', 'reviewer': 'Alex'
    }, auth=admin_auth)
    assert response.status_code == 200
    with client.app.state.test_session_factory() as db:
        assert db.get(Lead, lead_id).status == 'qualified'


def test_lead_status_change_is_authenticated_and_validated(client, api_headers, sample, admin_auth):
    process_and_draft(client, api_headers, sample)
    with client.app.state.test_session_factory() as db:
        lead_id = db.scalar(select(Lead.id))
    endpoint = f'/admin/leads/{lead_id}/status'
    assert client.post(endpoint, data={'status':'archived','reviewer':'Alex'}).status_code == 401
    assert client.post(endpoint, auth=admin_auth, data={'status':'hacked', 'reviewer':'Alex'}).status_code == 422
    assert client.post(endpoint, auth=admin_auth, data={'status':'archived', 'reviewer':'   '}).status_code == 422
    assert client.post(endpoint, auth=admin_auth, headers={'Origin':'https://other.example'}, data={'status':'archived','reviewer':'Alex'}).status_code == 403
    assert client.post('/admin/leads/900123/status', auth=admin_auth, data={'status':'archived','reviewer':'Alex'}).status_code == 404
    with client.app.state.test_session_factory() as db:
        assert db.get(Lead, lead_id).status == 'new'
