"""Xray/Jira business logic extracted from xray_test_generator.py.

GUI prompts, CLI input(), and the original main() loop are intentionally not
copied over - this module only exposes pure functions that the Flask layer
calls. Every public function takes a ``jira_pat`` argument so that requests
are authenticated as the calling user.
"""
from __future__ import annotations

import json
import logging
import os
import re
import subprocess
from datetime import datetime
from typing import Optional

import requests
from openpyxl import Workbook
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


# -------------------------
# CONFIG
# -------------------------
JIRA_DOMAIN = os.environ.get("JIRA_DOMAIN", "https://jira.g2-networks.net").strip()
XRAY_SAMPLE_TEST_KEY = os.environ.get("XRAY_SAMPLE_TEST_KEY", "ILREP-880").strip()
XRAY_STEP_FIELD_FALLBACK = os.environ.get("XRAY_STEP_FIELD_FALLBACK", "customfield_10004").strip()
JIRA_LINK_TYPE_NAME = os.environ.get("JIRA_LINK_TYPE_NAME", "Tests").strip()
JIRA_HTTP_TIMEOUT = int(os.environ.get("JIRA_HTTP_TIMEOUT", "60"))
JIRA_RETRY_TOTAL = int(os.environ.get("JIRA_RETRY_TOTAL", "3"))
JIRA_RETRY_BACKOFF = float(os.environ.get("JIRA_RETRY_BACKOFF", "0.8"))
JIRA_VERIFY_SSL = os.environ.get("JIRA_VERIFY_SSL", "true").strip().lower()
JIRA_CA_BUNDLE = os.environ.get("JIRA_CA_BUNDLE", "").strip()

logger = logging.getLogger(__name__)
_HTTP_SESSION: Optional[requests.Session] = None


# -------------------------
# HTTP helpers
# -------------------------

def _headers(jira_pat: str, include_content_type: bool = True) -> dict:
    h = {"Authorization": f"Bearer {jira_pat}", "Accept": "application/json"}
    if include_content_type:
        h["Content-Type"] = "application/json"
    return h


def _verify_setting():
    if JIRA_CA_BUNDLE:
        return JIRA_CA_BUNDLE
    return JIRA_VERIFY_SSL not in ("0", "false", "no", "off")


def _http_session() -> requests.Session:
    global _HTTP_SESSION
    if _HTTP_SESSION is not None:
        return _HTTP_SESSION

    retry = Retry(
        total=JIRA_RETRY_TOTAL,
        connect=JIRA_RETRY_TOTAL,
        read=JIRA_RETRY_TOTAL,
        backoff_factor=JIRA_RETRY_BACKOFF,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=("GET", "POST", "PUT"),
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry)
    s = requests.Session()
    s.mount("https://", adapter)
    s.mount("http://", adapter)
    _HTTP_SESSION = s
    return s


def _request(method: str, jira_pat: str, path: str, *, params=None, payload: Optional[dict] = None):
    url = f"{JIRA_DOMAIN}{path}"
    try:
        return _http_session().request(
            method=method,
            url=url,
            headers=_headers(jira_pat, include_content_type=(payload is not None)),
            params=params,
            json=payload,
            timeout=JIRA_HTTP_TIMEOUT,
            verify=_verify_setting(),
        )
    except requests.exceptions.SSLError as e:
        raise RuntimeError(
            "Jira SSL handshake failed. If your environment uses corporate TLS inspection, "
            "set JIRA_CA_BUNDLE to your trusted PEM bundle (preferred), or set "
            "JIRA_VERIFY_SSL=false only for local testing."
        ) from e
    except requests.exceptions.RequestException as e:
        raise RuntimeError(f"Jira request failed: {e}") from e


def jira_get(jira_pat: str, path: str, params=None):
    r = _request("GET", jira_pat, path, params=params)
    r.raise_for_status()
    return r.json()


def jira_post(jira_pat: str, path: str, payload: dict):
    return _request("POST", jira_pat, path, payload=payload)


def jira_put(jira_pat: str, path: str, payload: dict):
    return _request("PUT", jira_pat, path, payload=payload)


def get_issue_fields(jira_pat: str, issue_key: str, fields: str):
    return jira_get(jira_pat, f"/rest/api/2/issue/{issue_key}", {"fields": fields}).get("fields", {})


def get_issue_all_fields(jira_pat: str, issue_key: str):
    return jira_get(jira_pat, f"/rest/api/2/issue/{issue_key}", {"fields": "*all"}).get("fields", {})


def get_editmeta(jira_pat: str, issue_key: str):
    return jira_get(jira_pat, f"/rest/api/2/issue/{issue_key}/editmeta")


# -------------------------
# Copilot prompt + JSON parsing
# -------------------------

def extract_json_array(text: str) -> list:
    cleaned = (text or "").strip()
    if not cleaned:
        raise ValueError("No JSON array found in Copilot output")
    if cleaned.startswith("```"):
        lines = cleaned.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].startswith("```"):
            lines = lines[:-1]
        cleaned = "\n".join(lines).strip()

    try:
        parsed = json.loads(cleaned)
        if isinstance(parsed, list):
            return parsed
        if isinstance(parsed, dict):
            for key in ("testcases", "test_cases", "cases", "items", "data"):
                v = parsed.get(key)
                if isinstance(v, list):
                    return v
    except Exception:
        pass

    decoder = json.JSONDecoder()
    for idx, ch in enumerate(cleaned):
        if ch not in "[{":
            continue
        try:
            candidate, _ = decoder.raw_decode(cleaned[idx:])
        except Exception:
            continue
        if isinstance(candidate, list):
            return candidate
        if isinstance(candidate, dict):
            for key in ("testcases", "test_cases", "cases", "items", "data"):
                v = candidate.get(key)
                if isinstance(v, list):
                    return v

    raise ValueError("No JSON array found in Copilot output")


def _normalize_prompt(prompt: str) -> str:
    rules = (
        "Output format rules (mandatory):\n"
        "- Return ONLY a valid JSON array.\n"
        "- No markdown code fences.\n"
        "- No explanations or extra text.\n"
        "- Each testcase object must include: Title, Preconditions, TestData, Steps, Priority, Type.\n"
        "- Steps must be an array of objects containing: Step, Action, Expected."
    )
    flow_rules = (
        "Execution flow rules (mandatory):\n"
        "- For every testcase, Steps must be self-contained and start from the beginning of the app flow.\n"
        "- The first step must open the application and navigate to the Home page (or Login page when auth is required).\n"
        "- Include all required setup/navigation/user actions inside Steps. Do NOT rely on Preconditions for executable steps.\n"
        "- Preconditions must not contain the main user actions to execute the testcase."
    )
    base = (prompt or "").strip()
    base_lower = base.lower()

    parts = []
    if base:
        parts.append(base)

    if "return only a valid json array" not in base_lower:
        parts.append(rules)

    if "execution flow rules (mandatory):" not in base_lower:
        parts.append(flow_rules)

    return "\n\n".join(parts).strip()


def build_generation_prompt(prompt: str, story_context: Optional[dict] = None) -> str:
    prepared = _normalize_prompt(prompt)
    if not story_context:
        return prepared
    ctx = json.dumps(story_context, ensure_ascii=False, indent=2)
    return ("Use the following Jira story context to generate manual test cases.\n\n"
            f"Story Context:\n{ctx}\n\nTask:\n{prepared}")


def _normalize_step_item(step: object) -> dict:
    """Normalize a generated step into {Step, Action, Expected}."""
    if isinstance(step, dict):
        step_no = step.get("Step")
        action = (step.get("Action") or step.get("action") or "").strip()
        expected = (
            step.get("Expected")
            or step.get("expected")
            or step.get("Expected Result")
            or step.get("expected result")
            or ""
        )
        expected = str(expected).strip()
        try:
            step_no = int(step_no)
        except Exception:
            step_no = 0
        return {"Step": step_no, "Action": action, "Expected": expected}

    text = str(step or "").strip()
    if "->" in text:
        action, expected = text.split("->", 1)
        return {"Step": 0, "Action": action.strip(), "Expected": expected.strip()}
    return {"Step": 0, "Action": text, "Expected": ""}


def _enforce_start_from_beginning(testcases: list) -> list:
    """Ensure every testcase has self-contained app-entry and navigation steps."""
    startup_steps = [
        {
            "Action": "Open the application in a browser.",
            "Expected": "Application is launched successfully.",
        },
        {
            "Action": "Navigate to the Home page (log in first if prompted).",
            "Expected": "Home page is displayed and the user can start the flow.",
        },
    ]
    # Only skip prepending if step 1 already starts with the exact phrase we insert,
    # so we never get false positives from words like "home page" appearing mid-step.
    _open_marker = "open the application"
    _nav_marker = "navigate to the home page"

    normalized_cases = []
    for tc in testcases or []:
        case = dict(tc or {})
        existing_steps = [_normalize_step_item(s) for s in (case.get("Steps") or [])]

        first_action = (existing_steps[0].get("Action") or "").lower().strip() if existing_steps else ""
        has_startup = first_action.startswith(_open_marker) or first_action.startswith(_nav_marker)

        merged = []
        if not has_startup:
            for step in startup_steps:
                merged.append({"Step": 0, "Action": step["Action"], "Expected": step["Expected"]})
        merged.extend(existing_steps)

        final_steps = []
        for idx, step in enumerate(merged, start=1):
            final_steps.append(
                {
                    "Step": idx,
                    "Action": (step.get("Action") or "").strip(),
                    "Expected": (step.get("Expected") or "").strip(),
                }
            )

        case["Steps"] = final_steps
        normalized_cases.append(case)

    return normalized_cases


DEFAULT_PROMPT = """
Generate MANUAL test cases for the provided Jira story context.

Output ONLY a JSON array like this:
[
  {
    "Title": "",
    "Preconditions": "",
    "TestData": "",
    "Steps": [
      { "Step": 1, "Action": "", "Expected": "" }
    ],
    "Priority": "P1|P2|P3",
    "Type": "Positive|Negative|Boundary|Validation"
  }
]

Rules:
- Minimum 10 test cases
- Include negative & boundary cases
- For each testcase, Steps must start from app launch and navigation to Home/Login page.
- Include end-to-end user actions in Steps; do not rely on Preconditions for execution flow.
- No markdown
- No explanations
- Output JSON ONLY
""".strip()


def generate_testcases(prompt: str, story_context: Optional[dict] = None) -> list:
    prepared = build_generation_prompt(prompt, story_context)
    result = subprocess.run(
        "copilot --silent",
        input=prepared,
        text=True,
        encoding="utf-8",
        errors="replace",
        shell=True,
        capture_output=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"Copilot command failed: {result.stderr}")
    output = (result.stdout or "").strip()
    if not output:
        raise RuntimeError("Copilot returned empty output")
    return _enforce_start_from_beginning(extract_json_array(output))


# -------------------------
# Story context
# -------------------------

def _detect_field_id_by_name(meta_fields: dict, exact_name: str) -> Optional[str]:
    target = exact_name.strip().lower()
    for fid, meta in meta_fields.items():
        if (meta.get("name") or "").strip().lower() == target:
            return fid
    return None


def _detect_acceptance_criteria_field(jira_pat: str, story_key: str) -> Optional[str]:
    try:
        meta = get_editmeta(jira_pat, story_key)
        return _detect_field_id_by_name(meta.get("fields", {}), "Acceptance Criteria")
    except Exception:
        return None


def fetch_story_details(jira_pat: str, story_key: str) -> dict:
    fields_all = get_issue_all_fields(jira_pat, story_key)
    summary = (fields_all.get("summary") or "").strip()
    description = (fields_all.get("description") or "").strip()
    ac_text = ""
    ac_id = _detect_acceptance_criteria_field(jira_pat, story_key)
    if ac_id:
        try:
            ac_val = get_issue_fields(jira_pat, story_key, ac_id).get(ac_id)
            if isinstance(ac_val, str):
                ac_text = ac_val.strip()
            elif ac_val is not None:
                ac_text = json.dumps(ac_val, ensure_ascii=False)
        except Exception:
            ac_text = ""
    return {"summary": summary, "description": description, "acceptance": ac_text}


def build_story_context(jira_pat: str, story_key: str) -> dict:
    issue = jira_get(
        jira_pat,
        f"/rest/api/2/issue/{story_key}",
        {"fields": "summary,description,labels,priority,issuetype"},
    )
    fields = issue.get("fields", {}) or {}
    details = fetch_story_details(jira_pat, story_key)
    return {
        "story_key": story_key,
        "issue_type": ((fields.get("issuetype") or {}).get("name") or "").strip(),
        "priority": ((fields.get("priority") or {}).get("name") or "").strip(),
        "labels": fields.get("labels") or [],
        "summary": details["summary"],
        "description": details["description"],
        "acceptance_criteria": details["acceptance"],
    }


# -------------------------
# Xray field detection
# -------------------------

def detect_test_issuetype_name(jira_pat: str) -> str:
    fields = get_issue_fields(jira_pat, XRAY_SAMPLE_TEST_KEY, "issuetype")
    name = ((fields.get("issuetype") or {}).get("name"))
    if not name:
        raise RuntimeError(f"Unable to detect issuetype name from {XRAY_SAMPLE_TEST_KEY}")
    return name


def detect_steps_field_id(jira_pat: str) -> str:
    meta = get_editmeta(jira_pat, XRAY_SAMPLE_TEST_KEY)
    fm = meta.get("fields", {})
    fid = _detect_field_id_by_name(fm, "Manual Test Steps")
    if fid:
        return fid
    for k, v in fm.items():
        nm = (v.get("name") or "").lower()
        if "manual" in nm and "step" in nm:
            return k
        if "test" in nm and "step" in nm:
            return k
    return XRAY_STEP_FIELD_FALLBACK


def detect_test_type_field(jira_pat: str):
    meta = get_editmeta(jira_pat, XRAY_SAMPLE_TEST_KEY)
    fm = meta.get("fields", {})
    fid = _detect_field_id_by_name(fm, "Test Type")
    field_meta = fm.get(fid) if fid else None
    if not fid:
        for k, m in fm.items():
            if "test type" in (m.get("name") or "").lower():
                fid, field_meta = k, m
                break
    if not fid:
        return None, None
    for opt in (field_meta.get("allowedValues") or []):
        if not isinstance(opt, dict):
            continue
        label = (opt.get("value") or opt.get("name") or "").strip()
        if label.lower() == "manual":
            if opt.get("value"):
                return fid, {"value": opt["value"]}
            if opt.get("name"):
                return fid, {"value": opt["name"]}
            if opt.get("id"):
                return fid, {"id": opt["id"]}
    return fid, {"value": "Manual"}


def _copy_required_fields_from_sample(jira_pat: str, base_fields: dict) -> None:
    meta = get_editmeta(jira_pat, XRAY_SAMPLE_TEST_KEY)
    required_ids = [fid for fid, m in meta.get("fields", {}).items() if m.get("required") is True]
    for fid in required_ids:
        if fid in base_fields or fid in ("summary", "description", "project", "issuetype"):
            continue
        try:
            val = get_issue_fields(jira_pat, XRAY_SAMPLE_TEST_KEY, fid).get(fid)
        except Exception:
            val = None
        if val is not None:
            base_fields[fid] = val


# -------------------------
# Build payloads
# -------------------------

def _clean_summary(raw: str, story_key: str) -> str:
    """Strip story key / TC### prefixes and ensure summary starts with 'Verify'."""
    s = (raw or "").strip()
    # Strip leading story key (e.g., "ILREP-782 - ", "ILREP-782_TC001 - ", "ILREP-782:")
    s = re.sub(rf"^\s*{re.escape(story_key)}[\s_:\-]+", "", s, flags=re.IGNORECASE)
    # Strip leading test-id token (e.g., "TC001 - ", "TC_01: ")
    s = re.sub(r"^\s*TC[_\-]?\d+[\s_:\-]+", "", s, flags=re.IGNORECASE)
    # Strip leading "Verify the " / "Verify " so we can normalize
    s = re.sub(r"^\s*verify(\s+the)?\s+", "", s, flags=re.IGNORECASE).strip()
    if not s:
        s = f"scenario for {story_key}"
    # Capitalize first letter without altering rest
    s = s[0].upper() + s[1:] if s else s
    return f"Verify {s}"


def build_description(story_key: str, tc: dict) -> str:
    return _clean_summary(tc.get("Title") or "", story_key)


def build_steps_payload(tc: dict) -> dict:
    steps = []
    for i, s in enumerate(tc.get("Steps", []), start=1):
        steps.append({
            "index": i,
            "fields": {
                "action": (s.get("Action") or "").strip(),
                "data": (tc.get("TestData") or "").strip(),
                "expected result": (s.get("Expected") or "").strip(),
            },
        })
    return {"steps": steps}


def steps_as_text(tc: dict) -> tuple[str, str]:
    """Return (steps_text, expected_text) suitable for DB persistence."""
    actions, expecteds = [], []
    for i, s in enumerate(tc.get("Steps", []) or [], start=1):
        a = (s.get("Action") or "").strip()
        e = (s.get("Expected") or "").strip()
        if a:
            actions.append(f"{i}. {a}")
        if e:
            expecteds.append(f"{i}. {e}")
    return "\n".join(actions), "\n".join(expecteds)


# -------------------------
# Create / update / link
# -------------------------

def _create_issue(jira_pat: str, fields: dict) -> str:
    r = jira_post(jira_pat, "/rest/api/2/issue", {"fields": fields})
    if r.status_code not in (200, 201):
        raise RuntimeError(f"Create failed ({r.status_code}): {r.text}")
    return r.json().get("key")


def _update_fields(jira_pat: str, key: str, fields: dict) -> None:
    r = jira_put(jira_pat, f"/rest/api/2/issue/{key}", {"fields": fields})
    if r.status_code not in (200, 204):
        raise RuntimeError(f"Update failed ({r.status_code}): {r.text}")


def link_test_to_story(jira_pat: str, test_key: str, story_key: str,
                       link_type_name: str = JIRA_LINK_TYPE_NAME) -> None:
    payload = {"type": {"name": link_type_name},
               "inwardIssue": {"key": test_key},
               "outwardIssue": {"key": story_key}}
    r = jira_post(jira_pat, "/rest/api/2/issueLink", payload)
    if r.status_code in (400, 404):
        swapped = {"type": {"name": link_type_name},
                   "inwardIssue": {"key": story_key},
                   "outwardIssue": {"key": test_key}}
        r2 = jira_post(jira_pat, "/rest/api/2/issueLink", swapped)
        if r2.status_code not in (200, 201):
            raise RuntimeError(f"Linking failed: {r.text} | retry: {r2.text}")
        return
    if r.status_code not in (200, 201):
        raise RuntimeError(f"Linking failed ({r.status_code}): {r.text}")


def create_xray_test(
    jira_pat: str,
    *,
    story_key: str,
    project_key: str,
    issuetype_name: str,
    steps_field_id: str,
    test_type_field_id: Optional[str],
    test_type_payload: Optional[dict],
    tc: dict,
) -> str:
    summary = _clean_summary(tc.get("Title") or "", story_key)

    base_fields = {
        "project": {"key": project_key},
        "issuetype": {"name": issuetype_name},
        "summary": summary,
        "description": build_description(story_key, tc),
    }
    _copy_required_fields_from_sample(jira_pat, base_fields)
    if test_type_field_id and test_type_payload:
        base_fields[test_type_field_id] = test_type_payload

    steps_payload = build_steps_payload(tc)
    fields_with_steps = dict(base_fields)
    fields_with_steps[steps_field_id] = steps_payload

    try:
        return _create_issue(jira_pat, fields_with_steps)
    except Exception as e:
        msg = str(e)
        if "cannot be set" in msg or steps_field_id in msg or "Manual Test Steps" in msg:
            test_key = _create_issue(jira_pat, base_fields)
            _update_fields(jira_pat, test_key, {steps_field_id: steps_payload})
            return test_key
        raise


# -------------------------
# Excel export
# -------------------------

def export_testcases_to_excel(story_key: str, testcases: list, out_dir: str) -> str:
    wb = Workbook()
    ws = wb.active
    ws.title = "TestCases"
    ws.append(["Serial Number", "Title", "Preconditions", "TestData", "Priority", "Type", "Steps"])
    for serial_no, tc in enumerate(testcases, start=1):
        steps_text_lines = []
        for i, s in enumerate(tc.get("Steps", []) or [], start=1):
            a = (s.get("Action") or "").strip()
            e = (s.get("Expected") or "").strip()
            if a and e:
                steps_text_lines.append(f"{i}. {a} -> {e}")
            elif a:
                steps_text_lines.append(f"{i}. {a}")
            elif e:
                steps_text_lines.append(f"{i}. Expected: {e}")
        ws.append([
            serial_no,
            tc.get("Title", ""),
            tc.get("Preconditions", ""),
            tc.get("TestData", ""),
            tc.get("Priority", ""),
            tc.get("Type", ""),
            "\n".join(steps_text_lines),
        ])
    os.makedirs(out_dir, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_file = os.path.join(out_dir, f"Manual_TestCases_For_Story_{story_key}_{timestamp}.xlsx")
    wb.save(out_file)
    return out_file
