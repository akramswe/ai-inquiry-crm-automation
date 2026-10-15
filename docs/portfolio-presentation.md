# Presenting this project to an AI automation company

## One-minute explanation

> I built a customer inquiry automation demo connecting n8n with a Python FastAPI backend. When someone submits a web form or webhook inquiry, n8n orchestrates intake, AI classification and CRM lead creation. The Python API stores data in SQLite, produces a draft response, and holds it for human review. An authenticated dashboard lets a reviewer edit, approve or reject replies. Approved messages appear in a local Mailpit inbox. I also included consent-gated follow-up drafts, audit logging, idempotent workflows, Docker setup and tests.

## What to show in a live demo

1. Show the exported n8n workflow and explain where the internal API token is configured.
2. Submit a **test inquiry** through the web form.
3. Open the n8n execution view to show each HTTP node succeeding.
4. Open the dashboard to show classification, CRM lead and pending draft.
5. Modify the draft, approve it, and show delivery in Mailpit.
6. Mention why no message is sent automatically before approval.
7. Explain the optional OpenAI mode, the offline mock mode, and what you'd harden in production.

## Suggested CV bullet points (only once you have personally run / verified the project)

- Built an end-to-end AI-assisted customer inquiry workflow using **n8n, FastAPI, Python, SQLite, and REST APIs**, integrating lead classification, CRM updates, and response drafting.
- Implemented a **human-in-the-loop approval dashboard**, consent-based follow-up drafts, audit logging, request validation, and idempotent processing.
- Containerized the solution using **Docker Compose**, with local SMTP email testing via Mailpit and automated backend tests in GitHub Actions.

## What not to claim

- Don't describe the mock classifier as a live LLM unless you've enabled and tested OpenAI mode.
- Don't say this integrates with Salesforce, HubSpot, or Gmail; those external connectors are future work.
- Don't claim production deployment, hundreds of users, or measurable business savings without evidence.

## Good engineering discussion points

- Why keep sensitive customer replies behind a human approval gate?
- How do you handle duplicate n8n webhook deliveries?
- What is the tradeoff between SQLite for demos and Postgres for production?
- How would you add a retry queue / transactional outbox before sending external emails?
- What are the risks of forwarding customer text to a third-party LLM?
