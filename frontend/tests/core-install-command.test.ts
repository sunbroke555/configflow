// @vitest-environment node
import { afterEach, describe, expect, it } from 'vitest'
import { spawnSync } from 'node:child_process'
import { existsSync, mkdirSync, mkdtempSync, readFileSync, rmSync, symlinkSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { AGENT_CORE_INSTALL_INTERFACE, buildAgentCoreInstallCommand, buildCoreInstallCommand, type CoreInstallTarget } from '@/lib/coreInstallCommand'

const directories: string[] = []
const quotedUrl = "https://config.example.test/base'$(printf_injected)`ignored`/core-install-script"
const packageManagers = ['apt-get', 'apk', 'dnf', 'yum', 'pacman', 'zypper'] as const
type PackageManager = typeof packageManagers[number]

function fixture(options: { downloader?: 'curl' | 'wget' | 'none'; missingTempTools?: boolean; manager?: PackageManager; uid?: string; os?: string; combined?: boolean } = {}) {
  const interfaceMarker = options.combined ? AGENT_CORE_INSTALL_INTERFACE : '# ConfigFlow core installer interface: 1'
  const directory = mkdtempSync(join(tmpdir(), 'configflow-bootstrap-'))
  directories.push(directory)
  const bin = join(directory, 'bin')
  mkdirSync(bin)
  const script = (name: string, content: string, location = bin) => writeFileSync(join(location, name), `#!/bin/sh\n${content}\n`, { mode: 0o755 })
  symlinkSync('/bin/sh', join(bin, 'sh'))
  script('id', `printf '%s\\n' '${options.uid ?? '0'}'`)
  script('uname', `printf '%s\\n' '${options.os ?? 'Linux'}'`)
  script('staged-mktemp', ': > "$CORE_TEST_DIR/download"; printf "%s\\n" "$CORE_TEST_DIR/download"', directory)
  script('staged-rm', 'exec /bin/rm "$@"', directory)
  for (const downloader of ['curl', 'wget']) {
    script(`staged-${downloader}`, `
core_output=${downloader === 'curl' ? '$3' : '$2'}
core_url=${downloader === 'curl' ? '$4' : '$3'}
printf '%s' "$core_url" > "$CORE_TEST_DIR/url"
case "\${CORE_TEST_HEADER_MODE:-current}" in
  old) printf '%s\\n' '#!/bin/sh' > "$core_output";;
  truncated) printf '%s\\n%s' '#!/bin/sh' '${interfaceMarker}' > "$core_output"; exit 0;;
  future) printf '%s\\n' '#!/bin/sh' '# ConfigFlow core installer interface: 2' > "$core_output";;
  *) printf '%s\\n' '#!/bin/sh' '${interfaceMarker}' > "$core_output";;
esac
printf '%s\\n' 'printf "%s\\n" executed > "$CORE_TEST_DIR/result"' >> "$core_output"
printf '%s\\n' 'printf "%s\\n" "$*" > "$CORE_TEST_DIR/arguments"' >> "$core_output"
printf '%s\\n' 'exit "\${CORE_TEST_INSTALL_STATUS:-0}"' >> "$core_output"
if [ -n "\${CORE_TEST_SIGNAL:-}" ]; then kill -"$CORE_TEST_SIGNAL" "$PPID"; fi
exit "\${CORE_TEST_DOWNLOAD_STATUS:-0}"
`, directory)
  }
  if (!options.missingTempTools) {
    symlinkSync(join(directory, 'staged-mktemp'), join(bin, 'mktemp'))
    symlinkSync(join(directory, 'staged-rm'), join(bin, 'rm'))
  }
  const downloader = options.downloader ?? 'curl'
  if (downloader !== 'none') symlinkSync(join(directory, `staged-${downloader}`), join(bin, downloader))
  if (options.manager) {
    script(options.manager, `
printf '%s:%s:%s\\n' '${options.manager}' "\${DEBIAN_FRONTEND:-}" "$*" >> "$CORE_TEST_DIR/packages"
if [ "\${CORE_TEST_PACKAGE_FAIL:-}" = yes ] || { [ "\${CORE_TEST_UPDATE_FAIL:-}" = yes ] && [ "$1" = update ]; }; then exit 17; fi
[ "$1" != update ] || exit 0
[ "\${CORE_TEST_NO_TOOLS:-}" != yes ] || exit 0
for core_package in "$@"; do
  case "$core_package" in
    curl) /bin/cp "$CORE_TEST_DIR/staged-curl" "$CORE_TEST_DIR/bin/curl";;
    coreutils) /bin/cp "$CORE_TEST_DIR/staged-mktemp" "$CORE_TEST_DIR/bin/mktemp"; /bin/cp "$CORE_TEST_DIR/staged-rm" "$CORE_TEST_DIR/bin/rm";;
  esac
done
`)
  }
  return {
    directory,
    run(extraEnv: Record<string, string> = {}, target: CoreInstallTarget = 'both') {
      // PATH contains only test doubles: package operations can never touch the host.
      return spawnSync('/bin/sh', ['-c', options.combined ? buildAgentCoreInstallCommand(quotedUrl) : buildCoreInstallCommand(quotedUrl, target)], {
        env: { PATH: bin, CORE_TEST_DIR: directory, ...extraEnv }, encoding: 'utf8', timeout: 5000,
      })
    },
    read(name: string) { return readFileSync(join(directory, name), 'utf8') },
    exists(name: string) { return existsSync(join(directory, name)) },
  }
}

afterEach(() => {
  for (const directory of directories.splice(0)) rmSync(directory, { recursive: true, force: true })
})

describe('standalone core installer bootstrap', () => {
  it('is one physical line and valid POSIX shell', () => {
    const command = buildCoreInstallCommand(quotedUrl)
    expect(command).not.toMatch(/[\r\n]/)
    expect(spawnSync('/bin/sh', ['-n', '-c', command]).status).toBe(0)
    expect(command).not.toMatch(/\|\s*sh|<\(/)
  })

  it.each(['both', 'mihomo', 'oxidns'] as const)('passes only the selected %s target to the installer', target => {
    const test = fixture()
    expect(test.run({}, target).status).toBe(0)
    expect(test.read('arguments')).toBe(`--core ${target}\n`)
  })

  it('rejects unrecognized target arguments', () => {
    expect(() => buildCoreInstallCommand(quotedUrl, 'mihomo; injected' as CoreInstallTarget)).toThrow('请选择要安装的核心')
  })

  it.each(['curl', 'wget'] as const)('uses existing %s, preserves the literal URL, and removes the downloaded file', downloader => {
    const test = fixture({ downloader, manager: 'apt-get' })
    expect(test.run().status).toBe(0)
    expect(test.read('url')).toBe(quotedUrl)
    expect(test.read('result').trim()).toBe('executed')
    expect(test.exists('download')).toBe(false)
    expect(test.exists('packages')).toBe(false)
  })

  it.each(packageManagers)('installs a missing downloader with %s before downloading', manager => {
    const test = fixture({ downloader: 'none', manager })
    const result = test.run()
    expect(result.status, result.stderr).toBe(0)
    expect(test.read('packages')).toContain('curl ca-certificates')
    const expected: Record<PackageManager, string> = {
      'apt-get': 'apt-get::update\napt-get:noninteractive:install -y --no-install-recommends curl ca-certificates\n',
      apk: 'apk::add --no-cache curl ca-certificates\n',
      dnf: 'dnf::install -y curl ca-certificates\n',
      yum: 'yum::install -y curl ca-certificates\n',
      pacman: 'pacman::-S --needed --noconfirm curl ca-certificates\n',
      zypper: 'zypper::--non-interactive install --no-recommends curl ca-certificates\n',
    }
    expect(test.read('packages')).toBe(expected[manager])
    expect(test.read('result').trim()).toBe('executed')
  })

  it('installs only coreutils when an existing downloader lacks temporary-file tools', () => {
    const test = fixture({ downloader: 'wget', missingTempTools: true, manager: 'apk' })
    const result = test.run()
    expect(result.status, result.stderr).toBe(0)
    expect(test.read('packages')).toBe('apk::add --no-cache coreutils\n')
    expect(test.read('result').trim()).toBe('executed')
    expect(test.exists('download')).toBe(false)
  })

  it('installs all missing bootstrap tools in one package operation', () => {
    const test = fixture({ downloader: 'none', missingTempTools: true, manager: 'apk' })
    expect(test.run().status).toBe(0)
    expect(test.read('packages')).toBe('apk::add --no-cache curl ca-certificates coreutils\n')
  })

  it.each(['curl', 'wget'] as const)('never executes a partial download from %s', downloader => {
    const test = fixture({ downloader })
    const result = test.run({ CORE_TEST_DOWNLOAD_STATUS: '22' })
    expect(result.status).not.toBe(0)
    expect(result.stderr).toContain('下载失败，未执行安装脚本')
    expect(test.read('url')).toBe(quotedUrl)
    expect(test.exists('result')).toBe(false)
    expect(test.exists('download')).toBe(false)
  })

  it.each(['old', 'truncated', 'future'])('rejects a %s installer interface without executing it', mode => {
    const test = fixture()
    const result = test.run({ CORE_TEST_HEADER_MODE: mode }, 'oxidns')
    expect(result.status).not.toBe(0)
    expect(result.stderr).toContain('安装脚本版本不支持单核心选择')
    expect(test.exists('result')).toBe(false)
    expect(test.exists('download')).toBe(false)
  })

  it('propagates installer failure and still removes the temporary file', () => {
    const test = fixture()
    expect(test.run({ CORE_TEST_INSTALL_STATUS: '19' }).status).toBe(19)
    expect(test.read('result').trim()).toBe('executed')
    expect(test.exists('download')).toBe(false)
  })

  it.each(['HUP', 'INT', 'TERM'])('cleans up and stops before execution on %s', signal => {
    const test = fixture()
    expect(test.run({ CORE_TEST_SIGNAL: signal }).status).not.toBe(0)
    expect(test.exists('result')).toBe(false)
    expect(test.exists('download')).toBe(false)
  })

  it.each(packageManagers)('does not download or execute after %s fails', manager => {
    const test = fixture({ downloader: 'none', manager })
    const result = test.run({ CORE_TEST_PACKAGE_FAIL: 'yes' })
    expect(result.status).not.toBe(0)
    expect(result.stderr).toContain('依赖安装失败')
    expect(test.exists('url')).toBe(false)
    expect(test.exists('result')).toBe(false)
  })

  it('does not run apt installation if refreshing package metadata fails', () => {
    const test = fixture({ downloader: 'none', manager: 'apt-get' })
    expect(test.run({ CORE_TEST_UPDATE_FAIL: 'yes' }).status).not.toBe(0)
    expect(test.read('packages')).toBe('apt-get::update\n')
    expect(test.exists('url')).toBe(false)
  })

  it('rechecks tools even when the package manager reports success', () => {
    const test = fixture({ downloader: 'none', manager: 'apk' })
    const result = test.run({ CORE_TEST_NO_TOOLS: 'yes' })
    expect(result.status).not.toBe(0)
    expect(result.stderr).toContain('仍缺少 curl 或 wget')
    expect(test.exists('url')).toBe(false)
  })

  it('explains missing dependencies when no supported package manager exists', () => {
    const test = fixture({ downloader: 'none' })
    const result = test.run()
    expect(result.status).not.toBe(0)
    expect(result.stderr).toContain('手动安装缺少的依赖：curl ca-certificates')
    expect(test.exists('url')).toBe(false)
  })

  it.each([{ uid: '1000', expected: 'root' }, { os: 'Darwin', expected: '仅支持 Linux' }])('rejects unsupported privilege/platform before installing dependencies: %o', ({ expected, ...options }) => {
    const test = fixture({ ...options, downloader: 'none', manager: 'apk' })
    const result = test.run()
    expect(result.status).not.toBe(0)
    expect(result.stderr).toContain(expected)
    expect(test.exists('packages')).toBe(false)
    expect(test.exists('url')).toBe(false)
  })
})


describe('combined Agent and core installer bootstrap', () => {
  it.each(['curl', 'wget'] as const)('fully downloads via %s and runs without core arguments', downloader => {
    const test = fixture({ combined: true, downloader })
    const command = buildAgentCoreInstallCommand(quotedUrl)
    expect(command).not.toMatch(/[\r\n]/)
    expect(spawnSync('/bin/sh', ['-n', '-c', command]).status).toBe(0)
    expect(test.run().status).toBe(0)
    expect(test.read('url')).toBe(quotedUrl)
    expect(test.read('arguments')).toBe('\n')
    expect(test.exists('result')).toBe(true)
    expect(test.exists('download')).toBe(false)
  })

  it('installs missing fetch dependencies before the combined download', () => {
    const test = fixture({ combined: true, downloader: 'none', manager: 'apk' })
    expect(test.run().status).toBe(0)
    expect(test.read('packages')).toContain('curl ca-certificates')
    expect(test.exists('result')).toBe(true)
  })

  it.each(['curl', 'wget'] as const)('does not execute failed %s downloads', downloader => {
    const test = fixture({ combined: true, downloader })
    expect(test.run({ CORE_TEST_DOWNLOAD_STATUS: '22' }).status).not.toBe(0)
    expect(test.exists('result')).toBe(false)
    expect(test.exists('download')).toBe(false)
  })

  it.each(['old', 'truncated', 'future'])('rejects a %s interface and removes it', mode => {
    const test = fixture({ combined: true })
    const result = test.run({ CORE_TEST_HEADER_MODE: mode })
    expect(result.status).not.toBe(0)
    expect(result.stderr).toContain('安装脚本版本不支持同时安装核心')
    expect(test.exists('result')).toBe(false)
    expect(test.exists('download')).toBe(false)
  })

  it('preserves combined installer failure and cleans up the temporary file', () => {
    const test = fixture({ combined: true })
    expect(test.run({ CORE_TEST_INSTALL_STATUS: '19' }).status).toBe(19)
    expect(test.exists('result')).toBe(true)
    expect(test.exists('download')).toBe(false)
  })
})
