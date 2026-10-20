import { describe, expect, it } from 'vitest'
import { mount } from '@vue/test-utils'
import AgentLogLines from '@/components/agents/AgentLogLines.vue'
import { parseAgentLogLine, parseAgentLogs, splitFields } from '@/lib/agentLogs'

const OXIDNS_DOWNLOAD = '2026-10-20T23:22:20+08:00 T+341494.611 INFO oxidns::plugin::executor::download:184:download completed plugin=rules_download url=https://cf.example/api/profiles/default/mosdns/rule-proxy?url=https%3A%2F%2Fcf.example%2Fx&part=domain&token=[REDACTED] target=rules/rule_27b8_domain.txt timeout_ms=60000 socks5=""'
const OXIDNS_WARN = '2026-10-20T23:48:57+08:00 T+343092.132 WARN oxidns::plugin::server::udp:232:Error receiving message on UDP socket: DNS protocol error: dns compression pointer exceeds packet length'
const MIHOMO = 'time="2026-10-21T00:01:02.123456789+08:00" level=warning msg="[TCP] dial Proxy (match RuleSet(google)) 192.168.0.5:51234 --> www.google.com:443 error: i/o timeout"'
const AGENT = '2026/10/20 23:39:47 Heartbeat sent successfully (service status:  -> active)'

describe('agent log parsing', () => {
  it('splits OxiDNS tracing lines into time, level, source, message and fields', () => {
    const entry = parseAgentLogLine(OXIDNS_DOWNLOAD)
    expect(entry).toMatchObject({
      date: '2026-10-20',
      time: '23:22:20',
      level: 'INFO',
      source: 'oxidns::plugin::executor::download:184',
      sourceShort: 'download:184',
      message: 'download completed',
      continuation: false
    })
    expect(entry.fields).toEqual([
      { key: 'plugin', value: 'rules_download' },
      { key: 'url', value: 'https://cf.example/api/profiles/default/mosdns/rule-proxy?url=https%3A%2F%2Fcf.example%2Fx&part=domain&token=[REDACTED]' },
      { key: 'target', value: 'rules/rule_27b8_domain.txt' },
      { key: 'timeout_ms', value: '60000' },
      { key: 'socks5', value: '' }
    ])
  })

  it('keeps colons inside OxiDNS messages and normalizes WARN', () => {
    expect(parseAgentLogLine(OXIDNS_WARN)).toMatchObject({
      level: 'WARN',
      sourceShort: 'udp:232',
      message: 'Error receiving message on UDP socket: DNS protocol error: dns compression pointer exceeds packet length',
      fields: []
    })
  })

  it('reads Mihomo logrus lines', () => {
    expect(parseAgentLogLine(MIHOMO)).toMatchObject({
      date: '2026-10-21',
      time: '00:01:02',
      level: 'WARN',
      message: '[TCP] dial Proxy (match RuleSet(google)) 192.168.0.5:51234 --> www.google.com:443 error: i/o timeout'
    })
  })

  it('reads Go Agent lines without inventing a level', () => {
    expect(parseAgentLogLine(AGENT)).toMatchObject({
      date: '2026-10-20',
      time: '23:39:47',
      level: '',
      message: 'Heartbeat sent successfully (service status:  -> active)'
    })
  })

  it('keeps unknown lines as continuations and drops blank lines', () => {
    const entries = parseAgentLogs(`${AGENT}\n\n  goroutine 1 [running]:\r\n`)
    expect(entries).toHaveLength(2)
    expect(entries[1]).toMatchObject({ continuation: true, message: '  goroutine 1 [running]:', time: '' })
  })

  it('leaves prose with a later key=value untouched', () => {
    expect(splitFields('set a=1 then restarted')).toEqual({ message: 'set a=1 then restarted', fields: [] })
  })
})

describe('AgentLogLines', () => {
  it('renders rows with a date divider, level column and wrapped fields', () => {
    const wrapper = mount(AgentLogLines, { props: { text: [OXIDNS_DOWNLOAD, OXIDNS_WARN, MIHOMO].join('\n') } })
    expect(wrapper.findAll('.log-date').map(node => node.text())).toEqual(['2026-10-20', '2026-10-21'])
    expect(wrapper.findAll('.log-row')).toHaveLength(3)
    expect(wrapper.find('.lv-row-WARN .log-source').attributes('title')).toBe('oxidns::plugin::server::udp:232')
    expect(wrapper.findAll('.log-key').map(node => node.text())).toContain('url=')
  })

  it('hides the level column for Agent logs and shows notices first', () => {
    const plain = mount(AgentLogLines, { props: { text: AGENT } })
    expect(plain.find('ol').classes()).toContain('no-levels')
    expect(plain.find('.log-level').exists()).toBe(false)

    const notice = mount(AgentLogLines, { props: { text: AGENT, notice: '获取日志失败: 超时' } })
    expect(notice.text()).toBe('获取日志失败: 超时')
    expect(mount(AgentLogLines, { props: { text: '' } }).text()).toBe('该日志暂无内容')
  })
})
