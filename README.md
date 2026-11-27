# Codex skills

## Vanth Callback

[Vanth Callback](skills/vanth-callback/SKILL.md) runs an authorized local command in the background and resumes the originating Codex conversation when the command reaches a terminal state. It was tested with Python 3.13 and local Codex Desktop on macOS and Windows x64.

### Install

Install Python 3.11 or newer and [ChatGPT Desktop with Codex](https://chatgpt.com/desktop/). In Codex, enter:

```text
$skill-installer Install https://github.com/huujiee/skills/tree/main/skills/vanth-callback
```

Run the setup command for your platform once. The examples use the standard skill installer directory. If you use a custom `CODEX_HOME`, use its `skills/vanth-callback` path.

**macOS**

```sh
python3 "$HOME/.codex/skills/vanth-callback/scripts/setup_runtime.py"
```

**Windows PowerShell**

```powershell
py -3 "$env:USERPROFILE\.codex\skills\vanth-callback\scripts\setup_runtime.py"
```

Setup creates a dedicated Python environment and writes `runtime.json` on your computer. macOS installs upstream Vanth. Windows applies the documented one-line compatibility change inside that dedicated environment. The [compatibility notes](skills/vanth-callback/references/compatibility.md) describe both transports and prerequisites.

### Try a callback

Open a local Codex Desktop conversation with terminal access and enter:

> Use $vanth-callback to run a local smoke test. Run doctor first, then submit a Python command that waits 30 seconds and prints VANTH_SMOKE_OK. End this turn once the job is submitted. When the callback arrives, check the output, finish the receipt, and clean up the test job.

The test passes when the original conversation starts a new turn by itself, reports the marker, and cleans up its test daemon. Keep the computer awake and Codex Desktop running during the test.

### Upstream dependency

This skill builds on [Vanth](https://github.com/holdfast-labs/vanth), an Apache-2.0 project. The Windows adjustment and its verification scope are documented in the [compatibility notes](skills/vanth-callback/references/compatibility.md).
