import { describe, expect, it } from 'vitest'
import { shallowMount } from '@vue/test-utils'
import SetupInstallation from '@/components/setup/SetupInstallation.vue'
import GatewayInstallWizard from '@/components/agents/GatewayInstallWizard.vue'

describe('first-run installation reuses the original gateway controller', () => {
  it.each(['router', 'gateway'] as const)('embeds the existing controller for %s and forwards real completion/busy events', goal => {
    const wrapper = shallowMount(SetupInstallation, { props: { profileId: 'setup_test', goal } })
    const gateway = wrapper.getComponent(GatewayInstallWizard)
    expect(gateway.props()).toMatchObject({ open: true, embedded: true, profileId: 'setup_test', goal })
    gateway.vm.$emit('busy', true)
    gateway.vm.$emit('completed')
    expect(wrapper.emitted('busy')).toEqual([[true]])
    expect(wrapper.emitted('completed')).toEqual([[]])
    wrapper.unmount()
  })
})
