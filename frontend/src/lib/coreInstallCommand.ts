const shellQuote = (value: string) => `'${value.replace(/'/g, `'"'"'`)}'`
export type CoreInstallTarget = 'both' | 'mihomo' | 'oxidns'
export type InstallerDownloader = 'curl' | 'wget'
export const CORE_INSTALL_INTERFACE = '# ConfigFlow core installer interface: 1'
export const AGENT_CORE_INSTALL_INTERFACE = '# ConfigFlow Agent + core installer interface: 1'

export function buildCoreInstallCommand(url: string, target: CoreInstallTarget = 'both', downloader: InstallerDownloader = 'curl'): string {
  if (!['both', 'mihomo', 'oxidns'].includes(target)) throw new Error('请选择要安装的核心')
  return buildDownloadCommand(url, downloader, ` --core ${shellQuote(target)}`)
}

export function buildAgentCoreInstallCommand(url: string, downloader: InstallerDownloader = 'curl'): string {
  return buildAgentInstallCommand(url, downloader)
}

export function buildAgentInstallCommand(url: string, downloader: InstallerDownloader = 'curl'): string {
  return buildDownloadCommand(url, downloader)
}

/** Keep the download private and complete before execution; setup lives in the script. */
function buildDownloadCommand(url: string, downloader: InstallerDownloader, args = ''): string {
  if (!['curl', 'wget'].includes(downloader)) throw new Error('请选择 curl 或 wget')
  const download = downloader === 'curl'
    ? `curl -fSL ${shellQuote(url)} -o "$cf_script"`
    : `wget -O "$cf_script" ${shellQuote(url)}`
  return `(cf_script=$(mktemp) && trap 'rm -f "$cf_script"' 0 && ${download} && sh "$cf_script"${args})`
}
