"""Keep native installer diagnostics without flooding the interactive terminal."""
from pathlib import Path


INSTALL_OUTPUT_HELPERS = (Path(__file__).parent / "scripts" / "install-output.sh").read_text(encoding="utf-8")


def with_install_output(script: str) -> str:
    # Preserve the public interface marker on line two. The original body keeps
    # its own errexit and traps in an unconditional subshell. In particular, do
    # not move its execution into an if/&&/|| condition to collect its status.
    lines = script.splitlines(keepends=True)
    header = lines[0]
    body = ''.join(lines[1:])
    if len(lines) > 1 and lines[1].startswith('# ConfigFlow'):
        header += lines[1]
        body = ''.join(lines[2:])
    return header + '''set +e
CF_OUTPUT_REUSE=false
if command -v cf_output_is_active >/dev/null 2>&1 && cf_output_is_active; then
    CF_OUTPUT_REUSE=true
fi
''' + INSTALL_OUTPUT_HELPERS + '''
cf_output_begin
if cf_output_is_active; then
    # Open through a compound command so a log redirection failure cannot exit
    # a POSIX shell at the special exec builtin before dependency repair runs.
    if { exec 5>&1; } >>"$CF_OUTPUT_LOG"; then
        exec 1>&5 2>&1 5>&-
    else
        cf_output_degrade
    fi
fi
(
''' + body.rstrip() + '''
)
cf_installer_exit_status=$?
cf_output_end "$cf_installer_exit_status"
exit "$cf_installer_exit_status"
'''
