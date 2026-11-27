"""Per-task Vanth lifecycle for local Codex Desktop on macOS or Windows."""
import argparse
from datetime import datetime, timezone
import inspect
import json
import os
from pathlib import Path
import socket
import shlex
import subprocess
import sys
import time
from urllib.error import URLError
import uuid

ROOT = Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex"))) / "vanth-callback-runs"
SCRIPT = Path(__file__).resolve()
TERMINAL = {"completed", "failed", "timeout", "cancelled", "orphaned"}

def read(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))

def write(path, value):
    path = Path(path)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)

def validate_spec(spec):
    args = spec.get("args")
    if not isinstance(args, list) or not args or any(not isinstance(arg, str) for arg in args):
        raise ValueError("args must be a nonempty array of strings")
    if not Path(args[0]).is_absolute() or not Path(args[0]).is_file():
        raise ValueError("args[0] must name an existing absolute interpreter/executable")
    cwd = spec.get("cwd", "")
    if not isinstance(cwd, str) or not Path(cwd).is_absolute() or not Path(cwd).is_dir():
        raise ValueError("cwd must name an existing absolute directory")
    timeout = spec.get("timeout_seconds")
    if type(timeout) is not int or not 1 <= timeout <= 23 * 3600:
        raise ValueError("timeout_seconds must be 1..82800")
    if not isinstance(spec.get("resume_prompt"), str) or not spec["resume_prompt"].strip():
        raise ValueError("resume_prompt must describe the authorized next steps")
    return spec

def resolve_run(value):
    run = Path(value).resolve()
    if run.parent != ROOT.resolve() or not (run / "manifest.json").is_file():
        raise ValueError("Run must be an existing direct child of the callback run root")
    return run

def configure(run):
    manifest = read(run / "manifest.json")
    port = manifest["port"]
    os.environ.update(VANTH_HOME=str(run / "state"), VANTH_DAEMON_HOST="127.0.0.1",
        VANTH_DAEMON_PORT=str(port), VANTH_DAEMON_URL=f"http://127.0.0.1:{port}",
        VANTH_CLIENT_TIMEOUT="15")
    for name in ("AGENT_BG_HOME", "VANTH_CODEX_DESKTOP_PIPE", "VANTH_CODEX_DESKTOP_THREAD"):
        os.environ.pop(name, None)
    from vanth.client import VanthClient
    return manifest, VanthClient(home=run / "state")

def ready(client):
    try:
        payload = client.get("/doctor", timeout=1)
        return (isinstance(payload, dict) and payload.get("result") != "error"
            and payload.get("schema_version") is not None
            and Path(str(payload.get("home", ""))).resolve() == client.home)
    except URLError as exc:
        if isinstance(exc.reason, (ConnectionRefusedError, ConnectionResetError, TimeoutError)):
            return False
        raise
    except (ConnectionResetError, TimeoutError):
        return False

def spawn(run, mode):
    argv = [sys.executable, str(SCRIPT), mode, "--run", str(run)]
    with (run / (mode + ".log")).open("ab") as log:
        platform_options = ({"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW}
                            if os.name == "nt" else {"start_new_session": True})
        return subprocess.Popen(argv, cwd=run, env=os.environ.copy(), stdin=subprocess.DEVNULL,
            stdout=log, stderr=log, **platform_options)

def command_line(argv):
    return subprocess.list2cmdline(argv) if os.name == "nt" else shlex.join(argv)

def doctor():
    from importlib.metadata import version
    installed = version("vanth")
    if installed != "1.13.0":
        raise RuntimeError("This adapter was validated with Vanth 1.13.0; revalidate a different version")
    if sys.platform == "darwin":
        from cli_queue import doctor as queue_doctor
        config = read(SCRIPT.parent.parent / "runtime.json")
        return {"vanth_version": installed, **queue_doctor(config["codex_cli"], os.environ.get("CODEX_THREAD_ID"))}
    if sys.platform != "win32":
        raise RuntimeError("This release supports macOS and Windows.")
    from vanth.codex_pipe import CodexPipeClient, CodexPipeError
    if '"callerSource": "codex"' not in inspect.getsource(CodexPipeClient.send_message_to_thread):
        raise RuntimeError("Required callerSource compatibility patch is missing; see references/compatibility.md")
    pipe = os.environ.get("CODEX_APP_TOOLS_PIPE_PATH")
    if not pipe or not os.environ.get("CODEX_THREAD_ID"):
        raise RuntimeError("Run inside an active Codex Desktop conversation")
    connection = None
    try:
        connection = CodexPipeClient(pipe, timeout_seconds=5)
        if not connection.has_capability("codex_app", "send_message_to_thread"):
            raise RuntimeError("Desktop send_message_to_thread capability is unavailable")
    except CodexPipeError as exc:
        raise RuntimeError("Windows Desktop did not accept the callback client. "
            "Run doctor inside a local Codex Desktop conversation. See references/compatibility.md.") from exc
    finally:
        if connection is not None:
            connection.close()
    return {"vanth_version": installed, "transport": "codex_desktop", "protocol_patch_present": True, "desktop_capability": True}

def snapshot(run, manifest, client):
    job = manifest["job_id"]
    status = client.get(f"/jobs/{job}/status")
    events = client.get(f"/jobs/{job}/events", {"limit": 30}).get("events", [])
    deliveries = client.get("/deliveries", {"job_id": job, "limit": 30}).get("deliveries", [])
    result = {"run": str(run), "job_id": job, "status": status.get("status"),
        "exit_code": status.get("exit_code"), "event_types": [event.get("type") for event in events],
        "deliveries": [{key: delivery.get(key) for key in
            ("delivery_id", "status", "attempts", "last_error", "delivered_at")}
            for delivery in deliveries if delivery.get("job_id") == job],
        "resume_prompt": manifest["resume_prompt"],
        "transport": manifest.get("transport", "codex_desktop"),
        "queue_admissions": [read(path) for path in sorted(run.glob("queue-del_*.json"))],
        "stdout_tail": client.get(f"/jobs/{job}/tail", {"max_bytes": 4096, "stream": "stdout"}),
        "stderr_tail": client.get(f"/jobs/{job}/tail", {"max_bytes": 4096, "stream": "stderr"})}
    write(run / "result.json", result)
    return result

def post_saved_request(run, manifest, client):
    try:
        result = client.post("/jobs", read(run / "request.json"))
        if not result.get("job_id"):
            raise RuntimeError("Job submission response did not identify a job")
    except Exception as exc:
        manifest["submission_uncertain"] = True
        write(run / "manifest.json", manifest)
        raise RuntimeError(f"Submission unconfirmed; preserve this run and use recover --run {run}. Do not submit a new spec.") from exc
    manifest["job_id"] = result["job_id"]
    manifest.pop("submission_uncertain", None)
    write(run / "manifest.json", manifest)
    return {"job_id": manifest["job_id"], "status": result.get("status"),
        "run": str(run), "context_file": str(run / "manifest.json"),
        "timeout_seconds": manifest["timeout_seconds"], "heartbeat_created": False}

def submit(spec_path):
    spec = validate_spec(read(spec_path))
    preflight = doctor()
    directory_mode = 0o777 if os.name == "nt" else 0o700
    ROOT.mkdir(parents=True, exist_ok=True, mode=directory_mode)
    run = ROOT / uuid.uuid4().hex
    run.mkdir(mode=directory_mode)
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    manifest = {"port": port, "resume_prompt": spec["resume_prompt"],
        "created_at": datetime.now(timezone.utc).isoformat(), "timeout_seconds": spec["timeout_seconds"],
        "transport": preflight.get("transport", "codex_desktop")}
    if manifest["transport"] == "cli_queue":
        manifest.update(thread_id=preflight["thread_id"], codex_cli=preflight["codex_cli"])
    write(run / "manifest.json", manifest)
    # The spec contains no ambient environment or Desktop capability values.
    write(run / "spec.json", spec)
    manifest, client = configure(run)
    from vanth.paths import secure_home_permissions
    secure_home_permissions(run)
    daemon = spawn(run, "daemon")
    manifest["daemon_pid"] = daemon.pid
    write(run / "manifest.json", manifest)
    deadline = time.monotonic() + 12
    while not ready(client):
        if daemon.poll() is not None or time.monotonic() > deadline:
            raise RuntimeError(f"Daemon startup failed; inspect {run / 'daemon.log'}")
        time.sleep(.1)
    if manifest["transport"] != "cli_queue":
        from vanth.setup import register_codex_desktop
        register_codex_desktop(run / "state")
        relay = spawn(run, "relay")
        manifest["relay_pid"] = relay.pid
        write(run / "manifest.json", manifest)
        deadline = time.monotonic() + 12
        while not (run / "relay-ready.json").exists():
            if relay.poll() is not None or time.monotonic() > deadline:
                client.post("/shutdown", {})
                raise RuntimeError(f"Relay startup failed; inspect {run / 'relay.log'}")
            time.sleep(.1)
    if manifest["transport"] == "cli_queue":
        wake_target = {"type": "local_command", "thread_id": manifest["thread_id"],
            "callback_run_id": run.name,
            "command": [sys.executable, str(SCRIPT), "queue-notify", "--run", str(run)],
            "events": ["completed", "failed", "timeout", "cancelled"],
            "timeout_seconds": 25, "max_attempts": 1}
    else:
        wake_target = {"type": "codex_desktop", "thread_id": os.environ["CODEX_THREAD_ID"],
            "events": ["completed", "failed", "timeout", "cancelled"], "timeout_seconds": 15}
    command = command_line([sys.executable, str(SCRIPT), "workload", "--run", str(run)])
    request = {"command": command, "cwd": spec["cwd"],
        "idempotency_key": "callback-" + run.name,
        "name": spec.get("name", "callback-task"), "timeout_seconds": spec["timeout_seconds"],
        "notes": f"Callback context: {run / 'manifest.json'}. {spec['resume_prompt']}",
        "wake_targets": [wake_target]}
    write(run / "request.json", request)
    return post_saved_request(run, manifest, client)

def relay_worker(run, manifest, client):
    from vanth.relay import start_desktop_relay
    relay = start_desktop_relay()
    if relay is None:
        raise RuntimeError("Desktop relay unavailable")
    try:
        deadline = time.monotonic() + 10
        while not relay._registered and time.monotonic() < deadline:
            time.sleep(.1)
        if not relay._registered:
            raise RuntimeError("Desktop relay did not register")
        write(run / "relay-ready.json", {"registered": True})
        deadline = time.monotonic() + manifest["timeout_seconds"] + 300
        while time.monotonic() < deadline and not (run / "relay-stop.flag").exists():
            time.sleep(.5)
    finally:
        relay.stop()
        (run / "relay-stopped.flag").touch()

def cleanup_run(run, manifest, client):
    if manifest.get("cleaned_up"):
        return {"already_cleaned_up": True, "run": str(run)}
    uses_relay = manifest.get("transport", "codex_desktop") != "cli_queue"
    receipt_path = run / "receipt.json"
    receipt = read(receipt_path) if receipt_path.is_file() else {}
    if receipt.get("state") != "completed" or receipt.get("job_id") != manifest["job_id"]:
        raise RuntimeError("Finish the callback continuation before cleanup")
    daemon_ready = ready(client)
    if not daemon_ready and not (run / "result.json").exists():
        raise RuntimeError("No live daemon or saved terminal result; preserve run for diagnosis")
    result = snapshot(run, manifest, client) if daemon_ready else read(run / "result.json")
    if result["status"] not in TERMINAL:
        raise RuntimeError("Refusing cleanup of a running task; stop it only when authorized")
    if not any(delivery["status"] == "delivered" and delivery["delivery_id"] == receipt.get("delivery_id")
               for delivery in result["deliveries"]):
        raise RuntimeError("Finished receipt does not match an acknowledged delivery")
    if uses_relay:
        (run / "relay-stop.flag").touch()
        deadline = time.monotonic() + 10
        while not (run / "relay-stopped.flag").exists() and time.monotonic() < deadline:
            time.sleep(.1)
        if not (run / "relay-stopped.flag").exists():
            raise RuntimeError("Relay did not stop; daemon retained for diagnosis")
    if daemon_ready:
        client.post("/shutdown", {})
    deadline = time.monotonic() + 12
    while (ready(client) or (run / "state" / "daemon.json").exists()) and time.monotonic() < deadline:
        time.sleep(.1)
    if ready(client) or (run / "state" / "daemon.json").exists():
        raise RuntimeError("Daemon did not stop")
    manifest["cleaned_up"] = True
    write(run / "manifest.json", manifest)
    return {"job_id": manifest["job_id"], "relay_used": uses_relay,
            "relay_stopped": True, "daemon_stopped": True, "evidence_preserved": str(run)}

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["doctor", "submit", "recover", "status", "claim", "finish", "stop", "cleanup", "daemon", "relay", "workload", "queue-notify"])
    parser.add_argument("--spec")
    parser.add_argument("--run")
    parser.add_argument("--job")
    parser.add_argument("--delivery")
    args = parser.parse_args()
    if args.action == "doctor":
        result = doctor()
    elif args.action == "submit":
        if not args.spec:
            parser.error("submit requires --spec")
        result = submit(args.spec)
    else:
        if args.job:
            matches = [path.parent for path in ROOT.glob("*/manifest.json") if read(path).get("job_id") == args.job]
            if len(matches) != 1:
                raise RuntimeError("Expected exactly one saved run for callback job")
            run = resolve_run(matches[0])
        elif args.run:
            run = resolve_run(args.run)
        else:
            parser.error("operation requires --run or --job")
        if args.action == "queue-notify":
            from cli_queue import deliver
            print(json.dumps(deliver(run, json.load(sys.stdin)), ensure_ascii=False, indent=2))
            return
        manifest, client = configure(run)
        if args.action == "recover":
            print(json.dumps(post_saved_request(run, manifest, client), ensure_ascii=False, indent=2))
            return
        if args.action == "daemon":
            from vanth.daemon import main as daemon_main
            daemon_main()
            return
        if args.action == "relay":
            relay_worker(run, manifest, client)
            return
        if args.action == "workload":
            spec = validate_spec(read(run / "spec.json"))
            raise SystemExit(subprocess.call(spec["args"], cwd=spec["cwd"], stdin=subprocess.DEVNULL, shell=False))
        if args.action == "status":
            result = read(run / "result.json") if manifest.get("cleaned_up") else snapshot(run, manifest, client)
        elif args.action in {"claim", "finish"}:
            if not args.delivery:
                parser.error("claim and finish require --delivery from the received callback")
            current = read(run / "result.json") if manifest.get("cleaned_up") else snapshot(run, manifest, client)
            if not any(item["delivery_id"] == args.delivery and item["status"] == "delivered" for item in current["deliveries"]):
                raise RuntimeError("Delivery does not belong to this task or is not acknowledged")
            receipt = run / "receipt.json"
            if args.action == "claim":
                try:
                    with receipt.open("x", encoding="utf-8") as stream:
                        json.dump({"job_id": manifest["job_id"], "delivery_id": args.delivery,
                            "state": "processing", "claimed_at": datetime.now(timezone.utc).isoformat()}, stream)
                    result = {"claimed": True, "receipt": str(receipt)}
                except FileExistsError:
                    result = {"claimed": False, "prior_receipt": read(receipt)}
            else:
                previous = read(receipt)
                if previous.get("delivery_id") != args.delivery:
                    raise RuntimeError("Receipt belongs to a different delivery")
                previous.update(state="completed", finished_at=datetime.now(timezone.utc).isoformat())
                write(receipt, previous)
                result = previous
        elif args.action == "stop":
            result = client.post(f"/jobs/{manifest['job_id']}/stop", {})
        else:
            result = cleanup_run(run, manifest, client)
    print(json.dumps(result, ensure_ascii=False, indent=2))

if __name__ == "__main__":
    main()
