# Jira Xray Test Creation

Flask web app that generates manual test cases for Jira stories (via Copilot CLI) and either exports them as Excel or creates linked Xray Test issues in Jira. Includes user signup/login and an admin console.

## Quick start

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python run.py
```

Open http://127.0.0.1:5000.

Default admin credentials: **Admin / Welcome@1**

End users sign up with an `@brightstarlottery.com` email and an 8+ character password, then set their Jira PAT in **Profile**.

## Layout

- `run.py` – entry point
- `app/` – Flask application (auth, main, admin blueprints + Jinja templates + CSS)
- `app/xray_service.py` – Jira/Xray + Copilot + Excel logic (refactored from `xray_test_generator.py`)
- `xray_test_generator.py` – original standalone CLI (still works)

## Configuration

Environment variables (all optional):

| Variable | Default | Purpose |
| --- | --- | --- |
| `APP_SECRET_KEY` | `dev-secret-change-me` | Flask session signing key |
| `APP_DATABASE` | `<repo>/app_data.db` | SQLite file path |
| `JIRA_DOMAIN` | `https://jira.g2-networks.net` | Jira base URL |
| `XRAY_SAMPLE_TEST_KEY` | `ILREP-880` | Sample Xray Test issue used for field auto-detection |
| `JIRA_LINK_TYPE_NAME` | `Tests` | Issue link type used to link Test → Story |
| `AGENT_API_TOKEN` | _(empty)_ | Required token for `/api/agent/*` endpoints |

## Database

SQLite tables created on first run:

- `Tb_user`
- `Tb_Xray_Testcase_Created`
- `Tb_Testcase_Export`

## Copilot Custom Agent

This workspace now includes a custom GitHub Copilot agent:

- `.github/agents/jira-xray-test-designer.agent.md`

How to use in VS Code:

1. Open Copilot Chat.
2. Choose the **Jira Xray Test Designer** agent from the agent picker.
3. Provide your task, for example:
	- "Generate testcases for ILREP-869 as Excel"
	- "Run the Flask app and troubleshoot login"
	- "Refine generated tests to enforce Home(Login) start flow"

The agent is configured for this repo's Jira/Xray test generation workflow and can read/edit files, run commands, and validate outcomes.

## Agent-Callable API And CLI

The app now exposes a token-protected API endpoint that external agents can call directly.

- Health: `GET /api/agent/health`
- Generate/execute: `POST /api/agent/generate`

### 1) Start web server

```powershell
$env:AGENT_API_TOKEN = "change-me-strong-token"
python run.py
```

### 2) Call endpoint directly (PowerShell)

```powershell
$headers = @{ "X-Agent-Token" = $env:AGENT_API_TOKEN }
$body = @{
	story_key = "ILREP-869"
	jira_pat = $env:JIRA_PAT
	action = "preview"   # preview | excel | jira
	include_testcases = $true
} | ConvertTo-Json

Invoke-RestMethod -Method Post -Uri "http://127.0.0.1:5000/api/agent/generate" -Headers $headers -ContentType "application/json" -Body $body
```

### 3) Call via bundled CLI client

```powershell
python agent_client.py --story ILREP-869 --action preview --jira-pat $env:JIRA_PAT --agent-token $env:AGENT_API_TOKEN
```

`action` behavior:

- `preview`: generates testcases and returns JSON only
- `excel`: generates testcases and writes Excel under `exports/`
- `jira`: generates testcases, creates Xray tests in Jira, links them to the story, and records history
