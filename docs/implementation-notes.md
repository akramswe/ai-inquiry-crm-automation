# Reviewed version: what changed and why

This project was updated from an AI-assisted starter demo. This document records concrete engineering changes; it does not claim the work was written without AI assistance.

## Where to find changes

| File | What changed | Why it matters |
| --- | --- | --- |
| `app/services.py` | Full-payload idempotency validation; intent-aware reply templates; manual lead status updates and audit events | Avoid reusing a webhook ID for a changed request; give reviewers a useful first draft and a simple CRM triage tool |
| `app/ai.py` | Whole-word/phrase rules, incident-first classification, German terms, bullet extraction | Reduce false keyword matches; demo more realistic inquiry types |
| `app/main.py` | Priority-ordered pending queue and `POST /admin/leads/{id}/status` endpoint | Show high-priority cases first and support human qualification |
| `app/templates/admin.html` | Priority labels, summaries, lead status controls | Make the actual operator workflow visible |
| `app/static/style.css` | Styling for CRM controls and urgent items | Keep the form usable on desktop |
| `tests/test_review_improvements.py` | Regression tests for all of the above | Ensure improvements don't silently break |

## How to test locally

```bash
python -m pip install -r requirements-dev.txt
python -m pytest -q
python scripts/validate_workflows.py
```

For a manual end-to-end demo, follow the README's Docker Compose instructions, import both workflow JSON files, submit two inquiries (one containing **urgent support**), confirm the urgent item appears first, change its lead status to **qualified**, approve its draft, then check the Mailpit inbox and audit trail.

## What was not changed

- n8n workflow exports are still simple orchestration (create -> classify -> draft, plus daily follow-up sweep), not a Gmail/Outlook connection.
- `AI_MODE=mock` does not call a language model. Enable `AI_MODE=openai` with your own credentials only if you choose to test a real LLM.
- SQLite is a local demo CRM, not HubSpot or Salesforce.
- The HTTP Basic reviewer UI, workflow webhook, and SMTP delivery path are **not production hardened**.

## What to customize yourself before sharing

1. Run the full stack and take real screenshots of the workflow execution and approval queue.
2. Change the reply wording in `compose_draft()` to suit a service you know well.
3. Add another classification phrase or category, with a test that documents the intended behavior.
4. Commit each change you actually make. Don't manufacture Git history or claim independent authorship of generated code.
