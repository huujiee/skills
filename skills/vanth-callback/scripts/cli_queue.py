"""Mac callback admission through the supported Codex queue CLI.

No private app-tools pipe, second app-server, or model polling is involved.
A queued receipt is admission evidence; the resumed turn must still claim and
finish the delivery. Uncertain submissions are preserved instead of resent.
"""
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import subprocess
import uuid

EVENTS = {"completed", "failed", "timeout", "cancelled"}


def thread_uuid(value):
    try:
        return str(uuid.UUID(value))
    except (ValueError, TypeError, AttributeError) as error:
        raise ValueError("A current Codex conversation UUID is required") from error


def executable(value):
    path = Path(value)
    if not path.is_absolute() or not path.is_file() or not os.access(path, os.X_OK):
        raise ValueError("Configure an existing absolute Codex CLI executable")
    return str(path)


def doctor(cli_path, thread_id, *, runner=None):
    execute = runner or subprocess.run
    cli = executable(cli_path)
    thread = thread_uuid(thread_id)
    version = execute([cli, "--version"], capture_output=True, text=True, timeout=5)
    help_result = execute([cli, "queue", "--help"], capture_output=True, text=True, timeout=5)
    if version.returncode or help_result.returncode or not all(flag in help_result.stdout for flag in ("--thread", "--message", "--resume")):
        raise RuntimeError("Codex CLI does not advertise the supported queue --resume interface")
    return {"transport": "cli_queue", "codex_cli": cli, "thread_id": thread,
            "codex_version": version.stdout.strip(), "queue_supported": True, "resume_supported": True,
            "private_pipe_required": False}


def atomic_json(path, value):
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    with temporary.open("x", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
    temporary.replace(path)


def deliver(run, payload, *, runner=None):
    execute = runner or subprocess.run
    run = Path(run)
    manifest = json.loads((run / "manifest.json").read_text(encoding="utf-8"))
    if manifest.get("transport") != "cli_queue":
        raise ValueError("This run did not select CLI queue delivery")
    thread = thread_uuid(manifest.get("thread_id"))
    cli = executable(manifest.get("codex_cli", ""))
    event, target = payload.get("event", {}), payload.get("target", {})
    delivery = payload.get("delivery_id", "")
    job = event.get("job_id", "")
    if (not isinstance(delivery, str) or not re.fullmatch(r"del_[A-Za-z0-9_-]+", delivery)
            or not isinstance(job, str) or not job.startswith("job_")
            or event.get("type") not in EVENTS):
        raise ValueError("Invalid terminal delivery identity")
    if (target.get("type") != "local_command" or target.get("thread_id") != thread
            or target.get("callback_run_id") != run.name):
        raise ValueError("Delivery does not belong to this run and conversation")
    # An instant workload may finish before /jobs responds. The saved target
    # still binds that event to this run; never race the submitter by rewriting
    # its manifest. A later recover reuses its original idempotency key.
    if manifest.get("job_id") and manifest["job_id"] != job:
        raise ValueError("Delivery belongs to another job")
    receipt = run / ("queue-" + delivery + ".json")
    record = {"state": "submitting", "job_id": job, "delivery_id": delivery,
              "thread_id": thread, "event": event["type"],
              "resume_requested": True, "continuation_verified": False,
              "created_at": datetime.now(timezone.utc).isoformat()}
    try:
        fd = os.open(receipt, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(record, stream, indent=2)
    except FileExistsError:
        try:
            previous = json.loads(receipt.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise RuntimeError(f"Queue admission in progress or uncertain; inspect {receipt}") from error
        if previous.get("state") == "queued":
            return previous
        raise RuntimeError(f"Prior queue admission is uncertain; inspect {receipt}, do not requeue")
    message = ("[VANTH_CALLBACK]\n"
               f"job_id: {job}\ndelivery_id: {delivery}\nevent: {event['type']}\nrun: {run}\n\n"
               "Continue the authorized task using $vanth-callback. Read status for this run, "
               "claim this delivery, inspect actual output, and execute only its saved resume_prompt. "
               "If the submission response was lost and the manifest has no job_id, recover this "
               "same run first. Finish the receipt and clean up the run when continuation is complete. "
               "Queued admission alone does not prove that continuation has completed.")
    try:
        result = execute([cli, "queue", "--thread", thread, "--message", message, "--resume"],
                         capture_output=True, text=True, timeout=20)
    except Exception as error:
        record.update(state="uncertain", error_type=type(error).__name__)
        atomic_json(receipt, record)
        raise RuntimeError(f"Queue admission uncertain; inspect {receipt}, do not requeue") from error
    matches = re.findall(r"^Queued message ([0-9a-fA-F-]+) for thread ([0-9a-fA-F-]+)\.$",
                         result.stdout, re.MULTILINE)
    if result.returncode != 0 or len(matches) != 1 or matches[0][1].lower() != thread:
        record.update(state="uncertain", return_code=result.returncode,
                      stdout=result.stdout[-2000:], stderr=result.stderr[-2000:])
        atomic_json(receipt, record)
        raise RuntimeError(f"Queue admission uncertain; inspect {receipt}, do not requeue")
    message_id = thread_uuid(matches[0][0])
    record.update(state="queued", queued_message_id=message_id,
                  admitted_at=datetime.now(timezone.utc).isoformat(),
                  return_code=0, stdout=result.stdout[-2000:], stderr=result.stderr[-2000:])
    atomic_json(receipt, record)
    return record
