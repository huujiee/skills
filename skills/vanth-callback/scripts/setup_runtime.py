"""Install the pinned Vanth runtime for this copy of the skill."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import venv


SKILL = Path(__file__).resolve().parent.parent
VERSION = "1.13.0"
OWNER_MARKER = ".vanth-callback-runtime"
ORIGINAL_PIPE_SHA256 = "772c573b9897ea7c938134b2d616ba280653d4cd9511e798b582955d99176f3f"
PATCHED_PIPE_SHA256 = "fea788d1ddb973a6de82b761336331e47d40bd49c9c1c8c9b68f338622a6cdaf"


def find_codex(requested):
    candidates = [requested] if requested else [
        Path("/Applications/ChatGPT.app/Contents/Resources/codex-cli/bin/codex"),
        Path.home() / "Applications/ChatGPT.app/Contents/Resources/codex-cli/bin/codex",
        shutil.which("codex"),
    ]
    for candidate in candidates:
        if candidate:
            path = Path(candidate).expanduser().absolute()
            if path.is_file() and os.access(path, os.X_OK):
                return path
    raise RuntimeError("Set --codex-cli to the absolute path of a Codex CLI with queue --resume support.")


def patch_windows(python, runtime):
    entry = subprocess.check_output(
        [str(python), "-I", "-X", "utf8", "-c",
         "from importlib.metadata import distribution; "
         "print(distribution('vanth').locate_file('vanth/codex_pipe.py'))"],
        text=True, encoding="utf-8",
    )
    adapter = Path(entry.strip()).resolve()
    if not adapter.is_relative_to(runtime) or not adapter.is_file():
        raise RuntimeError("Could not locate the Vanth adapter in the dedicated runtime.")
    data = adapter.read_bytes().replace(b"\r\n", b"\n")
    digest = hashlib.sha256(data).hexdigest()
    if digest == PATCHED_PIPE_SHA256:
        return
    if digest != ORIGINAL_PIPE_SHA256:
        raise RuntimeError("The Vanth adapter differs from the verified upstream 1.13.0 source.")
    before = b'            "arguments": arguments,\n            "callId": call_id,\n'
    after = b'            "arguments": arguments,\n            "callerSource": "codex",\n            "callId": call_id,\n'
    if data.count(before) != 1:
        raise RuntimeError("The Windows compatibility patch did not match the expected adapter.")
    patched = data.replace(before, after, 1)
    if hashlib.sha256(patched).hexdigest() != PATCHED_PIPE_SHA256:
        raise RuntimeError("The patched adapter did not match the verified Windows source.")
    temporary = adapter.with_suffix(".py.tmp")
    temporary.write_bytes(patched)
    temporary.replace(adapter)


def install(runtime, codex_cli):
    if sys.platform not in {"darwin", "win32"}:
        raise RuntimeError("This release supports macOS and Windows.")
    if sys.version_info < (3, 11):
        raise RuntimeError("Python 3.11 or newer is required.")
    if sys.platform == "win32" and platform.machine().lower() not in {"amd64", "x86_64"}:
        raise RuntimeError("The Windows compatibility build is verified for x64 Python.")
    config = {"codex_cli": str(find_codex(codex_cli))} if sys.platform == "darwin" else {}
    runtime = runtime.expanduser().resolve()
    # Windows applies a special ACL for 0o700. Keep the inherited ACL for the runtime executables.
    runtime.mkdir(mode=0o777 if sys.platform == "win32" else 0o700, parents=True, exist_ok=True)
    marker = runtime / OWNER_MARKER
    owner = f"vanth-callback {VERSION}\n"
    if marker.exists():
        if marker.read_text(encoding="utf-8") != owner:
            raise RuntimeError("Choose a fresh runtime directory for this version.")
    else:
        if any(runtime.iterdir()):
            raise RuntimeError("Choose an empty directory for the dedicated runtime.")
        with marker.open("x", encoding="utf-8") as stream:
            stream.write(owner)

    directory = runtime / "venv"
    python = directory / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")
    if not python.is_file():
        venv.EnvBuilder(with_pip=True).create(directory)
    subprocess.run(
        [str(python), "-I", "-m", "pip", "install", "-r", str(SKILL / "requirements.txt")],
        check=True,
    )
    if sys.platform == "win32":
        patch_windows(python, runtime)
    subprocess.run([str(python), "-I", "-m", "pip", "check"], check=True)
    config["python"] = str(python)
    output = SKILL / "runtime.json"
    temporary = output.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
    temporary.replace(output)
    return output


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    codex_home = Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex")))
    parser.add_argument("--runtime-dir", type=Path,
                        default=codex_home / "runtimes" / "vanth-callback" / VERSION)
    parser.add_argument("--codex-cli", type=Path, help="Override the macOS Codex CLI path.")
    args = parser.parse_args()
    try:
        output = install(args.runtime_dir, args.codex_cli)
    except (OSError, RuntimeError, subprocess.CalledProcessError) as exc:
        parser.exit(1, f"Setup failed: {exc}\n")
    print(f"Wrote {output}. Run callback.py doctor from an active local Codex Desktop conversation.")


if __name__ == "__main__":
    main()
