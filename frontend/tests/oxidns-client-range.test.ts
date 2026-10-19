// @vitest-environment node
import { describe, expect, it } from 'vitest'
import { normalizeClientRange } from '@/lib/oxidnsClientRange'

describe('OxiDNS client IP and CIDR input', () => {
  it.each([
    ['192.168.50.51/24', '192.168.50.0/24'], [' 192.168.50.51/32 ', '192.168.50.51'],
    ['2001:0DB8:abcd::1234/48', '2001:db8:abcd::/48'], ['2001:db8::1/128', '2001:db8::1'],
    ['::ffff:192.168.50.51/120', '192.168.50.0/24'], ['::ffff:c0a8:3233', '192.168.50.51'],
    ['::ffff:192.168.50.51/128', '192.168.50.51'], ['::ffff:192.168.50.51/96', '0.0.0.0/0'],
    ['0.0.0.0/0', '0.0.0.0/0'], ['::/0', '::/0'], ['192.168.0.255/31', '192.168.0.254/31'],
    ['2001:db8::ffff/127', '2001:db8::fffe/127'], ['::1/64', '::/64'],
  ])('normalizes %s to %s before adding', (raw, expected) => {
    expect(normalizeClientRange(raw)).toBe(expected)
  })
  it.each(['example.com/24', '192.168.0.1/-1', '192.168.0.1/33', '192.168.0.1/24/1',
    '192.168.0.1/255.255.255.0', '192.168.0.1/', '2001:db8::1/129', 'fe80::1%eth0/64',
    '::ffff:192.168.0.1/95', '192.168.001.1/24', '224.0.0.0/4', 'ff00::/8',
    '0.0.0.0/32', '::/128', '192.168.0.1/２４', '192.168.0.1/24\nexec accept',
    '::ffff:192.168.001.1/120', '1.2.3', '1.2.3.4/00024', '1:2:3:4:5:6:7:8:9/64',
  ])('rejects invalid client range %s', raw => {
    expect(() => normalizeClientRange(raw)).toThrow()
  })
})
