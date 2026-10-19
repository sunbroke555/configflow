import { describe, expect, it } from 'vitest'
import { buildGatewayNetworkProbeCommand, parseGatewayNetworkProbe } from '@/lib/gatewayNetworkDetection'

const probe = { version: 1, interface: 'ens18', address: '192.168.1.2', lan_cidr: '192.168.1.0/24', upstream_gateway: '192.168.1.1' }

describe('gateway network probe', () => {
  it.each(['eth0', 'enp1s0', 'ens18', 'br0', 'bond0.10', 'wlan0'])('accepts a detected %s interface without a name prefix restriction', name => {
    expect(parseGatewayNetworkProbe(`\n${JSON.stringify({ ...probe, interface: name })}\n`)).toEqual({
      interface: name, address: probe.address, lan_cidr: probe.lan_cidr, upstream_gateway: probe.upstream_gateway,
    })
  })

  it.each([
    '', 'ip: command not found', 'null', '[]', JSON.stringify({ ...probe, version: 2 }),
    JSON.stringify({ ...probe, version: '1' }), JSON.stringify({ ...probe, address: 123 }),
    JSON.stringify({ version: 1, interface: 'ens18' }), ' '.repeat(8192) + '{}',
  ])('rejects incomplete or unsupported output (%s)', text => {
    expect(() => parseGatewayNetworkProbe(text)).toThrow()
  })

  it.each([
    { interface: 'lo' }, { interface: 'ens18;reboot' }, { address: '127.0.0.1' },
    { address: '192.168.1.0' }, { address: '192.168.1.255' }, { address: '192.168.1.1' },
    { upstream_gateway: '192.168.2.1' }, { lan_cidr: '192.168.1.2/24' }, { lan_cidr: '192.168.1.0/31' },
  ])('validates detected data using the gateway constraints: %j', change => {
    expect(() => parseGatewayNetworkProbe(JSON.stringify({ ...probe, ...change }))).toThrow()
  })

  it('fetches the public probe from the configured origin and executes only a successful download', () => {
    const command = buildGatewayNetworkProbeCommand(' https://configflow.example:8443/ ')
    expect(command).toContain("curl -fsSL 'https://configflow.example:8443/api/agents/gateway-network-probe.sh' -o \"$cf_probe\" && sh \"$cf_probe\"")
    expect(command).toContain('mktemp')
    expect(command).toContain('trap \'rm -f "$cf_probe"\' 0')
    expect(command).not.toContain('sudo')
  })

  it('quotes shell metacharacters in an otherwise accepted URL', () => {
    const command = buildGatewayNetworkProbeCommand("https://configflow.exam'ple")
    expect(command).toContain("'https://configflow.exam'\\''ple/api/agents/gateway-network-probe.sh'")
  })

  it.each(['', 'file:///tmp/probe', 'ftp://example.com', 'https://user:secret@example.com',
    'https://example.com/path', 'https://example.com?token=secret', 'https://example.com#fragment',
    'http://localhost:8000', 'http://127.0.0.1:8000', 'http://[::1]:8000'])('rejects an unusable server address (%s)', url => {
    expect(() => buildGatewayNetworkProbeCommand(url)).toThrow()
  })
})
