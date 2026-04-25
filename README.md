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

## Database

SQLite tables created on first run:

- `Tb_user`
- `Tb_Xray_Testcase_Created`
- `Tb_Testcase_Export`
