# Tool execution across Windows, macOS and Linux

The platform-specific boundary is `nerya/core/process.py` and
`nerya/core/sandbox.py`. Native file/skill tools use
`nerya/tools/native/paths.py`. The Agent Loop and trading approvals are unchanged.

## Paths and file contents

- Supply `cwd` separately from the shell command. Relative working directories
  resolve under the workspace, never under the API server's launch directory.
- Prefer workspace-relative forward-slash paths in tool JSON. Windows also
  accepts native backslashes and absolute paths inside the workspace. Returned
  workspace-relative paths use forward slashes on every platform.
- Do not pass drive-relative paths such as `C:notes.txt`. Different-drive escapes,
  symlink/junction escapes, Windows device names, alternate data streams and
  ambiguous trailing dots/spaces are rejected. Foreign Windows drive/UNC paths
  on POSIX are not silently converted into local filenames. POSIX literal
  backslashes remain literal; command arguments are never globally rewritten.
- File writes preserve the exact UTF-8 bytes used for the file-state hash.
  Edits to uniformly CRLF files accept LF snippets and retain CRLF on disk;
  mixed-newline files require an exact match. Fresh-read checks remain enforced.

## Commands and scripts

`run_shell` defaults to **cmd.exe on Windows** and **/bin/sh on macOS/Linux**.
The tool description reports this at registration time. Its optional `shell`
argument selects `powershell`, `pwsh`, `bash`, `sh` or Windows `cmd`; selecting an
interpreter does not install it. Bash commands are not guessed or translated.

PowerShell commands are classified in their original form, then passed directly
to the selected interpreter as UTF-16LE `-EncodedCommand`. This avoids re-parsing
them through cmd. PowerShell uses UTF-8 output and propagates native exit codes.
For a UNC working directory, the Windows default uses PowerShell; explicit cmd
returns an actionable error instead of running in an unrelated directory.

Use `script_run` for skill helpers. Python uses the running interpreter; bundled
Python packages retain `-m` invocation. JavaScript uses Node and `.ps1` uses an
installed PowerShell interpreter. Both `helper.py` and `scripts/helper.py` names
are accepted. Script arguments remain an argv array, preserving spaces, empty
strings, backslashes and JSON. Shell scripts still require Bash. Batch scripts
retain Windows' native cmd argument semantics; they are not POSIX commands.

On Windows, process launch resolves executables against the child environment's
PATH (including `.cmd` shims), normalizes environment-variable key casing and
defaults Python subprocess I/O to UTF-8. Existing explicit encoding settings
win. POSIX executable lookup and environment casing are unchanged.

## Output, stop and timeout

POSIX retains its selector/process-group implementation. Windows drains stdout
and stderr on separate threads rather than attempting to select anonymous
pipes. Cancellation and timeout use `taskkill /T /F` with a direct-child kill
fallback, then reap the child. This is process lifecycle management, not an OS
security sandbox. `process_group_stopped` is true on Windows only after tree-stop
confirmation; normal parent exit alone does not prove every descendant exited.
Independently detached processes may outlive their parent; retained pipes have
a bounded drain wait and do not imply ownership of those processes.

Stdio MCP uses independently drained stdout/stderr, real bounded reply waits
and serialized request/reply pairs. Timeout closes the transport so a late
response cannot be consumed by the next call. Server stderr is discarded rather
than copied into logs, where it could reveal credentials.

## Regression gate

`.github/workflows/tool-compatibility.yml` runs the same offline contracts on
Windows, macOS and Linux with pinned Python. POSIX process-group tests run
separately on macOS/Linux. Native Windows-only filename and PowerShell checks
are explicitly skipped on other hosts, not represented as Windows verification.

```text
python -m pytest -m "" -q tests/test_cross_platform_tools.py tests/test_sandbox_exec.py tests/test_native_search_tool.py tests/test_runtime_env.py tests/test_builtin_script_module.py tests/test_mcp_connectors.py
```

Creating this workflow is not evidence that its hosted runners have passed;
check the actual run results before releasing a Windows package.
