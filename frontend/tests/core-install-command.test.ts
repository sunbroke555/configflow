// @vitest-environment node
import { afterEach, describe, expect, it } from 'vitest'
import { spawnSync } from 'node:child_process'
import { existsSync, mkdirSync, mkdtempSync, readFileSync, rmSync, symlinkSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { buildAgentInstallCommand, buildAgentCoreInstallCommand, buildCoreInstallCommand, type CoreInstallTarget, type InstallerDownloader } from '@/lib/coreInstallCommand'

const directories: string[] = []
const quotedUrl = "https://config.example.test/base'$(printf_injected)`ignored`/install-script?name=home&type=mihomo"

function fixture(options: { missingDownloader?: InstallerDownloader; tempFailure?: boolean } = {}) {
  const directory = mkdtempSync(join(tmpdir(), 'configflow-download-'))
  directories.push(directory)
  const bin = join(directory, 'bin')
  mkdirSync(bin)
  const script = (name: string, content: string) => writeFileSync(join(bin, name), `#!/bin/sh\n${content}\n`, { mode: 0o755 })
  symlinkSync('/bin/sh', join(bin, 'sh'))
  script('mktemp', options.tempFailure ? 'exit 9' : 'umask 077; : > "$CORE_TEST_DIR/download"; printf "%s\\n" "$CORE_TEST_DIR/download"')
  script('rm', 'exec /bin/rm "$@"')
  for (const downloader of ['curl', 'wget'] as const) {
    if (options.missingDownloader === downloader) continue
    script(downloader, `
core_output=; core_url=
while [ "$#" -gt 0 ]; do
  case "$1" in
    -o|-O) core_output=$2; shift 2;;
    -fSL) shift;;
    *) core_url=$1; shift;;
  esac
done
printf '%s' "$core_url" > "$CORE_TEST_DIR/url"
printf '%s' '${downloader}' > "$CORE_TEST_DIR/downloader"
printf '%s\\n' '#!/bin/sh' '[ -f "$CORE_TEST_DIR/download-complete" ] || exit 33' 'printf "%s\\n" "$*" > "$CORE_TEST_DIR/arguments"' 'IFS= read -r answer; printf "%s" "$answer" > "$CORE_TEST_DIR/input"' 'printf executed > "$CORE_TEST_DIR/result"' 'exit "\${CORE_TEST_INSTALL_STATUS:-0}"' > "$core_output"
[ "\${CORE_TEST_DOWNLOAD_STATUS:-0}" = 0 ] || exit "$CORE_TEST_DOWNLOAD_STATUS"
: > "$CORE_TEST_DIR/download-complete"
`)
  }
  return {
    run(command: string, extraEnv: Record<string, string> = {}) {
      // Only test doubles are on PATH: no package installation or network access.
      return spawnSync('/bin/sh', ['-c', command], {
        env: { PATH: bin, CORE_TEST_DIR: directory, ...extraEnv },
        input: 'interactive-choice\n', encoding: 'utf8', timeout: 5000,
      })
    },
    read(name: string) { return readFileSync(join(directory, name), 'utf8') },
    exists(name: string) { return existsSync(join(directory, name)) },
  }
}

afterEach(() => {
  for (const directory of directories.splice(0)) rmSync(directory, { recursive: true, force: true })
})

const variants = [
  { name: 'Agent', build: (downloader: InstallerDownloader) => buildAgentInstallCommand(quotedUrl, downloader), args: '' },
  { name: 'Agent with cores', build: (downloader: InstallerDownloader) => buildAgentCoreInstallCommand(quotedUrl, downloader), args: '' },
  ...(['both', 'mihomo', 'oxidns'] as const).map(target => ({
    name: target, build: (downloader: InstallerDownloader) => buildCoreInstallCommand(quotedUrl, target, downloader), args: `--core ${target}`,
  })),
]

describe.each(['curl', 'wget'] as const)('short %s installation commands', downloader => {
  it.each(variants)('downloads completely before running $name, preserving URL, arguments and stdin', ({ build, args }) => {
    const test = fixture()
    const command = build(downloader)
    expect(command).not.toMatch(/[\r\n]/)
    const result = test.run(command)
    expect(result.status, result.stderr).toBe(0)
    expect(test.read('url')).toBe(quotedUrl)
    expect(test.read('downloader')).toBe(downloader)
    expect(test.read('arguments')).toBe(`${args}\n`)
    expect(test.read('input')).toBe('interactive-choice')
    expect(test.read('result')).toBe('executed')
    expect(test.exists('download')).toBe(false)
  })

  it('never executes a failed or partial download, and removes the temporary file', () => {
    const test = fixture()
    const result = test.run(buildAgentInstallCommand(quotedUrl, downloader), { CORE_TEST_DOWNLOAD_STATUS: '22' })
    expect(result.status).toBe(22)
    expect(test.exists('download-complete')).toBe(false)
    expect(test.exists('result')).toBe(false)
    expect(test.exists('download')).toBe(false)
  })

  it('preserves installation failure and still removes the temporary file', () => {
    const test = fixture()
    const result = test.run(buildCoreInstallCommand(quotedUrl, 'oxidns', downloader), { CORE_TEST_INSTALL_STATUS: '19' })
    expect(result.status).toBe(19)
    expect(test.read('result')).toBe('executed')
    expect(test.exists('download')).toBe(false)
  })

  it('stops if the selected downloader is missing without silently changing tools', () => {
    const test = fixture({ missingDownloader: downloader })
    const result = test.run(buildAgentCoreInstallCommand(quotedUrl, downloader))
    expect(result.status).toBe(127)
    expect(test.exists('url')).toBe(false)
    expect(test.exists('result')).toBe(false)
    expect(test.exists('download')).toBe(false)
  })
})

describe('short installation command defaults', () => {
  it('uses curl by default', () => {
    const test = fixture()
    expect(test.run(buildCoreInstallCommand(quotedUrl)).status).toBe(0)
    expect(test.read('downloader')).toBe('curl')
  })

  it('does not download or execute if a private temporary file cannot be created', () => {
    const test = fixture({ tempFailure: true })
    expect(test.run(buildAgentInstallCommand(quotedUrl)).status).toBe(9)
    expect(test.exists('url')).toBe(false)
    expect(test.exists('result')).toBe(false)
  })

  it('rejects unsupported core and downloader choices', () => {
    expect(() => buildCoreInstallCommand(quotedUrl, 'mihomo; injected' as CoreInstallTarget)).toThrow('请选择要安装的核心')
    expect(() => buildAgentInstallCommand(quotedUrl, 'curl; injected' as InstallerDownloader)).toThrow('请选择 curl 或 wget')
  })
})
