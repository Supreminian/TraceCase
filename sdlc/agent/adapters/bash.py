"""An opt-in, fixed-operation process adapter; deliberately not a shell sandbox."""

from __future__ import annotations

import json
import os
import selectors
import signal
import subprocess
import time
from pathlib import Path

from ..domain import Tool


class BashTool(Tool):
    """Expose a tiny read-only argv grammar with no arbitrary script execution.

    Production test preparation does not need shell access. This adapter exists
    for explicit local diagnostics, disabled by default. It lists approved
    directory names, reports cwd, or gives git file/status statistics; it cannot
    read file contents, invoke hooks, start a shell, or access network commands.
    """

    name = "bash"
    description = ("Optional local read-only diagnostics. Pass command as argv: ['pwd'], "
                   "['ls'] or ['ls', approved_directory], ['git','status','--short'], "
                   "or ['git','diff','--stat']. No arbitrary Bash, file contents or network.")
    parameters = {"type": "object", "properties": {
        "command": {"type": "array", "items": {"type": "string"}, "minItems": 1,
                    "maxItems": 3}}, "required": ["command"], "additionalProperties": False}

    def __init__(self, workspace, *, enabled=False,
                 allowed_commands=("pwd", "ls", "git"), allowed_directories=(".",),
                 timeout_seconds=5, max_output_bytes=16384):
        self.workspace = Path(workspace).resolve(strict=True)
        if not self.workspace.is_dir():
            raise ValueError("workspace must be a directory")
        if set(allowed_commands) - {"pwd", "ls", "git"}:
            raise ValueError("only fixed read-only operations are supported")
        if not 0 < timeout_seconds <= 30 or not 128 <= max_output_bytes <= 65536:
            raise ValueError("invalid diagnostics resource limits")
        self.enabled = enabled
        self.allowed_commands = tuple(allowed_commands)
        self.allowed_directories = {self._directory(value) for value in allowed_directories}
        self.timeout_seconds = timeout_seconds
        self.max_output_bytes = max_output_bytes

    def _directory(self, value):
        if not isinstance(value, str) or Path(value).is_absolute() or ".." in Path(value).parts:
            raise ValueError("directory must be an approved workspace relative directory")
        path = self.workspace / value
        if any(part.startswith(".") and part != "." for part in Path(value).parts):
            raise ValueError("hidden directories are not diagnostic targets")
        if path.resolve(strict=True) != path.absolute() or not path.is_dir():
            raise ValueError("symlink directories are not diagnostic targets")
        path.resolve().relative_to(self.workspace)
        return path.resolve()

    def execute(self, arguments):
        if not self.enabled:
            raise PermissionError("local process diagnostics are disabled")
        if not isinstance(arguments, dict) or set(arguments) != {"command"}:
            raise ValueError("command argv is required")
        command = arguments["command"]
        if (not isinstance(command, list) or not command
                or any(not isinstance(arg, str) or len(arg) > 256 for arg in command)
                or command[0] not in self.allowed_commands):
            raise PermissionError("command is outside the diagnostic allowlist")
        if command == ["pwd"]:
            argv = ["/bin/pwd", "-P"]
        elif command[0] == "ls" and len(command) <= 2:
            path = self._directory(command[1] if len(command) == 2 else ".")
            if path not in self.allowed_directories:
                raise PermissionError("directory is outside the diagnostic allowlist")
            argv = ["/bin/ls", "-1", "--", str(path)]
        elif command in (["git", "status", "--short"], ["git", "diff", "--stat"]):
            git_dir = self.workspace / ".git"
            if not git_dir.is_dir() or git_dir.is_symlink():
                raise PermissionError("git diagnostics require local workspace metadata")
            # Do not follow a worktree's external .git pointer or symlinked
            # metadata, and override core.worktree from repository config.
            if any(path.is_symlink() for path in (git_dir / "config", git_dir / "index",
                                                  git_dir / "objects")):
                raise PermissionError("external git metadata is not a diagnostic target")
            argv = ["/usr/bin/git", "--no-pager", "--git-dir", str(git_dir),
                    "--work-tree", str(self.workspace), "-c", "core.fsmonitor=false", "-c",
                    "core.hooksPath=/dev/null", "-c", "core.untrackedCache=false"]
            if command[1] == "status":
                argv += ["status", "--short", "--untracked-files=no", "--ignore-submodules=all"]
            else:
                argv += ["diff", "--stat", "--no-ext-diff", "--no-textconv",
                         "--ignore-submodules=all"]
        else:
            raise PermissionError("arguments are outside the fixed diagnostic grammar")
        # Deliberately build a fresh environment: no provider keys, PATH lookup,
        # HOME configuration, Git helpers, tracing, or dynamic-loader injection.
        env = {"PATH": "/usr/bin:/bin", "LANG": "C", "LC_ALL": "C",
               "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null",
               "GIT_TERMINAL_PROMPT": "0", "GIT_OPTIONAL_LOCKS": "0"}
        output, truncated, timed_out = bytearray(), False, False
        process = subprocess.Popen(argv, cwd=self.workspace, env=env, stdin=subprocess.DEVNULL,
                                   stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                   shell=False, start_new_session=True)
        deadline = time.monotonic() + self.timeout_seconds
        try:
            with selectors.DefaultSelector() as selector:
                selector.register(process.stdout, selectors.EVENT_READ)
                while selector.get_map():
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        timed_out = True
                        break
                    for key, _ in selector.select(min(remaining, 0.1)):
                        chunk = os.read(key.fileobj.fileno(), 4096)
                        if not chunk:
                            selector.unregister(key.fileobj)
                            break
                        room = self.max_output_bytes - len(output)
                        output.extend(chunk[:room])
                        if len(chunk) > room:
                            truncated = True
                            break
                    if truncated:
                        break
        finally:
            if process.poll() is None:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    # macOS may deny group signaling even for our own child.
                    # The fixed grammar cannot spawn arbitrary descendants.
                    if process.poll() is None:
                        process.kill()
            process.wait(timeout=2)
            process.stdout.close()
        return json.dumps({"exit_code": process.returncode,
                           "output": output.decode("utf-8", errors="replace"),
                           "truncated": truncated, "timed_out": timed_out}, ensure_ascii=False)
