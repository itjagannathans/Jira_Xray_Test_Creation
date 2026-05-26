"""In-process Xray test generator (no Flask required).

Calls app.xray_service directly. Reads JIRA_PAT from environment.

Examples:
    python scripts/generate_xray_testcases.py --jira-issue-key ILREP-981 --jira-auth-type pat --use-copilot --action preview
    python scripts/generate_xray_testcases.py --jira-issue-key ILREP-981 --jira-auth-type pat --use-copilot --action excel --confirm-before-export
    python scripts/generate_xray_testcases.py --jira-issue-key ILREP-981 --jira-auth-type pat --use-copilot --action jira --confirm-before-export

When --confirm-before-export is set, a native popup (tkinter) shows a summary of
the generated testcases and asks Yes/No before performing the export.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

# Ensure project root is importable when run from anywhere.
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app import xray_service as xs  # noqa: E402


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Generate Xray manual testcases in-process.")
    p.add_argument("--jira-issue-key", required=True, help="Jira story key, e.g. ILREP-981")
    p.add_argument("--jira-auth-type", default="pat", choices=["pat"],
                   help="Authentication type (only 'pat' is supported)")
    p.add_argument("--jira-pat", default=os.environ.get("JIRA_PAT", ""),
                   help="Jira PAT (or set JIRA_PAT env var)")
    p.add_argument("--use-copilot", action="store_true",
                   help="Use Copilot for generation (always on; flag retained for compatibility)")
    p.add_argument("--action", default="preview", choices=["preview", "excel", "jira"],
                   help="What to do after generation (default: preview)")
    p.add_argument("--confirm-before-export", action="store_true",
                   help="Show a popup with the preview and require Yes before exporting")
    p.add_argument("--custom-prompt", default="",
                   help="Optional override prompt for test generation")
    p.add_argument("--out-dir", default=str(ROOT / "exports"),
                   help="Excel output directory (default: ./exports)")
    return p


def format_summary(story_key: str, testcases: list) -> str:
    lines = [f"Story: {story_key}", f"Generated {len(testcases)} testcase(s):", ""]
    for i, tc in enumerate(testcases, 1):
        title = tc.get("Title", "(no title)")
        steps = tc.get("Steps") or []
        lines.append(f"  {i:>2}. {title}  ({len(steps)} steps)")
    return "\n".join(lines)


def confirm_popup(title: str, message: str) -> bool:
    """Show a Yes/No popup. Falls back to console prompt if no GUI available."""
    try:
        import tkinter as tk
        from tkinter import messagebox
        root = tk.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        try:
            answer = messagebox.askyesno(title=title, message=message)
        finally:
            root.destroy()
        return bool(answer)
    except Exception as exc:
        print(f"[popup unavailable: {exc}] Falling back to console prompt.", file=sys.stderr)
        try:
            return input(f"\n{message}\n\nProceed? (y/N): ").strip().lower() in ("y", "yes")
        except EOFError:
            return False


def do_excel(story_key: str, testcases: list, out_dir: str) -> str:
    Path(out_dir).mkdir(parents=True, exist_ok=True)
    return xs.export_testcases_to_excel(story_key, testcases, out_dir)


def do_jira(jira_pat: str, story_key: str, testcases: list) -> dict:
    issuetype_name = xs.detect_test_issuetype_name(jira_pat)
    steps_field_id = xs.detect_steps_field_id(jira_pat)
    tt_field_id, tt_payload = xs.detect_test_type_field(jira_pat)
    project_key = story_key.split("-")[0]

    created, errors = [], []
    for tc in testcases:
        try:
            test_key = xs.create_xray_test(
                jira_pat,
                story_key=story_key,
                project_key=project_key,
                issuetype_name=issuetype_name,
                steps_field_id=steps_field_id,
                test_type_field_id=tt_field_id,
                test_type_payload=tt_payload,
                tc=tc,
            )
            xs.link_test_to_story(jira_pat, test_key, story_key)
            created.append(test_key)
        except Exception as e:
            errors.append(f"{tc.get('Title', '?')}: {e}")
    return {"created": created, "errors": errors, "created_count": len(created)}


def main() -> int:
    args = build_parser().parse_args()

    if args.jira_auth_type != "pat":
        print("ERROR: only --jira-auth-type pat is supported.", file=sys.stderr)
        return 2
    if not args.jira_pat:
        print("ERROR: Jira PAT not found. Set JIRA_PAT env var or pass --jira-pat.",
              file=sys.stderr)
        return 2

    print(f"Fetching Jira story context for {args.jira_issue_key}...")
    try:
        story_context = xs.build_story_context(args.jira_pat, args.jira_issue_key)
    except Exception as e:
        print(f"ERROR: Failed to fetch story: {e}", file=sys.stderr)
        return 1

    prompt = args.custom_prompt or xs.DEFAULT_PROMPT
    print("Generating testcases via Copilot...")
    try:
        testcases = xs.generate_testcases(prompt, story_context)
    except Exception as e:
        print(f"ERROR: Generation failed: {e}", file=sys.stderr)
        return 1

    summary = format_summary(args.jira_issue_key, testcases)
    print("\n" + summary)

    if args.action == "preview":
        print("\n--- Full preview (JSON) ---")
        print(json.dumps(testcases, indent=2, ensure_ascii=False))
        return 0

    if args.confirm_before_export:
        msg = f"{summary}\n\nProceed with --action {args.action}?"
        if not confirm_popup("Confirm Xray Export", msg):
            print("Aborted by user before export.")
            return 0

    if args.action == "excel":
        try:
            out_file = do_excel(args.jira_issue_key, testcases, args.out_dir)
        except Exception as e:
            print(f"ERROR: Excel export failed: {e}", file=sys.stderr)
            return 1
        print(f"\nExcel written: {out_file}")
        return 0

    # action == "jira"
    try:
        result = do_jira(args.jira_pat, args.jira_issue_key, testcases)
    except Exception as e:
        print(f"ERROR: Jira creation failed: {e}", file=sys.stderr)
        return 1
    print(f"\nCreated {result['created_count']} test(s): {', '.join(result['created']) or '(none)'}")
    if result["errors"]:
        print("Errors:")
        for err in result["errors"]:
            print(f"  - {err}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
