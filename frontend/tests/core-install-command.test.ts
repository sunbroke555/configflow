// @vitest-environment node
import { afterEach, describe, expect, it } from 'vitest'
import { spawnSync } from 'node:child_process'
import { existsSync, mkdirSync, mkdtempSync, readFileSync, rmSync, symlinkSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { buildInstallCommand, type InstallerDownloader } from '@/lib/coreInstallCommand'

const directories: string[] = []
const quotedUrl = "https://config.example.test/base'$(printf_injected)`ignored`/api/agents/i/test-link"

function fixture(options: { missingDownloader?: InstallerDownloader } = {}) {
  const directory = mkdtempSync(join(tmpdir(), 'configflow-download-'))
  directories.push(directory)
  const bin = join(directory, 'bin')
  mkdirSync(bin)
  const script = (name: string, content: string) => writeFileSync(join(bin, name), `#!/bin/sh\n${content}\n`, { mode: 0o755 })
  symlinkSync('/bin/sh', join(bin, 'sh'))
  for (const downloader of ['curl', 'wget'] as const) {
    if (options.missingDownloader === downloader) continue
    script(downloader, `
core_output=; core_url=
while [ "$#" -gt 0 ]; do
  case "$1" in
    -o|-O) core_output=$2; shift 2;;
    -fsSL) shift;;
    *) core_url=$1; shift;;
  esac
done
printf '%s' "$core_url" > "$CORE_TEST_DIR/url"
printf '%s' '${downloader}' > "$CORE_TEST_DIR/downloader"
printf '%s\\n' '#!/bin/sh' '[ -f "$CORE_TEST_DIR/download-complete" ] || exit 33' 'IFS= read -r answer; printf "%s" "$answer" > "$CORE_TEST_DIR/input"' 'printf executed > "$CORE_TEST_DIR/result"' 'exit "\${CORE_TEST_INSTALL_STATUS:-0}"' > "$core_output"
[ "\${CORE_TEST_DOWNLOAD_STATUS:-0}" = 0 ] || exit "$CORE_TEST_DOWNLOAD_STATUS"
: > "$CORE_TEST_DIR/download-complete"
`)
  }
  return {
    run(command: string, extraEnv: Record<string, string> = {}) {
      // Isolated working directory and PATH: no real download or package installation.
      return spawnSync('/bin/sh', ['-c', command], {
        cwd: directory, env: { PATH: bin, CORE_TEST_DIR: directory, ...extraEnv },
        input: 'interactive-choice\n', encoding: 'utf8', timeout: 5000,
      })
    },
    read(name: string) { return readFileSync(join(directory, name), 'utf8') },
    write(name: string, content: string) { writeFileSync(join(directory, name), content) },
    exists(name: string) { return existsSync(join(directory, name)) },
  }
}

afterEach(() => {
  for (const directory of directories.splice(0)) rmSync(directory, { recursive: true, force: true })
})

describe.each(['curl', 'wget'] as const)('short %s installation commands', downloader => {
  it('downloads completely before running, preserving the literal URL and stdin', () => {
    const test = fixture()
    const command = buildInstallCommand(quotedUrl, downloader)
    expect(command).not.toMatch(/[\r\n]/)
    expect(command.startsWith(`${downloader} `)).toBe(true)
    const result = test.run(command)
    expect(result.status, result.stderr).toBe(0)
    expect(test.read('url')).toBe(quotedUrl)
    expect(test.read('downloader')).toBe(downloader)
    expect(test.read('input')).toBe('interactive-choice')
    expect(test.read('result')).toBe('executed')
    expect(test.exists('configflow-install.sh')).toBe(true)
  })

  it('never executes a failed or partial download', () => {
    const test = fixture()
    const result = test.run(buildInstallCommand(quotedUrl, downloader), { CORE_TEST_DOWNLOAD_STATUS: '22' })
    expect(result.status).toBe(22)
    expect(test.exists('download-complete')).toBe(false)
    expect(test.exists('result')).toBe(false)
  })

  it('does not execute a previously downloaded file if the selected downloader is missing', () => {
    const test = fixture({ missingDownloader: downloader })
    test.write('configflow-install.sh', '#!/bin/sh\nprintf stale > "$CORE_TEST_DIR/result"\n')
    expect(test.run(buildInstallCommand(quotedUrl, downloader)).status).toBe(127)
    expect(test.exists('result')).toBe(false)
    expect(test.exists('url')).toBe(false)
  })

  it('preserves an installation failure exit code', () => {
    const test = fixture()
    expect(test.run(buildInstallCommand(quotedUrl, downloader), { CORE_TEST_INSTALL_STATUS: '19' }).status).toBe(19)
    expect(test.read('result')).toBe('executed')
  })
})

describe('short installation command defaults', () => {
  it('uses curl by default without extra installer options', () => {
    expect(buildInstallCommand('https://config.example.test/api/agents/i/short')).toBe("curl -fsSL 'https://config.example.test/api/agents/i/short' -o configflow-install.sh && sh configflow-install.sh")
  })

  it('rejects unsupported downloaders', () => {
    expect(() => buildInstallCommand(quotedUrl, 'curl; injected' as InstallerDownloader)).toThrow('请选择 curl 或 wget')
  })
})
