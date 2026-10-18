const shellQuote = (value: string) => `'${value.replace(/'/g, `'"'"'`)}'`
export type CoreInstallTarget = 'both' | 'mihomo' | 'oxidns'
export const CORE_INSTALL_INTERFACE = '# ConfigFlow core installer interface: 1'
export const AGENT_CORE_INSTALL_INTERFACE = '# ConfigFlow Agent + core installer interface: 1'

/** Download to a private file before execution, even on minimal Linux images. */
export function buildCoreInstallCommand(url: string, target: CoreInstallTarget = 'both'): string {
  if (!['both', 'mihomo', 'oxidns'].includes(target)) throw new Error('请选择要安装的核心')
  return buildInstallBootstrap(url, CORE_INSTALL_INTERFACE, '安装脚本版本不支持单核心选择，请更新 ConfigFlow 后重试。', target)
}

export function buildAgentCoreInstallCommand(url: string): string {
  return buildInstallBootstrap(url, AGENT_CORE_INSTALL_INTERFACE, '安装脚本版本不支持同时安装核心，请更新 ConfigFlow 后重试。')
}

function buildInstallBootstrap(url: string, marker: string, versionError: string, target?: CoreInstallTarget): string {
  const bootstrap = [
    'set -eu;',
    'core_url=$1; core_target=${2-};',
    'core_fail() { printf "%s\\n" "$*" >&2; exit 1; };',
    '[ "$(id -u)" = 0 ] || core_fail "请以 root 身份执行安装命令。";',
    '[ "$(uname -s)" = Linux ] || core_fail "仅支持 Linux（systemd 或 OpenRC）。";',
    'set --;',
    'if ! command -v curl >/dev/null 2>&1 && ! command -v wget >/dev/null 2>&1; then set -- "$@" curl ca-certificates; fi;',
    'if ! command -v mktemp >/dev/null 2>&1 || ! command -v rm >/dev/null 2>&1; then set -- "$@" coreutils; fi;',
    'if [ "$#" -gt 0 ]; then',
    'printf "%s\\n" "正在安装缺少的下载工具：$*";',
    'if command -v apt-get >/dev/null 2>&1; then',
    'apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends "$@" || core_fail "依赖安装失败，请检查软件源、网络和包管理器锁后重试。";',
    'elif command -v apk >/dev/null 2>&1; then apk add --no-cache "$@" || core_fail "依赖安装失败，请检查软件源、网络和包管理器锁后重试。";',
    'elif command -v dnf >/dev/null 2>&1; then dnf install -y "$@" || core_fail "依赖安装失败，请检查软件源、网络和包管理器锁后重试。";',
    'elif command -v yum >/dev/null 2>&1; then yum install -y "$@" || core_fail "依赖安装失败，请检查软件源、网络和包管理器锁后重试。";',
    'elif command -v pacman >/dev/null 2>&1; then pacman -S --needed --noconfirm "$@" || core_fail "依赖安装失败，请先按发行版要求维护软件源与系统，再重试。";',
    'elif command -v zypper >/dev/null 2>&1; then zypper --non-interactive install --no-recommends "$@" || core_fail "依赖安装失败，请检查软件源、网络和包管理器锁后重试。";',
    'else core_fail "未找到支持的包管理器，请手动安装缺少的依赖：$*，然后重试。"; fi;',
    'fi;',
    'command -v mktemp >/dev/null 2>&1 && command -v rm >/dev/null 2>&1 || core_fail "仍缺少 mktemp 或 rm，请安装 coreutils 后重试。";',
    'if command -v curl >/dev/null 2>&1; then core_downloader=curl; elif command -v wget >/dev/null 2>&1; then core_downloader=wget; else core_fail "仍缺少 curl 或 wget，请安装后重试。"; fi;',
    'umask 077;',
    'core_script=;',
    'trap \'core_status=$?; trap - 0; [ -z "$core_script" ] || rm -f "$core_script"; exit "$core_status"\' 0;',
    'trap "exit 129" HUP; trap "exit 130" INT; trap "exit 143" TERM;',
    'core_script=$(mktemp) || core_fail "无法创建临时文件，请检查临时目录权限和剩余空间。";',
    'if [ "$core_downloader" = curl ]; then curl -fSL --output "$core_script" "$core_url" || core_fail "下载失败，未执行安装脚本。请检查网络、面板地址及 ca-certificates / 自定义证书信任配置。";',
    'else wget -O "$core_script" "$core_url" || core_fail "下载失败，未执行安装脚本。请检查网络、面板地址及 ca-certificates / 自定义证书信任配置。"; fi;',
    `{ IFS= read -r core_shebang && IFS= read -r core_interface; } < "$core_script" || core_fail ${shellQuote(versionError)};`,
    'case "$core_shebang" in "#!"*) ;; *) core_fail "服务未返回有效的安装脚本，请确认 ConfigFlow 已更新。";; esac;',
    `[ "$core_interface" = ${shellQuote(marker)} ] || core_fail ${shellQuote(versionError)};`,
    target ? 'sh "$core_script" --core "$core_target"' : 'sh "$core_script"',
  ].join(' ')
  return `sh -c ${shellQuote(bootstrap)} sh ${shellQuote(url)}${target ? ` ${shellQuote(target)}` : ''}`
}
