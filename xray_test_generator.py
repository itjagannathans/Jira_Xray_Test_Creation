import os
import sys
import json
import subprocess
import logging
from datetime import datetime
import requests
from openpyxl import Workbook

# =========================================================
# XRAY TEST GENERATOR (FULL CORRECTED VERSION)
# - Option 1: Generate Excel of manual test cases
# - Option 2: Create Xray Test issues + link to Story
# =========================================================

# -------------------------
# CONFIG
# -------------------------
JIRA_DOMAIN = os.environ.get("JIRA_DOMAIN", "https://jira.g2-networks.net").strip()

# Sample Xray Test key in the same target project used for auto-detection
XRAY_SAMPLE_TEST_KEY = os.environ.get("XRAY_SAMPLE_TEST_KEY", "ILREP-880").strip()

# Fallback field if detection fails
XRAY_STEP_FIELD_FALLBACK = os.environ.get("XRAY_STEP_FIELD_FALLBACK", "customfield_10004").strip()

# Issue link type name used to link Test -> Story
JIRA_LINK_TYPE_NAME = os.environ.get("JIRA_LINK_TYPE_NAME", "Tests").strip()

# Files
LOG_FILE = "xray_test_generator.log"
COPILOT_RAW_FILE = "copilot_raw_output.txt"
ISSUE_JSON_FILE = "issue.json"
META_DEBUG_FILE = "xray_meta_debug.json"

# -------------------------
# LOGGING
# -------------------------
logging.basicConfig(
    filename=LOG_FILE,
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s"
)

def console(msg: str):
    print(msg, flush=True)
    logging.info(msg)


def wait_for_any_key(prompt: str = "\nPress any key to exit..."):
    """Wait for a single keypress on Windows; fallback to Enter on other platforms."""
    try:
        import msvcrt
        print(prompt, end="", flush=True)
        msvcrt.getch()
        print()
    except Exception:
        input(prompt)


def jira_headers(jira_pat: str, include_content_type: bool = True):
    h = {
        "Authorization": f"Bearer {jira_pat}",
        "Accept": "application/json",
    }
    if include_content_type:
        h["Content-Type"] = "application/json"
    return h


# -------------------------
# SUBPROCESS (COPILOT)
# -------------------------

def run(cmd, input_text=None):
    return subprocess.run(
        cmd,
        input=input_text,
        text=True,
        encoding="utf-8",
        errors="replace",
        shell=True,
        capture_output=True
    )


def extract_json_array(text: str):
    """Extract test cases JSON array from Copilot output with tolerant parsing."""
    cleaned = (text or "").strip()
    if not cleaned:
        raise ValueError("No JSON array found in Copilot output")

    # Handle markdown fenced output.
    if cleaned.startswith("```"):
        lines = cleaned.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].startswith("```"):
            lines = lines[:-1]
        cleaned = "\n".join(lines).strip()

    # Direct parse first.
    try:
        parsed = json.loads(cleaned)
        if isinstance(parsed, list):
            return parsed
        if isinstance(parsed, dict):
            for key in ("testcases", "test_cases", "cases", "items", "data"):
                value = parsed.get(key)
                if isinstance(value, list):
                    return value
    except Exception:
        pass

    # Scan for first decodable JSON array/object inside mixed text.
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
                value = candidate.get(key)
                if isinstance(value, list):
                    return value

    raise ValueError("No JSON array found in Copilot output")


def normalize_generation_prompt(prompt: str) -> str:
    """Ensure prompt enforces machine-parseable JSON-array output."""
    strict_rules = """
Output format rules (mandatory):
- Return ONLY a valid JSON array.
- No markdown code fences.
- No explanations or extra text.
- Each testcase object must include: Title, Preconditions, TestData, Steps, Priority, Type.
- Steps must be an array of objects containing: Step, Action, Expected.
""".strip()

    flow_rules = """
Execution flow rules (mandatory):
- For every testcase, Steps must be self-contained and start from the beginning of the app flow.
- The first step must open the application and navigate to the Home page (or Login page when auth is required).
- Include all required setup/navigation/user actions inside Steps. Do NOT rely on Preconditions for executable steps.
- Preconditions must not contain the main user actions to execute the testcase.
""".strip()

    base = (prompt or "").strip()
    base_lower = base.lower()

    parts = []
    if base:
        parts.append(base)

    if "return only a valid json array" not in base_lower:
        parts.append(strict_rules)

    if "execution flow rules (mandatory):" not in base_lower:
        parts.append(flow_rules)

    return "\n\n".join(parts).strip()


def build_generation_prompt(prompt: str, story_context: dict | None = None) -> str:
    """Compose the final Copilot prompt with compact Jira story context."""
    prepared_prompt = normalize_generation_prompt(prompt)
    if not story_context:
        return prepared_prompt

    context_json = json.dumps(story_context, ensure_ascii=False, indent=2)
    return (
        "Use the following Jira story context to generate manual test cases.\n\n"
        f"Story Context:\n{context_json}\n\n"
        f"Task:\n{prepared_prompt}"
    )


def normalize_step_item(step: object) -> dict:
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


def enforce_start_from_beginning(testcases: list) -> list:
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
        existing_steps = [normalize_step_item(s) for s in (case.get("Steps") or [])]

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


# -------------------------
# JIRA API HELPERS
# -------------------------

def jira_get(jira_pat: str, path: str, params=None):
    url = f"{JIRA_DOMAIN}{path}"
    r = requests.get(url, headers=jira_headers(jira_pat, include_content_type=False), params=params, timeout=60)
    r.raise_for_status()
    return r.json()


def jira_post(jira_pat: str, path: str, payload: dict):
    url = f"{JIRA_DOMAIN}{path}"
    r = requests.post(url, headers=jira_headers(jira_pat, include_content_type=True), json=payload, timeout=60)
    return r


def jira_put(jira_pat: str, path: str, payload: dict):
    url = f"{JIRA_DOMAIN}{path}"
    r = requests.put(url, headers=jira_headers(jira_pat, include_content_type=True), json=payload, timeout=60)
    return r


def get_issue_fields(jira_pat: str, issue_key: str, fields: str):
    return jira_get(jira_pat, f"/rest/api/2/issue/{issue_key}", params={"fields": fields}).get("fields", {})


def get_issue_all_fields(jira_pat: str, issue_key: str):
    return jira_get(jira_pat, f"/rest/api/2/issue/{issue_key}", params={"fields": "*all"}).get("fields", {})


def get_editmeta(jira_pat: str, issue_key: str):
    return jira_get(jira_pat, f"/rest/api/2/issue/{issue_key}/editmeta")


def save_meta_debug(jira_pat: str):
    try:
        meta = get_editmeta(jira_pat, XRAY_SAMPLE_TEST_KEY)
        with open(META_DEBUG_FILE, "w", encoding="utf-8") as f:
            json.dump(meta, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logging.warning(f"Failed saving meta debug: {e}")


# -------------------------
# AUTO-DETECT XRAY FIELDS
# -------------------------

def detect_test_issuetype_name(jira_pat: str) -> str:
    fields = get_issue_fields(jira_pat, XRAY_SAMPLE_TEST_KEY, "issuetype")
    name = ((fields.get("issuetype") or {}).get("name"))
    if not name:
        raise RuntimeError(f"Unable to detect issuetype name from {XRAY_SAMPLE_TEST_KEY}")
    return name


def detect_field_id_by_exact_name(editmeta_fields: dict, exact_name: str):
    target = exact_name.strip().lower()
    for fid, meta in editmeta_fields.items():
        nm = (meta.get("name") or "").strip().lower()
        if nm == target:
            return fid
    return None


def detect_steps_field_id(jira_pat: str) -> str:
    meta = get_editmeta(jira_pat, XRAY_SAMPLE_TEST_KEY)
    fields_meta = meta.get("fields", {})

    # Prefer exact name
    fid = detect_field_id_by_exact_name(fields_meta, "Manual Test Steps")
    if fid:
        return fid

    # Heuristic match
    for k, v in fields_meta.items():
        nm = (v.get("name") or "").lower()
        if "manual" in nm and "step" in nm:
            return k
        if "test" in nm and "step" in nm:
            return k

    return XRAY_STEP_FIELD_FALLBACK


def detect_test_type_field(jira_pat: str):
    """Return (field_id, payload_to_set_manual) or (None, None)."""
    meta = get_editmeta(jira_pat, XRAY_SAMPLE_TEST_KEY)
    fields_meta = meta.get("fields", {})

    field_id = None
    field_meta = None

    # Common exact name
    field_id = detect_field_id_by_exact_name(fields_meta, "Test Type")
    if field_id:
        field_meta = fields_meta[field_id]
    else:
        # Heuristic
        for fid, m in fields_meta.items():
            if "test type" in (m.get("name") or "").lower():
                field_id, field_meta = fid, m
                break

    if not field_id:
        return None, None

    # Determine allowed Manual option
    allowed = field_meta.get("allowedValues") or []
    for opt in allowed:
        if not isinstance(opt, dict):
            continue
        label = (opt.get("value") or opt.get("name") or "").strip()
        if label.lower() == "manual":
            if opt.get("value"):
                return field_id, {"value": opt["value"]}
            if opt.get("name"):
                return field_id, {"value": opt["name"]}
            if opt.get("id"):
                return field_id, {"id": opt["id"]}

    # Fallback payload
    return field_id, {"value": "Manual"}


def copy_required_fields_from_sample(jira_pat: str, base_fields: dict):
    """If target project enforces required custom fields, copy them from XRAY_SAMPLE_TEST_KEY."""
    meta = get_editmeta(jira_pat, XRAY_SAMPLE_TEST_KEY)
    fields_meta = meta.get("fields", {})

    required_ids = [fid for fid, m in fields_meta.items() if m.get("required") is True]
    if not required_ids:
        return

    # Fetch values from sample test (only required fields)
    for fid in required_ids:
        if fid in base_fields:
            continue
        # Do not override core creation fields
        if fid in ("summary", "description", "project", "issuetype"):
            continue
        try:
            val = get_issue_fields(jira_pat, XRAY_SAMPLE_TEST_KEY, fid).get(fid)
        except Exception:
            val = None
        if val is not None:
            base_fields[fid] = val


# -------------------------
# STORY DETAILS (for rich Test Description)
# -------------------------

def detect_acceptance_criteria_field_id(jira_pat: str, story_key: str):
    """Try to find an 'Acceptance Criteria' field id for the story using editmeta."""
    try:
        meta = get_editmeta(jira_pat, story_key)
        fields_meta = meta.get("fields", {})
        return detect_field_id_by_exact_name(fields_meta, "Acceptance Criteria")
    except Exception:
        return None


def fetch_story_details(jira_pat: str, story_key: str) -> dict:
    """Fetch summary, description and acceptance criteria value (if available)."""
    fields_all = get_issue_all_fields(jira_pat, story_key)

    summary = (fields_all.get("summary") or "").strip()
    description = (fields_all.get("description") or "").strip()

    ac_text = ""
    ac_id = detect_acceptance_criteria_field_id(jira_pat, story_key)
    if ac_id:
        try:
            ac_val = get_issue_fields(jira_pat, story_key, ac_id).get(ac_id)
            if isinstance(ac_val, str):
                ac_text = ac_val.strip()
            elif ac_val is not None:
                # sometimes AC is rich object; stringify safely
                ac_text = json.dumps(ac_val, ensure_ascii=False)
        except Exception:
            ac_text = ""

    return {
        "summary": summary,
        "description": description,
        "acceptance": ac_text,
    }


def build_story_generation_context(story_key: str, issue_fields: dict, story_details: dict) -> dict:
    """Create a compact Jira story context for faster testcase generation."""
    priority = ((issue_fields.get("priority") or {}).get("name") or "").strip()
    issue_type = ((issue_fields.get("issuetype") or {}).get("name") or "").strip()
    labels = issue_fields.get("labels") or []

    return {
        "story_key": story_key,
        "issue_type": issue_type,
        "priority": priority,
        "labels": labels,
        "summary": (story_details.get("summary") or "").strip(),
        "description": (story_details.get("description") or "").strip(),
        "acceptance_criteria": (story_details.get("acceptance") or "").strip(),
    }


def build_description(story_key: str, tc: dict) -> str:
    """
    XRAY Test Description must contain ONLY:
    'Verify the <Title of the test>'
    """
    title = (tc.get("Title") or "").strip()
    if title:
        return f"Verify the {title}"
    return f"Verify the scenario for {story_key}"


def build_test_description(story_key: str, story_details: dict, tc: dict) -> str:
    parts = []

    if story_details.get("summary"):
        parts.append(f"Story Summary:\n{story_details['summary']}\n")

    if story_details.get("description"):
        parts.append(f"Story Description:\n{story_details['description']}\n")

    if story_details.get("acceptance"):
        parts.append(f"Acceptance Criteria:\n{story_details['acceptance']}\n")

    # Test-specific section
    parts.append("Test Details:")
    parts.append(f"- Story Key: {story_key}")

    if tc.get("Preconditions"):
        parts.append(f"- Preconditions: {tc.get('Preconditions')}")
    if tc.get("TestData"):
        parts.append(f"- Test Data: {tc.get('TestData')}")
    if tc.get("Priority"):
        parts.append(f"- Priority: {tc.get('Priority')}")
    if tc.get("Type"):
        parts.append(f"- Type: {tc.get('Type')}")

    return "\n".join(parts).strip()


# -------------------------
# XRAY STEPS FORMAT
# -------------------------

def build_steps_payload(tc: dict) -> dict:
    steps = []
    for i, s in enumerate(tc.get("Steps", []), start=1):
        steps.append({
            "index": i,
            "fields": {
                "action": (s.get("Action") or "").strip(),
                "data": (tc.get("TestData") or "").strip(),
                "expected result": (s.get("Expected") or "").strip(),
            }
        })
    return {"steps": steps}


# -------------------------
# CREATE + UPDATE + LINK
# -------------------------

def create_jira_issue(jira_pat: str, fields: dict) -> str:
    r = jira_post(jira_pat, "/rest/api/2/issue", {"fields": fields})
    if r.status_code not in (200, 201):
        raise RuntimeError(f"Create failed ({r.status_code}): {r.text}")
    return r.json().get("key")


def update_issue_fields(jira_pat: str, issue_key: str, fields: dict):
    r = jira_put(jira_pat, f"/rest/api/2/issue/{issue_key}", {"fields": fields})
    if r.status_code not in (200, 204):
        raise RuntimeError(f"Update failed ({r.status_code}): {r.text}")


def link_test_to_story(jira_pat: str, test_key: str, story_key: str, link_type_name: str):
    payload = {
        "type": {"name": link_type_name},
        "inwardIssue": {"key": test_key},
        "outwardIssue": {"key": story_key}
    }
    r = jira_post(jira_pat, "/rest/api/2/issueLink", payload)

    # retry swapped direction
    if r.status_code in (400, 404):
        swapped = {
            "type": {"name": link_type_name},
            "inwardIssue": {"key": story_key},
            "outwardIssue": {"key": test_key}
        }
        r2 = jira_post(jira_pat, "/rest/api/2/issueLink", swapped)
        if r2.status_code not in (200, 201):
            raise RuntimeError(f"Linking failed: {r.text} | retry: {r2.text}")
        return

    if r.status_code not in (200, 201):
        raise RuntimeError(f"Linking failed ({r.status_code}): {r.text}")


def create_xray_test_per_case(
    jira_pat: str,
    story_key: str,
    project_key: str,
    issuetype_name: str,
    steps_field_id: str,
    test_type_field_id: str,
    test_type_payload: dict,
    story_details: dict,
    tc: dict,
) -> str:

    summary = (tc.get("Title") or f"Manual Test - {story_key}").strip()
    # include story key to avoid duplicates
    if story_key not in summary:
        summary = f"{story_key} - {summary}".strip()

    description = build_description(story_key, tc)

    base_fields = {
        "project": {"key": project_key},
        "issuetype": {"name": issuetype_name},
        "summary": summary,
        "description": description,
    }

    # Ensure required fields from sample are present
    copy_required_fields_from_sample(jira_pat, base_fields)

    # Test Type = Manual if field exists
    if test_type_field_id and test_type_payload:
        base_fields[test_type_field_id] = test_type_payload

    steps_payload = build_steps_payload(tc)

    # Attempt create with steps
    fields_with_steps = dict(base_fields)
    fields_with_steps[steps_field_id] = steps_payload

    try:
        test_key = create_jira_issue(jira_pat, fields_with_steps)
        return test_key
    except Exception as e:
        msg = str(e)
        # If steps field cannot be set on create screen, fallback create without steps and PUT afterwards
        if "cannot be set" in msg or steps_field_id in msg or "Manual Test Steps" in msg:
            test_key = create_jira_issue(jira_pat, base_fields)
            update_issue_fields(jira_pat, test_key, {steps_field_id: steps_payload})
            return test_key
        raise


# -------------------------
# EXCEL EXPORT
# -------------------------

def export_to_excel(story_key: str, testcases: list):
    wb = Workbook()
    ws = wb.active
    ws.title = "TestCases"

    # header format aligned with sample workbook
    ws.append(["Serial Number", "Title", "Preconditions", "TestData", "Priority", "Type", "Steps"])

    for serial_no, tc in enumerate(testcases, start=1):
        title = tc.get("Title", "")
        pre = tc.get("Preconditions", "")
        pri = tc.get("Priority", "")
        typ = tc.get("Type", "")
        td = tc.get("TestData", "")
        steps = tc.get("Steps", []) or []

        step_lines = []
        for i, s in enumerate(steps, start=1):
            action = (s.get("Action") or "").strip()
            expected = (s.get("Expected") or "").strip()
            if action and expected:
                step_lines.append(f"{i}. {action} -> {expected}")
            elif action:
                step_lines.append(f"{i}. {action}")
            elif expected:
                step_lines.append(f"{i}. Expected: {expected}")

        steps_text = "\n".join(step_lines)
        ws.append([serial_no, title, pre, td, pri, typ, steps_text])

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_file = f"Manual_TestCases_For_Story_{story_key}_{timestamp}.xlsx"
    wb.save(out_file)
    console(f"✅ Excel generated: {os.path.abspath(out_file)}")


def testcase_title_preview(testcases: list) -> str:
    """Build serial no + title list for display in popups/prompts."""
    lines = []
    for idx, tc in enumerate(testcases, start=1):
        title = (tc.get("Title") or "(no title)").strip()
        lines.append(f"{idx}. {title}")
    return "\n".join(lines)


def testcase_serial_title_priority_preview(testcases: list) -> str:
    """Build serial no + title + priority list for Option 1 confirmation."""
    lines = []
    for idx, tc in enumerate(testcases, start=1):
        title = (tc.get("Title") or "(no title)").strip()
        priority = (tc.get("Priority") or "").strip() or "(not set)"
        lines.append(f"{idx}. {title} | Priority: {priority}")
    return "\n".join(lines)


def get_custom_prompt_yes_no() -> str:
    """Show custom prompt dialog with Yes/No buttons and return prompt text if confirmed."""
    try:
        import tkinter as tk

        result = {"text": None}

        prompt_dialog = tk.Tk()
        prompt_dialog.title("My Prompt")
        prompt_dialog.geometry("1000x650")
        prompt_dialog.attributes("-topmost", True)
        prompt_dialog.after(200, lambda: prompt_dialog.attributes("-topmost", False))

        prompt_label = tk.Label(
            prompt_dialog,
            text="Enter customized prompt to generate test cases:",
            anchor="w",
            justify="left"
        )
        prompt_label.pack(fill="x", padx=12, pady=(12, 6))

        editor_frame = tk.Frame(prompt_dialog)
        editor_frame.pack(fill="both", expand=True, padx=12, pady=6)

        prompt_text = tk.Text(editor_frame, wrap="word", height=24)
        prompt_text.pack(side="left", fill="both", expand=True)

        scroll = tk.Scrollbar(editor_frame, command=prompt_text.yview)
        scroll.pack(side="right", fill="y")
        prompt_text.configure(yscrollcommand=scroll.set)

        button_frame = tk.Frame(prompt_dialog)
        button_frame.pack(fill="x", padx=12, pady=(6, 12))

        def submit_prompt_yes():
            result["text"] = prompt_text.get("1.0", "end-1c")
            prompt_dialog.quit()
            prompt_dialog.destroy()

        def cancel_prompt_no():
            result["text"] = None
            prompt_dialog.quit()
            prompt_dialog.destroy()

        tk.Button(button_frame, text="Yes", width=12, command=submit_prompt_yes).pack(side="left", padx=(0, 8))
        tk.Button(button_frame, text="No", width=12, command=cancel_prompt_no).pack(side="left")

        prompt_dialog.protocol("WM_DELETE_WINDOW", cancel_prompt_no)
        prompt_dialog.focus_force()
        prompt_dialog.mainloop()

        return (result["text"] or "").strip()
    except Exception:
        custom_prompt = input("\nEnter customized prompt (leave blank to cancel): ").strip()
        return custom_prompt


def confirm_excel_generation_with_prompt_option(testcases: list):
    """Return tuple (action, custom_prompt). action: yes|no|myprompt."""
    preview = testcase_serial_title_priority_preview(testcases)

    try:
        import tkinter as tk

        decision = {"value": "no"}

        dialog = tk.Tk()
        dialog.title("Confirm Excel Generation")
        dialog.geometry("950x620")
        dialog.attributes("-topmost", True)
        dialog.after(200, lambda: dialog.attributes("-topmost", False))

        label = tk.Label(
            dialog,
            text="Generated test cases (Serial No, Title, Priority):",
            anchor="w",
            justify="left"
        )
        label.pack(fill="x", padx=12, pady=(12, 6))

        text_box = tk.Text(dialog, wrap="word", height=24)
        text_box.insert("1.0", preview)
        text_box.configure(state="disabled")
        text_box.pack(fill="both", expand=True, padx=12, pady=6)

        button_row = tk.Frame(dialog)
        button_row.pack(fill="x", padx=12, pady=(6, 12))

        def pick_yes():
            decision["value"] = "yes"
            dialog.quit()
            dialog.destroy()

        def pick_no():
            decision["value"] = "no"
            dialog.quit()
            dialog.destroy()

        def pick_my_prompt():
            decision["value"] = "myprompt"
            dialog.quit()
            dialog.destroy()

        tk.Button(button_row, text="Yes", width=12, command=pick_yes).pack(side="left", padx=(0, 8))
        tk.Button(button_row, text="No", width=12, command=pick_no).pack(side="left", padx=(0, 8))
        tk.Button(button_row, text="My Prompt", width=12, command=pick_my_prompt).pack(side="left")

        dialog.protocol("WM_DELETE_WINDOW", pick_no)
        dialog.focus_force()
        dialog.mainloop()

        action = decision["value"]
        if action == "myprompt":
            custom_prompt = get_custom_prompt_yes_no()
            if not custom_prompt:
                return "no", None
            return "myprompt", custom_prompt
        return action, None
    except Exception as e:
        console(f"⚠️ Popup not available, switching to terminal confirmation: {e}")
        print("\nGenerated test cases (Serial No, Title, Priority):")
        print(preview)
        answer = input("\nGenerate and download Excel? (Yes/No/My Prompt): ").strip().lower()
        if answer in ("y", "yes"):
            return "yes", None
        if answer in ("m", "myprompt", "my prompt"):
            custom_prompt = input("\nEnter customized prompt: ").strip()
            if not custom_prompt:
                return "no", None
            return "myprompt", custom_prompt
        return "no", None


def confirm_excel_generation_yes_no(testcases: list) -> bool:
    """Show regenerated testcases and confirm Yes/No for Excel generation."""
    preview = testcase_serial_title_priority_preview(testcases)

    try:
        import tkinter as tk
        from tkinter import messagebox

        root = tk.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        root.update()
        confirmed = messagebox.askyesno(
            "Confirm Excel Generation",
            "Generated test cases (Serial No, Title, Priority):\n\n"
            f"{preview}\n\n"
            "Do you want to generate and download Excel?",
            parent=root
        )
        root.destroy()
        return confirmed
    except Exception as e:
        console(f"⚠️ Popup not available, switching to terminal confirmation: {e}")
        print("\nGenerated test cases (Serial No, Title, Priority):")
        print(preview)
        answer = input("\nGenerate and download Excel? (Yes/No): ").strip().lower()
        return answer in ("y", "yes")


def confirm_jira_creation_with_prompt_option(testcases: list):
    """Return tuple (action, custom_prompt). action: yes|no|myprompt"""
    preview = testcase_title_preview(testcases)

    # Try GUI popup first; fallback to console prompt if unavailable.
    try:
        import tkinter as tk

        decision = {"value": "no"}

        dialog = tk.Tk()
        dialog.title("Confirm Jira Test Creation")
        dialog.geometry("900x600")
        dialog.attributes("-topmost", True)
        dialog.after(200, lambda: dialog.attributes("-topmost", False))

        label = tk.Label(dialog, text="Generated test cases (Serial No and Title):", anchor="w", justify="left")
        label.pack(fill="x", padx=12, pady=(12, 6))

        text_box = tk.Text(dialog, wrap="word", height=24)
        text_box.insert("1.0", preview)
        text_box.configure(state="disabled")
        text_box.pack(fill="both", expand=True, padx=12, pady=6)

        button_row = tk.Frame(dialog)
        button_row.pack(fill="x", padx=12, pady=(6, 12))

        def pick_yes():
            decision["value"] = "yes"
            dialog.quit()
            dialog.destroy()

        def pick_no():
            decision["value"] = "no"
            dialog.quit()
            dialog.destroy()

        def pick_my_prompt():
            decision["value"] = "myprompt"
            dialog.quit()
            dialog.destroy()

        tk.Button(button_row, text="Yes", width=12, command=pick_yes).pack(side="left", padx=(0, 8))
        tk.Button(button_row, text="No", width=12, command=pick_no).pack(side="left", padx=(0, 8))
        tk.Button(button_row, text="My Prompt", width=12, command=pick_my_prompt).pack(side="left")

        dialog.protocol("WM_DELETE_WINDOW", pick_no)
        dialog.focus_force()
        dialog.mainloop()

        action = decision["value"]
        custom_prompt = None
        if action == "myprompt":
            prompt_value = {"text": None}

            prompt_dialog = tk.Tk()
            prompt_dialog.title("My Prompt")
            prompt_dialog.geometry("1000x650")
            prompt_dialog.attributes("-topmost", True)
            prompt_dialog.after(200, lambda: prompt_dialog.attributes("-topmost", False))

            prompt_label = tk.Label(
                prompt_dialog,
                text="Enter customized prompt to generate test cases:",
                anchor="w",
                justify="left"
            )
            prompt_label.pack(fill="x", padx=12, pady=(12, 6))

            editor_frame = tk.Frame(prompt_dialog)
            editor_frame.pack(fill="both", expand=True, padx=12, pady=6)

            prompt_text = tk.Text(editor_frame, wrap="word", height=24)
            prompt_text.pack(side="left", fill="both", expand=True)

            scroll = tk.Scrollbar(editor_frame, command=prompt_text.yview)
            scroll.pack(side="right", fill="y")
            prompt_text.configure(yscrollcommand=scroll.set)

            button_frame = tk.Frame(prompt_dialog)
            button_frame.pack(fill="x", padx=12, pady=(6, 12))

            def submit_prompt():
                prompt_value["text"] = prompt_text.get("1.0", "end-1c")
                prompt_dialog.quit()
                prompt_dialog.destroy()

            def cancel_prompt():
                prompt_value["text"] = None
                prompt_dialog.quit()
                prompt_dialog.destroy()

            tk.Button(button_frame, text="OK", width=12, command=submit_prompt).pack(side="left", padx=(0, 8))
            tk.Button(button_frame, text="Cancel", width=12, command=cancel_prompt).pack(side="left")

            prompt_dialog.protocol("WM_DELETE_WINDOW", cancel_prompt)
            prompt_dialog.focus_force()
            prompt_dialog.mainloop()

            custom_prompt = prompt_value["text"]

        if action == "myprompt":
            custom_prompt = (custom_prompt or "").strip()
            if not custom_prompt:
                return "no", None
            return "myprompt", custom_prompt
        return action, None
    except Exception as e:
        console(f"⚠️ Popup not available, switching to terminal confirmation: {e}")
        print("\nGenerated test cases (Serial No and Title):")
        print(preview)
        answer = input("\nCreate these tests in Jira? (Yes/No/My Prompt): ").strip().lower()
        if answer in ("y", "yes"):
            return "yes", None
        if answer in ("m", "myprompt", "my prompt"):
            custom_prompt = input("\nEnter customized prompt: ").strip()
            if not custom_prompt:
                return "no", None
            return "myprompt", custom_prompt
        return "no", None


def confirm_jira_creation_yes_no(testcases: list) -> bool:
    """Show serial no + title list and confirm Yes/No for Jira creation."""
    preview = testcase_title_preview(testcases)

    try:
        import tkinter as tk
        from tkinter import messagebox

        root = tk.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        root.update()
        confirmed = messagebox.askyesno(
            "Confirm Jira Test Creation",
            "Generated test cases (Serial No and Title):\n\n"
            f"{preview}\n\n"
            "Do you want to create these tests in Jira?",
            parent=root
        )
        root.destroy()
        return confirmed
    except Exception as e:
        console(f"⚠️ Popup not available, switching to terminal confirmation: {e}")
        print("\nGenerated test cases (Serial No and Title):")
        print(preview)
        answer = input("\nCreate these tests in Jira? (Yes/No): ").strip().lower()
        return answer in ("y", "yes")


def generate_testcases(prompt: str, story_context: dict | None = None) -> list:
    console("Running Copilot to Generate Test Cases...")
    prepared_prompt = build_generation_prompt(prompt, story_context)
    result = run("copilot --silent", input_text=prepared_prompt)

    if result.returncode != 0:
        raise RuntimeError(f"Copilot command failed: {result.stderr}")

    copilot_output = (result.stdout or "").strip()
    if not copilot_output:
        raise RuntimeError("Copilot returned empty output")

    with open(COPILOT_RAW_FILE, "w", encoding="utf-8") as f:
        f.write(copilot_output)

    return enforce_start_from_beginning(extract_json_array(copilot_output))


# -------------------------
# MAIN
# -------------------------

def main():
    jira_pat = os.environ.get("JIRA_PAT")
    if not jira_pat:
        print("❌ JIRA_PAT environment variable not set")
        wait_for_any_key("\nPress any key to exit...")
        sys.exit(1)

    story_key = input("Enter Jira User Story key (e.g. ILREP-869): ").strip()

    print("\nChoose option:")
    print("Click 1 - To Generate Manual Test Cases in Excel")
    print("Click 2 - To Create Manual Test and Add Xray Tests to Jira Story")
    choice = input("\nPlease enter 1 or 2: ").strip()


    if choice not in ("1", "2"):
        print("❌ Invalid option")
        wait_for_any_key("\nPress any key to exit...")
        sys.exit(1)

    console("Fetching Jira Story...")
    issue_json = jira_get(
        jira_pat,
        f"/rest/api/2/issue/{story_key}",
        params={"fields": "summary,description,labels,priority,issuetype"}
    )

    story_details = fetch_story_details(jira_pat, story_key)
    story_context = build_story_generation_context(story_key, issue_json.get("fields", {}), story_details)

    with open(ISSUE_JSON_FILE, "w", encoding="utf-8") as f:
        json.dump(story_context, f, ensure_ascii=False, indent=2)

    console(f"✅ {ISSUE_JSON_FILE} generated")

    # Prompt Copilot
    prompt = """
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
- No markdown
- No explanations
- Output JSON ONLY
""".strip()

    try:
        testcases = generate_testcases(prompt, story_context)
    except Exception as e:
        print("❌ Failed to parse Copilot JSON")
        print(str(e))
        print(f"\nRaw output saved to {COPILOT_RAW_FILE}")
        wait_for_any_key("\nPress any key to exit...")
        sys.exit(1)

    console(f"✅ Parsed {len(testcases)} test cases")

    if choice == "1":
        action, custom_prompt = confirm_excel_generation_with_prompt_option(testcases)

        if action == "myprompt" and custom_prompt:
            try:
                testcases = generate_testcases(custom_prompt, story_context)
            except Exception as e:
                print("❌ Failed to generate test cases from custom prompt")
                print(str(e))
                print(f"\nRaw output saved to {COPILOT_RAW_FILE}")
                wait_for_any_key("\nPress any key to exit...")
                sys.exit(1)

            console(f"✅ Parsed {len(testcases)} test cases from custom prompt")

            if not confirm_excel_generation_yes_no(testcases):
                console("⚠️ Excel generation cancelled by user.")
                wait_for_any_key("\nPress any key to close...")
                return
        elif action != "yes":
            console("⚠️ Excel generation cancelled by user.")
            wait_for_any_key("\nPress any key to close...")
            return

        export_to_excel(story_key, testcases)
        wait_for_any_key("\nPress any key to close...")
        return

    # Option 2: confirm and create Jira tests first, then export Excel at the end

    action, custom_prompt = confirm_jira_creation_with_prompt_option(testcases)
    if action == "myprompt" and custom_prompt:
        try:
            testcases = generate_testcases(custom_prompt, story_context)
        except Exception as e:
            print("❌ Failed to generate test cases from custom prompt")
            print(str(e))
            print(f"\nRaw output saved to {COPILOT_RAW_FILE}")
            wait_for_any_key("\nPress any key to exit...")
            sys.exit(1)

        console(f"✅ Parsed {len(testcases)} test cases from custom prompt")

        if not confirm_jira_creation_yes_no(testcases):
            console("⚠️ Jira test creation cancelled by user.")
            wait_for_any_key("\nPress any key to close...")
            return
    elif action != "yes":
        console("⚠️ Jira test creation cancelled by user.")
        wait_for_any_key("\nPress any key to close...")
        return

    console("Creating Xray Tests (one per generated testcase) + linking to the Story...")

    save_meta_debug(jira_pat)

    issuetype_name = detect_test_issuetype_name(jira_pat)
    steps_field_id = detect_steps_field_id(jira_pat)
    test_type_field_id, test_type_payload = detect_test_type_field(jira_pat)

    console(f"✅ Detected Xray Test issue type: {issuetype_name} (from {XRAY_SAMPLE_TEST_KEY})")
    console(f"✅ Using Manual Test Steps field: {steps_field_id} (from {XRAY_SAMPLE_TEST_KEY})")
    if test_type_field_id:
        console(f"✅ Using Test Type field: {test_type_field_id} = {test_type_payload.get('value', test_type_payload)}")
    else:
        console("⚠️ Could not detect Test Type field; continuing without setting Test Type")

    project_key = story_key.split("-")[0]

    created = []
    for idx, tc in enumerate(testcases, start=1):
        title = tc.get("Title", "(no title)")
        console(f"[{idx}/{len(testcases)}] Creating Test: {title}")

        test_key = create_xray_test_per_case(
            jira_pat=jira_pat,
            story_key=story_key,
            project_key=project_key,
            issuetype_name=issuetype_name,
            steps_field_id=steps_field_id,
            test_type_field_id=test_type_field_id,
            test_type_payload=test_type_payload,
            story_details=story_details,
            tc=tc,
        )

        console(f"✅ Created Test: {test_key}")

        link_test_to_story(jira_pat, test_key, story_key, JIRA_LINK_TYPE_NAME)
        console(f"🔗 Linked {test_key} to {story_key}")

        created.append(test_key)

    with open("created_tests.json", "w", encoding="utf-8") as f:
        json.dump({"story": story_key, "tests": created}, f, indent=2)

    with open("created_tests.txt", "w", encoding="utf-8") as f:
        f.write("\n".join(created))

    export_to_excel(story_key, testcases)

    console(f"✅ Created & linked {len(created)} Tests to {story_key}")
    console("📄 created_tests.json / created_tests.txt saved in the same folder")

    wait_for_any_key("\nPress any key to close...")


if __name__ == "__main__":
    try:
        main()
    except Exception:
        import traceback
        traceback.print_exc()
        wait_for_any_key("\nPress any key to exit...")
