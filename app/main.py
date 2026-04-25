"""Main user-facing blueprint: landing page, generation, profile."""
from __future__ import annotations

import io
import json
import logging
import os
import traceback
from datetime import datetime

from flask import (Blueprint, current_app, flash, redirect, render_template,
                   request, send_file, session, url_for)
from flask_login import current_user, login_required

from . import db as dbm
from . import xray_service as xs


main_bp = Blueprint("main", __name__)
logger = logging.getLogger(__name__)


@main_bp.route("/")
def root():
    return redirect(url_for("auth.login"))


@main_bp.route("/landing")
@login_required
def landing():
    if current_user.is_admin:
        return redirect(url_for("admin.dashboard"))
    db_path = current_app.config["DATABASE"]
    xray_rows = dbm.list_xray_tests(db_path, created_by=current_user.username)
    export_rows = dbm.list_exports(db_path, created_by=current_user.username)
    return render_template(
        "landing.html",
        xray_rows=xray_rows,
        export_rows=export_rows,
        has_pat=bool(current_user.jira_pat),
    )


@main_bp.route("/profile", methods=["GET", "POST"])
@login_required
def profile():
    db_path = current_app.config["DATABASE"]
    if request.method == "POST":
        new_pat = (request.form.get("jira_pat") or "").strip()
        dbm.update_jira_pat(db_path, current_user.id, new_pat or None)
        flash("Jira PAT updated.", "success")
        return redirect(url_for("main.profile"))
    return render_template("profile.html")


@main_bp.route("/generate", methods=["POST"])
@login_required
def generate():
    """Step 1: fetch story + generate testcases. Stash in session for confirmation."""
    if current_user.is_admin:
        flash("Admins must use a non-admin account to create tests.", "error")
        return redirect(url_for("admin.dashboard"))

    story_key = (request.form.get("story_key") or "").strip()
    action = request.form.get("action")  # "excel" or "jira"
    custom_prompt = (request.form.get("custom_prompt") or "").strip()

    if not story_key:
        flash("Please enter a Jira story key.", "error")
        return redirect(url_for("main.landing"))
    if action not in ("excel", "jira"):
        flash("Please select an action.", "error")
        return redirect(url_for("main.landing"))
    if not current_user.jira_pat:
        flash("Please set your Jira PAT in your profile first.", "error")
        return redirect(url_for("main.profile"))

    try:
        story_context = xs.build_story_context(current_user.jira_pat, story_key)
        prompt = custom_prompt or xs.DEFAULT_PROMPT
        testcases = xs.generate_testcases(prompt, story_context)
    except Exception as e:
        logger.exception("Generation failed")
        flash(f"Generation failed: {e}", "error")
        return redirect(url_for("main.landing"))

    session["pending"] = {
        "story_key": story_key,
        "action": action,
        "testcases": testcases,
    }
    return render_template("confirm.html", story_key=story_key, action=action,
                           testcases=testcases)


@main_bp.route("/confirm", methods=["POST"])
@login_required
def confirm():
    decision = request.form.get("decision")  # yes | regenerate | cancel
    pending = session.get("pending")
    if not pending:
        flash("No pending generation; please start again.", "error")
        return redirect(url_for("main.landing"))

    story_key = pending["story_key"]
    action = pending["action"]
    testcases = pending["testcases"]

    if decision == "cancel":
        session.pop("pending", None)
        flash("Cancelled.", "info")
        return redirect(url_for("main.landing"))

    if decision == "regenerate":
        custom = (request.form.get("custom_prompt") or "").strip()
        if not custom:
            flash("Please provide a custom prompt to regenerate.", "error")
            return render_template("confirm.html", story_key=story_key,
                                   action=action, testcases=testcases)
        try:
            ctx = xs.build_story_context(current_user.jira_pat, story_key)
            testcases = xs.generate_testcases(custom, ctx)
        except Exception as e:
            logger.exception("Regeneration failed")
            flash(f"Regeneration failed: {e}", "error")
            return redirect(url_for("main.landing"))
        session["pending"] = {"story_key": story_key, "action": action,
                              "testcases": testcases}
        return render_template("confirm.html", story_key=story_key,
                               action=action, testcases=testcases)

    # decision == yes -> execute the action
    db_path = current_app.config["DATABASE"]
    out_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                           "exports")

    if action == "excel":
        try:
            out_file = xs.export_testcases_to_excel(story_key, testcases, out_dir)
        except Exception as e:
            logger.exception("Excel export failed")
            flash(f"Excel export failed: {e}", "error")
            return redirect(url_for("main.landing"))

        dbm.insert_export(
            db_path,
            jira_id=story_key,
            test_name=os.path.basename(out_file),
            count=len(testcases),
            created_by=current_user.username,
        )
        session.pop("pending", None)
        return send_file(out_file, as_attachment=True,
                         download_name=os.path.basename(out_file))

    # action == jira
    try:
        issuetype_name = xs.detect_test_issuetype_name(current_user.jira_pat)
        steps_field_id = xs.detect_steps_field_id(current_user.jira_pat)
        tt_field_id, tt_payload = xs.detect_test_type_field(current_user.jira_pat)
    except Exception as e:
        logger.exception("Field detection failed")
        flash(f"Field detection failed: {e}", "error")
        return redirect(url_for("main.landing"))

    project_key = story_key.split("-")[0]
    created = []
    errors = []
    for tc in testcases:
        try:
            test_key = xs.create_xray_test(
                current_user.jira_pat,
                story_key=story_key,
                project_key=project_key,
                issuetype_name=issuetype_name,
                steps_field_id=steps_field_id,
                test_type_field_id=tt_field_id,
                test_type_payload=tt_payload,
                tc=tc,
            )
            xs.link_test_to_story(current_user.jira_pat, test_key, story_key)
            steps_text, expected_text = xs.steps_as_text(tc)
            dbm.insert_xray_test(
                db_path,
                jira_id=story_key,
                test_id=test_key,
                description=xs.build_description(story_key, tc),
                steps=steps_text,
                expected=expected_text,
                created_by=current_user.username,
            )
            created.append(test_key)
        except Exception as e:
            logger.exception("Failed creating test for %s", tc.get("Title"))
            errors.append(f"{tc.get('Title','?')}: {e}")

    # Also export an Excel and record export history
    try:
        out_file = xs.export_testcases_to_excel(story_key, testcases, out_dir)
        dbm.insert_export(
            db_path,
            jira_id=story_key,
            test_name=os.path.basename(out_file),
            count=len(testcases),
            created_by=current_user.username,
        )
    except Exception:
        out_file = None

    session.pop("pending", None)
    return render_template(
        "result.html",
        story_key=story_key,
        created=created,
        errors=errors,
        excel_filename=os.path.basename(out_file) if out_file else None,
    )
