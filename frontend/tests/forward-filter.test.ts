import { describe, expect, it } from 'vitest'
import { forwardFilterSourceLabel, forwardFilterWarning } from '@/lib/forwardFilter'
import { DOCKER_COMMANDS, DOCKER_HINT, NFT_COMMANDS, UFW_COMMANDS, VERIFY, mayDrop, quiet, ufwDrop } from './fixtures/forward-filter'

describe('forward filter warning', () => {
  it('maps a may_drop report to labelled command groups without rewriting the commands', () => {
    const warning = forwardFilterWarning(mayDrop())!
    expect(warning).toMatchObject({ status: '可能拦截 UDP', short: '可能拦截 UDP', hint: DOCKER_HINT, verify: VERIFY })
    expect(warning.groups.map(group => group.label)).toEqual(['在这台 Linux 上执行（Docker）', '在这台 Linux 上执行（nftables inet filter forward）'])
    expect(warning.groups[0]).toMatchObject({ source: 'Docker', commands: DOCKER_COMMANDS, text: DOCKER_COMMANDS.join('\n') })
    expect(warning.groups[1]!.text).toBe(NFT_COMMANDS.join('\n'))
    expect(warning.groups[0]!.persistNote).toContain('DOCKER-USER')
    expect(warning.details).toEqual(['iptables FORWARD 默认策略为 DROP', 'nftables inet filter forward 默认策略为 drop'])
  })

  it('says every forwarded connection may fail without auto-redirect', () => {
    expect(forwardFilterWarning(mayDrop({ auto_redirect: false }))).toMatchObject({ status: '可能拦截转发', short: '可能拦截转发' })
  })

  it.each([
    ['forwarded UDP only', ['forward'], true, '可能拦截 UDP'],
    ['forwarding without auto-redirect (TCP too)', ['forward'], false, '可能拦截转发'],
    // DNS failure breaks everything else, so it wins over every other flow.
    ['forwarded UDP and DNS to this host', ['forward', 'dns'], true, '可能拦截 DNS'],
    ['DNS to this host only', ['dns'], true, '可能拦截 DNS'],
    ['TCP DNS from the TUN and DNS to this host', ['dns', 'tun_tcp'], true, '可能拦截 DNS'],
    ['UFW on a standalone host', ['forward', 'dns', 'redirect_tcp', 'tun_tcp'], true, '可能拦截 DNS'],
    // Lab v6-d-aio: UFW on an all-in-one host; OxiDNS on this host is dropped on INPUT.
    ['UFW on an all-in-one host', ['forward', 'local_dns', 'redirect_tcp'], true, '可能拦截 DNS'],
    ['local DNS only', ['local_dns'], true, '可能拦截 DNS'],
    ['redirected TCP only (INPUT drop)', ['redirect_tcp'], true, '可能拦截 TCP'],
    ['redirected and TUN TCP', ['redirect_tcp', 'tun_tcp'], true, '可能拦截 TCP'],
    ['TCP from the TUN only (firewalld)', ['tun_tcp'], true, '可能拦截 TCP'],
    ['TUN TCP without auto-redirect', ['tun_tcp'], false, '可能拦截 TCP'],
    ['forwarded UDP and redirected TCP', ['forward', 'redirect_tcp'], true, '可能拦截转发'],
    ['an older Agent without flows', undefined, true, '可能拦截 UDP'],
    ['unknown flow names', ['bogus'], false, '可能拦截转发'],
  ])('labels %s', (_case, flows, auto_redirect, label) => {
    expect(forwardFilterWarning(mayDrop({ flows: flows as any, auto_redirect }))).toMatchObject({ status: label, short: label })
  })

  it('keeps every UFW command, including the before.rules line, verbatim', () => {
    const warning = forwardFilterWarning(ufwDrop())!
    expect(warning.status).toBe('可能拦截 DNS')
    expect(warning.groups).toHaveLength(1)
    expect(warning.groups[0]).toMatchObject({ label: '在这台 Linux 上执行（UFW）', commands: UFW_COMMANDS, text: UFW_COMMANDS.join('\n') })
  })

  it('uses only the short labels that fit the routers column', () => {
    const labels = new Set<string>()
    for (const flows of [[], ['forward'], ['dns'], ['local_dns'], ['redirect_tcp'], ['tun_tcp'], ['forward', 'tun_tcp']]) {
      for (const auto_redirect of [true, false]) labels.add(forwardFilterWarning(mayDrop({ flows: flows as any, auto_redirect }))!.short)
    }
    // Four characters plus a three-letter word or two more characters: one line in the 102px column.
    expect([...labels].sort()).toEqual(['可能拦截 DNS', '可能拦截 TCP', '可能拦截 UDP', '可能拦截转发'])
  })

  it.each([
    ['an older Agent', undefined],
    ['a missing report', null],
    ['ok', quiet('ok')],
    ['unknown', quiet('unknown', 'timeout')],
    ['not applicable (selective gateway)', quiet('not_applicable', 'linux_gateway')],
    ['may_drop without a hint', mayDrop({ hint: '' })],
    ['a malformed report', { state: 'may_drop' } as any],
  ])('shows nothing for %s', (_case, filter) => {
    expect(forwardFilterWarning(filter)).toBeNull()
  })

  it('keeps the hint but drops groups without usable commands', () => {
    const warning = forwardFilterWarning(mayDrop({
      device: '',
      remedies: [
        { source: 'docker', label: 'Docker', device: '', commands: [], persist_note: '' },
        { source: 'bogus' as any, label: 'x', device: 'Meta', commands: ['iptables -L'], persist_note: '' },
      ],
    }))!
    expect(warning.hint).toBe(DOCKER_HINT)
    expect(warning.groups).toEqual([])
  })

  it('falls back to the plain source name', () => {
    expect(forwardFilterSourceLabel('ufw')).toBe('UFW')
    expect(forwardFilterSourceLabel('iptables', 'iptables（legacy）')).toBe('iptables（legacy）')
    const warning = forwardFilterWarning(mayDrop({ remedies: [{ source: 'firewalld', label: '', device: 'Meta', commands: ['firewall-cmd --permanent --zone=trusted --add-interface=Meta && firewall-cmd --reload'], persist_note: '' }] }))!
    expect(warning.groups[0]!.label).toBe('在这台 Linux 上执行（firewalld）')
  })
})
