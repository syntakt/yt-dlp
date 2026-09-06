"""Executable boundary for yt-dlp's FFmpeg downloaders AND postprocessors.

Some ffprobe calls ignore postprocessor_args entirely. Keep the policy at
the executable boundary instead. This restricts input protocols; it is not
a filesystem sandbox or a replacement for container egress isolation.
"""

import os
from pathlib import Path
import shutil
import sys


LOCAL_PROTOCOLS = "file,pipe,crypto,data"


def restricted_args(program, args):
    if program == "ffprobe":
        # ffprobe has one input, often positional instead of preceded by -i.
        return ["-protocol_whitelist", LOCAL_PROTOCOLS, *args]
    result = []
    for arg in args:
        if arg == "-i":
            result.extend(["-protocol_whitelist", LOCAL_PROTOCOLS])
        result.append(arg)
    return result


def main(program):
    # Search the ordinary system PATH, excluding these wrappers even if an
    # operator added their directory to PATH. Never silently fall back to an
    # unrestricted invocation when the real executable is missing.
    wrapper_dir = Path(__file__).resolve().parent
    search_path = os.pathsep.join(
        entry for entry in os.get_exec_path()
        if Path(entry).resolve() != wrapper_dir
    )
    executable = shutil.which(program, path=search_path)
    if not executable or Path(executable).resolve().parent == wrapper_dir:
        sys.exit(f"System {program} executable not found")
    os.execv(executable, [executable, *restricted_args(program, sys.argv[1:])])
