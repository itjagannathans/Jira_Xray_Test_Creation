
import subprocess
import json
import os
import sys
import requests
import logging
from openpyxl import Workbook

# =========================
# CONFIG
# =========================
JIRA_DOMAIN = "https://jira.g2-networks.net"

# Existing Xray Test issue key in the SAME project (used for auto-detecting issue type + fields)
XRAY_SAMPLE_TEST_KEY = os.environ.get("XRAY_SAMPLE_TEST_KEY", "ILREP-880").strip()

# Fallbacks (used only if auto-detection fails)
XRAY_STEP_FIELD_FALLBACK = os.environ.get("XRAY_STEP_FIELD_FALLBACK", "customfield_10004").strip()
JIRA_LINK_TYPE_DEFAULT = os.environ.get("JIRA_LINK_TYPE_NAME", "Tests").strip()

# Output files
LOG_FILE = "xray_test_generator.log"
COPILOT_RAW_FILE = "copilot_raw_output.txt"
ISSUE_JSON_FILE = "issue.json"
META_DEBUG_FILE = "xray_meta_debug.json"

# =========================
# LOGGING
# =========================
logging.basicConfig(
    filename=LOG_FILE,
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s"
)

def console(msg: str):
    print(msg, flush=True)
    logging.info(msg)

def jira_headers(jira_pat: str, include_content_type: bool = True):
    h = {
        "Authorization": f"Bearer {jira_pat}",
        "Accept": "application/json",
    }
    if include_content_type:
        h["Content-Type"] = "application/json"
    return h


# =========================
# COPILOT
# =========================
def run(cmd, input_text=None):
    return subprocess.run(
        cmd,
        input=input_text,
        text=True,
        shell=True,
        capture_output=True
    )

def extract_json_array(text: str):
    """Extract first JSON array from Copilot output."""
    start = text.find("[")
    end = text.rfind("]")
    if start == -1 or end == -1:
        raise ValueError("No JSON array found in Copilot output")
    return json.loads(text[start:end + 1])


# =========================
# AUTO-DETECT (no /createmeta)
# =========================
def get_issue(jira_pat: str, issue_key: str, fields: str):
    url = f"{JIRA_DOMAIN}/rest/api/2/issue/{issue_key}?fields={fields}"
    r = requests.get(url, headers=jira_headers(jira_pat, include_content_type=False), timeout=60)
    r.raise_for_status()
    return r.json()

def get_editmeta(jira_pat: str, issue_key: str):
    url = f"{JIRA_DOMAIN}/rest/api/2/issue/{issue_key}/editmeta"
    r = requests.get(url, headers=jira_headers(jira_pat, include_content_type=False), timeout=60)
    r.raise_for_status()
    return r.json()

def detect_test_issuetype_name(jira_pat: str) -> str:
    data = get_issue(jira_pat, XRAY_SAMPLE_TEST_KEY, "issuetype")
    name = (((data.get("fields") or {}).get("issuetype") or {}).get("name"))
    if not name:
        raise RuntimeError(f"Unable to detect issuetype name from {XRAY_SAMPLE_TEST_KEY}")
    return name

def _find_field_by_name(fields_meta: dict, target_names_lower: list):
    for fid, meta in fields_meta.items():
        nm = (meta.get("name") or "").strip().lower()
        if nm in target_names_lower:
            return fid, meta
    return None, None

def detect_steps_field_id(jira_pat: str) -> str:
    meta = get_editmeta(jira_pat, XRAY_SAMPLE_TEST_KEY)
    fields_meta = meta.get("fields", {})

    # Most accurate
    fid, _ = _find_field_by_name(fields_meta, ["manual test steps"])
    if fid:
        return fid

    # Heuristic
    for k, v in fields_meta.items():
        nm = (v.get("name") or "").lower()
        if "manual" in nm and "step" in nm:
            return k
        if "test" in nm and "step" in nm:
            return k

    return XRAY_STEP_FIELD_FALLBACK

def detect_test_type_field(jira_pat: str):
    """Returns (field_id, manual_value_payload) or (None, None) if not found."""
    meta = get_editmeta(jira_pat, XRAY_SAMPLE_TEST_KEY)
    fields_meta = meta.get("fields", {})

    candidate_id = None
    candidate_meta = None

    for fid, m in fields_meta.items():
        nm = (m.get("name") or "").lower()
        if nm == "test type":
            candidate_id, candidate_meta = fid, m
            break

    if not candidate_id:
        for fid, m in fields_meta.items():
            nm = (m.get("name") or "").lower()
            if "test type" in nm:
                candidate_id, candidate_meta = fid, m
                break

    if not candidate_id:
        return None, None

    allowed = candidate_meta.get("allowedValues") or []
    manual_option = None
    for opt in allowed:
        val = (opt.get("value") or opt.get("name") or "")
        if isinstance(val, str) and val.strip().lower() == "manual":
            manual_option = opt
            break

    if manual_option:
        if "value" in manual_option:
            return candidate_id, {"value": manual_option["value"]}
        if "name" in manual_option:
            return candidate_id, {"value": manual_option["name"]}

    return candidate_id, {"value": "Manual"}

def save_meta_debug(jira_pat: str):
    try:
        meta = get_editmeta(jira_pat, XRAY_SAMPLE_TEST_KEY)
        with open(META_DEBUG_FILE, "w", encoding="utf-8") as f:
            json.dump(meta, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logging.warning(f"Failed saving meta debug: {e}")


# =========================
# XRAY STEPS FORMAT
# =========================
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

def build_description(story_key: str, tc: dict) -> str:
    parts = [
        f"Story: {story_key}",
        f"Priority: {tc.get('Priority', '')}",
        f"Type: {tc.get('Type', '')}",
        f"Preconditions: {tc.get('Preconditions', '')}",
        f"TestData: {tc.get('TestData', '')}",
    ]
    return "\n".join(parts).strip()


# =========================
# CREATE + UPDATE
# =========================
def create_issue(jira_pat: str, fields: dict) -> str:
    r = requests.post(
        f"{JIRA_DOMAIN}/rest/api/2/issue",
        headers=jira_headers(jira_pat),
        json={"fields": fields},
        timeout=60
    )
    if r.status_code not in (200, 201):
        raise RuntimeError(f"Create issue failed ({r.status_code}): {r.text}")
    return r.json().get("key")

def update_issue_fields(jira_pat: str, issue_key: str, fields: dict):
    r = requests.put(
        f"{JIRA_DOMAIN}/rest/api/2/issue/{issue_key}",
        headers=jira_headers(jira_pat),
        json={"fields": fields},
        timeout=60
    )
    if r.status_code not in (200, 204):
        raise RuntimeError(f"Update issue failed ({r.status_code}): {r.text}")

def create_xray_test_per_case(jira_pat: str, story_key: str, tc: dict,
                             project_key: str, issuetype_name: str,
                             steps_field_id: str,
                             test_type_field_id: str = None,
                             test_type_payload: dict = None) -> str:

    summary = (tc.get("Title") or f"Manual Test - {story_key}").strip()
    description = build_description(story_key, tc)
    steps_payload = build_steps_payload(tc)

    base_fields = {
        "project": {"key": project_key},
        "issuetype": {"name": issuetype_name},
        "summary": summary,              # ✅ Header
        "description": description,      # ✅ Description
    }

    # ✅ Ensure Manual Test Type if available
    if test_type_field_id and test_type_payload:
        base_fields[test_type_field_id] = test_type_payload

    # Attempt 1: create with steps
    fields_with_steps = dict(base_fields)
    fields_with_steps[steps_field_id] = steps_payload

    try:
        test_key = create_issue(jira_pat, fields_with_steps)
        return test_key
    except Exception as e:
        msg = str(e)
        if "cannot be set" not in msg and steps_field_id not in msg:
            raise

        console(f"⚠️ {steps_field_id} rejected on CREATE screen. Falling back to create-then-update...")

        test_key = create_issue(jira_pat, base_fields)
        update_issue_fields(jira_pat, test_key, {steps_field_id: steps_payload})
        return test_key


def link_test_to_story(jira_pat: str, test_key: str, story_key: str, link_type_name: str):
    payload = {
        "type": {"name": link_type_name},
        "inwardIssue": {"key": test_key},
        "outwardIssue": {"key": story_key}
    }
    r = requests.post(
        f"{JIRA_DOMAIN}/rest/api/2/issueLink",
        headers=jira_headers(jira_pat),
        json=payload,
        timeout=60
    )

    if r.status_code in (400, 404):
        swapped = {
            "type": {"name": link_type_name},
            "inwardIssue": {"key": story_key},
            "outwardIssue": {"key": test_key}
        }
        r2 = requests.post(
            f"{JIRA_DOMAIN}/rest/api/2/issueLink",
            headers=jira_headers(jira_pat),
            json=swapped,
            timeout=60
        )
        if r2.status_code not in (200, 201):
            raise RuntimeError(f"Linking failed: {r.text} | retry: {r2.text}")
        return

    if r.status_code not in (200, 201):
        raise RuntimeError(f"Linking failed ({r.status_code}): {r.text}")


# =========================
# EXCEL
# =========================
def export_to_excel(story_key: str, testcases: list):
    wb = Workbook()
    ws = wb.active
    ws.title = "TestCases"
    ws.append(["Title", "Preconditions", "TestData", "Priority", "Type", "Steps"])

    for tc in testcases:
        steps_text = ""
        for s in tc.get("Steps", []):
            steps_text += f"{s.get('Step')}. {s.get('Action')} -> {s.get('Expected')}\n"

        ws.append([
            tc.get("Title", ""),
            tc.get("Preconditions", ""),
            tc.get("TestData", ""),
            tc.get("Priority", ""),
            tc.get("Type", ""),
            steps_text.strip()
        ])

    out_file = f"Manual_TestCases_For_Story_{story_key}.xlsx"
    wb.save(out_file)
    console(f"✅ Excel generated: {os.path.abspath(out_file)}")


# =========================
# MAIN
# =========================
def main():
    jira_pat = os.environ.get("JIRA_PAT")
    if not jira_pat:
        print("❌ JIRA_PAT environment variable not set")
        input("\nPress Enter to exit...")
        sys.exit(1)

    story_key = input("Enter Jira User Story key (e.g. NWUW-329): ").strip()

    print("\nChoose option:")
    print("Click 1 - if you want to Generate Manual Test Cases For given Jira Story in Excel")
    print("Click 2 - if you want to Create Xray Test for each generated Test Case from Jira Story")
    choice = input("\nPlease enter 1 or 2: ").strip()

    if choice not in ("1", "2"):
        print("❌ Invalid option")
        input("\nPress Enter to exit...")
        sys.exit(1)

    # Fetch Jira issue JSON
    console("Fetching Jira issue...")
    r = requests.get(
        f"{JIRA_DOMAIN}/rest/api/2/issue/{story_key}",
        headers=jira_headers(jira_pat, include_content_type=False),
        timeout=60
    )
    if r.status_code != 200:
        print(f"❌ Failed to fetch Jira issue ({r.status_code})")
        print(r.text[:1000])
        input("\nPress Enter to exit...")
        sys.exit(1)

    issue_json = r.json()
    with open(ISSUE_JSON_FILE, "w", encoding="utf-8") as f:
        json.dump(issue_json, f, ensure_ascii=False, indent=2)
    console(f"✅ {ISSUE_JSON_FILE} generated")

    # Copilot prompt
    prompt = """
Read @issue.json and generate MANUAL test cases.

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
- Minimum 5 test case
- Include negative & boundary cases
- No markdown
- No explanations
- Output JSON ONLY
""".strip()

    console("Running Copilot...")
    result = run("copilot --silent", input_text=prompt)

    if result.returncode != 0:
        print("❌ Copilot command failed")
        print(result.stderr)
        input("\nPress Enter to exit...")
        sys.exit(1)

    copilot_output = (result.stdout or "").strip()
    if not copilot_output:
        print("❌ Copilot returned empty output")
        input("\nPress Enter to exit...")
        sys.exit(1)

    with open(COPILOT_RAW_FILE, "w", encoding="utf-8") as f:
        f.write(copilot_output)

    try:
        testcases = extract_json_array(copilot_output)
    except Exception as e:
        print("❌ Failed to parse Copilot JSON")
        print(str(e))
        print(f"\nRaw output saved to {COPILOT_RAW_FILE}")
        input("\nPress Enter to exit...")
        sys.exit(1)

    console(f"✅ Parsed {len(testcases)} test cases")

    if choice == "1":
        export_to_excel(story_key, testcases)
        input("\nPress Enter to close...")
        return

    # Option 2
    console("Creating Xray Tests (one per generated testcase) + linking to the Story...")

    save_meta_debug(jira_pat)

    issuetype_name = detect_test_issuetype_name(jira_pat)
    steps_field_id = detect_steps_field_id(jira_pat)
    test_type_field_id, test_type_payload = detect_test_type_field(jira_pat)

    console(f"✅ Detected Xray Test issue type: {issuetype_name} (from {XRAY_SAMPLE_TEST_KEY})")
    console(f"✅ Using Manual Test Steps field: {steps_field_id} (from {XRAY_SAMPLE_TEST_KEY})")
    if test_type_field_id:
        console(f"✅ Using Test Type field: {test_type_field_id} = {test_type_payload.get('value')}")
    else:
        console("⚠️ Could not detect Test Type field; continuing without setting Test Type")

    project_key = story_key.split("-")[0]
    link_type_name = JIRA_LINK_TYPE_DEFAULT

    created = []
    for idx, tc in enumerate(testcases, start=1):
        title = tc.get("Title", "(no title)")
        console(f"[{idx}/{len(testcases)}] Creating Test: {title}")

        test_key = create_xray_test_per_case(
            jira_pat=jira_pat,
            story_key=story_key,
            tc=tc,
            project_key=project_key,
            issuetype_name=issuetype_name,
            steps_field_id=steps_field_id,
            test_type_field_id=test_type_field_id,
            test_type_payload=test_type_payload
        )
        console(f"✅ Created Test: {test_key}")

        link_test_to_story(jira_pat, test_key, story_key, link_type_name)
        console(f"🔗 Linked {test_key} to {story_key}")

        created.append(test_key)

    with open("created_tests.json", "w", encoding="utf-8") as f:
        json.dump({"story": story_key, "tests": created}, f, indent=2)

    with open("created_tests.txt", "w", encoding="utf-8") as f:
        f.write("\n".join(created))

    console(f"✅ Created & linked {len(created)} Tests to {story_key}")
    console("📄 created_tests.json / created_tests.txt saved in the same folder")

    input("\nPress Enter to close...")


if __name__ == "__main__":
    try:
        main()
    except Exception:
        import traceback
        traceback.print_exc()
        input("\nPress Enter to exit...")
 
