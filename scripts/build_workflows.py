"""Create importable n8n workflows with no embedded credentials."""
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent / "n8n" / "workflows"
ROOT.mkdir(parents=True, exist_ok=True)


def node(name, kind, version, position, parameters, uid):
    return {"parameters": parameters, "id": uid, "name": name, "type": kind, "typeVersion": version, "position": position}


def http_node(name, path, x, uid, send_body=False):
    parameters = {
        "method": "POST",
        "url": "={{ 'http://api:8000/api/inquiries/' + $json.id + '/" + path + "' }}",
        "sendHeaders": True,
        "headerParameters": {"parameters": [{"name": "X-Internal-Token", "value": "={{ $env.INTERNAL_API_TOKEN }}"}]},
        "options": {"timeout": 30000},
    }
    if send_body:
        parameters.update({"sendBody": True, "specifyBody": "json", "jsonBody": "={{ JSON.stringify($json.body) }}"})
    return node(name, "n8n-nodes-base.httpRequest", 4.2, [x, 240], parameters, uid)


webhook = node("Customer inquiry webhook", "n8n-nodes-base.webhook", 2, [220, 240], {
    "httpMethod": "POST", "path": "customer-inquiry", "responseMode": "lastNode",
    "responseData": "firstEntryJson", "options": {}
}, "webhook-customer-inquiry")
webhook["webhookId"] = "portfolio-customer-inquiry"

create = node("1 · Store inquiry", "n8n-nodes-base.httpRequest", 4.2, [490, 240], {
    "method": "POST", "url": "http://api:8000/api/inquiries",
    "sendHeaders": True,
    "headerParameters": {"parameters": [{"name": "X-Internal-Token", "value": "={{ $env.INTERNAL_API_TOKEN }}"}]},
    "sendBody": True,
    "specifyBody": "json",
    "jsonBody": "={{ JSON.stringify($json.body) }}",
    "options": {"timeout": 30000},
}, "store-inquiry")

process = http_node("2 · AI classification + CRM", "process", 760, "process-inquiry")
draft = http_node("3 · Create approval draft", "draft", 1030, "draft-inquiry")

workflow = {
    "name": "AI Email & CRM - Intake and Human Review",
    "nodes": [webhook, create, process, draft],
    "connections": {
        "Customer inquiry webhook": {"main": [[{"node": "1 · Store inquiry", "type": "main", "index": 0}]]},
        "1 · Store inquiry": {"main": [[{"node": "2 · AI classification + CRM", "type": "main", "index": 0}]]},
        "2 · AI classification + CRM": {"main": [[{"node": "3 · Create approval draft", "type": "main", "index": 0}]]},
    },
    "settings": {"executionOrder": "v1", "timezone": "Europe/Vienna"},
    "active": False,
    "pinData": {},
    "tags": [],
}
(ROOT / "01-intake-and-review.json").write_text(json.dumps(workflow, indent=2, ensure_ascii=False) + "\n")

schedule = node("Every morning at 09:00", "n8n-nodes-base.scheduleTrigger", 1.2, [260, 220], {
    "rule": {"interval": [{"field": "days", "daysInterval": 1, "triggerAtHour": 9, "triggerAtMinute": 0}]}
}, "daily-followup-schedule")
prepare = node("Prepare opt-in follow-up drafts", "n8n-nodes-base.httpRequest", 4.2, [560, 220], {
    "method": "POST",
    "url": "http://api:8000/api/followups/prepare",
    "sendHeaders": True,
    "headerParameters": {"parameters": [{"name": "X-Internal-Token", "value": "={{ $env.INTERNAL_API_TOKEN }}"}]},
    "options": {"timeout": 30000},
}, "prepare-followup-drafts")
followup = {
    "name": "AI Email & CRM - Opt-in Follow-up Drafts",
    "nodes": [schedule, prepare],
    "connections": {"Every morning at 09:00": {"main": [[{"node": "Prepare opt-in follow-up drafts", "type": "main", "index": 0}]]}},
    "settings": {"executionOrder": "v1", "timezone": "Europe/Vienna"},
    "active": False,
    "pinData": {},
    "tags": [],
}
(ROOT / "02-scheduled-followups.json").write_text(json.dumps(followup, indent=2, ensure_ascii=False) + "\n")
print("Wrote n8n workflow exports to", ROOT)
