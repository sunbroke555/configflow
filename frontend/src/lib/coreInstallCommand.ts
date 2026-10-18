const shellQuote = (value: string) => `'${value.replace(/'/g, `'"'"'`)}'`
export type CoreInstallTarget = 'both' | 'mihomo' | 'oxidns'
export type InstallerDownloader = 'curl' | 'wget'
export const CORE_INSTALL_INTERFACE = '# ConfigFlow core installer interface: 1'
export const AGENT_CORE_INSTALL_INTERFACE = '# ConfigFlow Agent + core installer interface: 1'

/** The short URL identifies the exact server-generated script, including its install options. */
export function buildInstallCommand(url: string, downloader: InstallerDownloader = 'curl'): string {
  if (!['curl', 'wget'].includes(downloader)) throw new Error('请选择 curl 或 wget')
  const download = downloader === 'curl'
    ? `curl -fsSL ${shellQuote(url)} -o configflow-install.sh`
    : `wget -O configflow-install.sh ${shellQuote(url)}`
  return `${download} && sh configflow-install.sh`
}
