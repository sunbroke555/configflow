import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount, type VueWrapper } from '@vue/test-utils'
import ForwardFilterNotice from '@/components/agents/ForwardFilterNotice.vue'
import { copyText } from '@/lib/clipboard'
import { notify } from '@/lib/feedback'
import { DOCKER_COMMANDS, DOCKER_HINT, NFT_COMMANDS, VERIFY, mayDrop, quiet } from './fixtures/forward-filter'

vi.mock('@/lib/clipboard', () => ({ copyText: vi.fn() }))
vi.mock('@/lib/feedback', () => ({ notify: { success: vi.fn(), error: vi.fn() } }))

const wrappers: VueWrapper[] = []
const render = (filter: unknown) => {
  const wrapper = mount(ForwardFilterNotice, { props: { filter: filter as any } })
  wrappers.push(wrapper)
  return wrapper
}
beforeEach(() => { vi.mocked(copyText).mockResolvedValue(undefined) })
afterEach(() => { wrappers.splice(0).forEach(wrapper => wrapper.unmount()); vi.resetAllMocks() })

describe('forward filter notice', () => {
  it('shows the hint, each command group in a monospace block and the verification note', () => {
    const wrapper = render(mayDrop())
    expect(wrapper.text()).toContain(DOCKER_HINT)
    expect(wrapper.text()).toContain(VERIFY)
    const blocks = wrapper.findAll('pre')
    expect(blocks.map(block => block.text())).toEqual([DOCKER_COMMANDS.join('\n'), NFT_COMMANDS.join('\n')])
    for (const block of blocks) expect(block.classes()).toEqual(expect.arrayContaining(['font-mono', 'whitespace-pre', 'overflow-x-auto']))
    expect(wrapper.text()).toContain('在这台 Linux 上执行（Docker）')
    expect(wrapper.text()).toContain('重启 Docker 不会清空 DOCKER-USER')
    // Never painted as one warning block.
    expect(wrapper.get('[data-forward-filter-notice]').classes().join(' ')).not.toMatch(/warning/)
  })

  it('puts a ghost copy button on each label row and copies the exact commands', async () => {
    const wrapper = render(mayDrop())
    const group = wrapper.findAll('[data-forward-filter-group]')[1]!
    const copy = group.get('button[aria-label="复制放行命令（nftables inet filter forward）"]')
    expect(copy.attributes('data-variant')).toBe('ghost')
    expect(copy.attributes('data-size')).toBe('sm')
    expect(copy.element.parentElement!.textContent).toContain('在这台 Linux 上执行（nftables inet filter forward）')
    await copy.trigger('click'); await flushPromises()
    expect(copyText).toHaveBeenCalledExactlyOnceWith(`nft 'insert rule inet filter forward oifname "Meta" accept'\nnft 'insert rule inet filter forward iifname "Meta" accept'`)
    expect(notify.success).toHaveBeenCalledWith('放行命令已复制。')
  })

  it('reports a failed copy', async () => {
    vi.mocked(copyText).mockRejectedValue(new Error('denied'))
    const wrapper = render(mayDrop())
    await wrapper.get('button[aria-label="复制放行命令（Docker）"]').trigger('click'); await flushPromises()
    expect(notify.error).toHaveBeenCalledWith('复制失败，请手动选择命令。')
    expect(notify.success).not.toHaveBeenCalled()
  })

  it('keeps detected sources in collapsed details', () => {
    const details = render(mayDrop()).get('details')
    expect(details.attributes('open')).toBeUndefined()
    expect(details.text()).toContain('iptables FORWARD 默认策略为 DROP')
  })

  it.each([undefined, null, quiet('ok'), quiet('unknown', 'timeout'), quiet('not_applicable', 'linux_gateway')])('renders nothing for %j', filter => {
    expect(render(filter).html()).not.toContain('data-forward-filter-notice')
  })
})
