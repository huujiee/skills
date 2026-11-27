# Setup and compatibility

Run [setup_runtime.py](../scripts/setup_runtime.py) with Python 3.11 or newer. The script creates a dedicated Vanth 1.13.0 environment and generates `runtime.json` next to the skill. It accepts `--runtime-dir` for a different environment location. Run `callback.py doctor` using the configured Python from an active local Codex Desktop conversation.

## macOS

The adapter calls the public `codex queue --thread … --message … --resume` command with the installed CLI. A running Codex daemon and a CLI that exposes `--resume` are required. The setup script finds the packaged CLI or the `codex` on your path. Set `--codex-cli` if you use a different installation.

Queue admission and actual resume are distinct events. The CLI can return success with a resume warning. The run keeps the command's stdout, stderr, and queue receipt. An active Desktop session can consume a queued callback even when its explicit resume request is refused. Check the continuation before drawing conclusions about a warning.

## Windows x64

The adapter uses Vanth's native Desktop relay with the capability supplied to the active local Codex conversation. Setup applies the verified single-line `callerSource` compatibility change to the dedicated Vanth 1.13.0 environment and checks both the original and patched source fingerprints. The change is implemented in [setup_runtime.py](../scripts/setup_runtime.py) against [the upstream v1.13.0 adapter](https://github.com/holdfast-labs/vanth/blob/v1.13.0/src/vanth/codex_pipe.py). This path requires the Desktop-provided capability and caller thread identity.

Run doctor from a Codex Desktop task on Windows, including a task sent through a connected Windows Codex Remote host. A standalone PowerShell or SSH session lacks that conversation context. Use the Desktop conversation for submission and the job evidence for independent readback.

## Verification

After installation, run a short callback smoke test from the conversation where you want to resume. The expected evidence is the job's terminal state, a single delivered callback, a new turn in the originating conversation, a completed receipt, and daemon cleanup.

The tested macOS CLI and Windows native paths have each resumed a conversation after it was unloaded. Treat application restart, system sleep, and new Codex or Vanth versions as separate compatibility checks.
