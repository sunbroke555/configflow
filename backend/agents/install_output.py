"""Keep native installer diagnostics without changing shell lifecycle semantics."""
from pathlib import Path


_SCRIPTS = Path(__file__).parent / "scripts"
CLI_OUTPUT_HELPERS = (_SCRIPTS / "cli-output.sh").read_text(encoding="utf-8")
INSTALL_OUTPUT_HELPERS = (_SCRIPTS / "install-output.sh").read_text(encoding="utf-8")


def with_root_privileges(script: str) -> str:
    """Keep privilege handling in the downloaded file, preserving stdin/args."""
    lines = script.splitlines(keepends=True)
    split = 2 if len(lines) > 1 and lines[1].startswith('# ConfigFlow') else 1
    return ''.join(lines[:split]) + '''if [ "$(id -u)" -ne 0 ]; then
    if ! command -v sudo >/dev/null 2>&1; then
        printf '%s\\n' '请使用 root 运行安装脚本，或先安装并配置 sudo。' >&2
        exit 1
    fi
    exec sudo sh "$0" "$@"
fi
''' + ''.join(lines[split:])


def with_install_output(script: str) -> str:
    # Preserve the public interface marker on line two. The original body keeps
    # its own errexit and traps in an unconditional subshell. Never wrap it in
    # if/&&/||: those contexts disable the body's set -e failure handling.
    lines = script.splitlines(keepends=True)
    header = lines[0]
    body = ''.join(lines[1:])
    if len(lines) > 1 and lines[1].startswith('# ConfigFlow'):
        header += lines[1]
        body = ''.join(lines[2:])
    return header + 'set +e\n' + CLI_OUTPUT_HELPERS + '\n' + INSTALL_OUTPUT_HELPERS + '''
cf_verbose_options "$@"
cf_init "安装与维护"
(
''' + body.rstrip() + '''
)
cf_installer_exit_status=$?
cf_finish "$cf_installer_exit_status"
exit "$cf_installer_exit_status"
'''
