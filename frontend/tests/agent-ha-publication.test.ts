import { afterEach, describe, expect, it } from 'vitest'
import { mount } from '@vue/test-utils'
import AgentDeploymentStatus from '@/components/agents/AgentDeploymentStatus.vue'
import type { AgentHAPublication } from '@/types/agentHAPublication'
import type { AgentDeployment } from '@/composables/useAgentDeployments'

const wrappers: ReturnType<typeof mount>[] = []
afterEach(() => wrappers.splice(0).forEach(wrapper => wrapper.unmount()))
const publication: AgentHAPublication = {
  id: 'pair-handoff', status: 'checking', phase: 'handoff', owner_agent_id: 'original-holder',
  owner_deployment_id: 'holder-release', active_agent_id: 'new-holder',
  members: [
    { agent_id: 'new-holder', name: '接管设备', deployment_id: 'standby-release', status: 'succeeded' },
    { agent_id: 'original-holder', name: '原持有设备', deployment_id: 'holder-release', status: 'waiting' },
  ],
}

describe('planned HA handoff progress across publication surfaces', () => {
  it.each([false, true])('keeps the handoff visible after the first device succeeds (compact=%s)', compact => {
    const task: AgentDeployment = { deployment_id: 'holder-release', status: 'checking', ha_publication: publication }
    const wrapper = mount(AgentDeploymentStatus, { props: { task, compact } })
    wrappers.push(wrapper)
    expect(wrapper.get('[data-ha-publication]').text()).toContain('迁移虚拟 IP 并确认解析')
    expect(wrapper.get('[data-ha-publication]').text()).toContain('接管设备：发布成功')
    expect(wrapper.get('[data-ha-publication]').text()).toContain('原持有设备：等待前一台完成')
    expect(wrapper.text()).toContain('正在迁移虚拟 IP')
    if (compact) {
      expect(wrapper.get('[data-agent-status-row]').attributes('data-tone')).toBe('progress')
      expect(wrapper.get('[data-status-notice] [data-ha-publication]').exists()).toBe(true)
    }
  })

  it('shows the server handoff failure without masking the stopped second device', () => {
    const task: AgentDeployment = { deployment_id: 'holder-release', status: 'failed', ha_publication: {
      ...publication, status: 'failed', message: '虚拟 IP 解析检查失败，原持有设备未更新',
      members: [publication.members[0], { ...publication.members[1], status: 'skipped' }],
    } }
    const wrapper = mount(AgentDeploymentStatus, { props: { task, compact: true } })
    wrappers.push(wrapper)
    expect(wrapper.get('[data-agent-status-row]').attributes('data-tone')).toBe('danger')
    expect(wrapper.get('[data-status-notice]').text()).toContain('虚拟 IP 解析检查失败')
    expect(wrapper.get('[data-status-notice]').text()).toContain('原持有设备：未执行')
  })
})
