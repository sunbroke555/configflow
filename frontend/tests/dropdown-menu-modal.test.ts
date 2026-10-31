import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount, type VueWrapper } from '@vue/test-utils'
import { defineComponent, ref } from 'vue'
import { DialogRoot, DialogTitle } from 'reka-ui'
import DialogContent from '@/components/ui/dialog/DialogContent.vue'
import { DropdownMenu, DropdownMenuContent, DropdownMenuItem, DropdownMenuTrigger } from '@/components/ui/dropdown-menu'

const wrappers: VueWrapper[] = []
const menu = () => document.querySelector<HTMLElement>('[role="menu"]')
const pointerDown = (target: EventTarget) => target.dispatchEvent(new MouseEvent('pointerdown', { bubbles: true, button: 0, cancelable: true }))
// reka 在挂载后的下一个宏任务才开始监听外部 pointerdown
const nextMacrotask = () => new Promise(resolve => setTimeout(resolve, 0))

beforeEach(() => {
  vi.stubGlobal('ResizeObserver', class { observe() {} unobserve() {} disconnect() {} })
})

afterEach(async () => {
  wrappers.splice(0).forEach(wrapper => wrapper.unmount())
  await settle()
  document.body.innerHTML = ''
  document.body.removeAttribute('style')
  vi.unstubAllGlobals()
})

async function settle() {
  await nextMacrotask()
  await flushPromises()
}

function mountMenu(attrs = '') {
  const open = ref(false)
  const onSelect = vi.fn()
  const wrapper = mount(defineComponent({
    components: { DropdownMenu, DropdownMenuContent, DropdownMenuItem, DropdownMenuTrigger },
    setup: () => ({ open, onSelect }),
    template: `<div>
      <button type="button" data-testid="outside">页面其他按钮</button>
      <DropdownMenu v-model:open="open" ${attrs}>
        <DropdownMenuTrigger as-child><button type="button">更多</button></DropdownMenuTrigger>
        <DropdownMenuContent><DropdownMenuItem @select="onSelect">调整顺序</DropdownMenuItem></DropdownMenuContent>
      </DropdownMenu>
    </div>`,
  }), { attachTo: document.body })
  wrappers.push(wrapper)
  return { open, onSelect }
}

describe('DropdownMenu modal default', () => {
  it('does not lock body pointer events or scrolling while open', async () => {
    const { open } = mountMenu()
    open.value = true
    await settle()
    expect(menu()).toBeTruthy()
    expect(document.body.style.pointerEvents).toBe('')
    expect(document.body.style.overflow).toBe('')

    pointerDown(document.querySelector('[data-testid="outside"]')!)
    await settle()
    expect(open.value).toBe(false)
  })

  it('keeps the modal lock when a menu opts in explicitly', async () => {
    const { open } = mountMenu(':modal="true"')
    open.value = true
    await settle()
    expect(menu()).toBeTruthy()
    expect(document.body.style.pointerEvents).toBe('none')

    open.value = false
    await settle()
    expect(document.body.style.pointerEvents).toBe('')
  })

  it('selects an item inside a modal dialog without dismissing the dialog', async () => {
    const dialogOpen = ref(true)
    const menuOpen = ref(false)
    const onSelect = vi.fn()
    wrappers.push(mount(defineComponent({
      components: { DialogRoot, DialogContent, DialogTitle, DropdownMenu, DropdownMenuContent, DropdownMenuItem, DropdownMenuTrigger },
      setup: () => ({ dialogOpen, menuOpen, onSelect }),
      template: `<DialogRoot v-model:open="dialogOpen">
        <DialogContent :show-close-button="false" :aria-describedby="undefined">
          <DialogTitle>调整顺序</DialogTitle>
          <DropdownMenu v-model:open="menuOpen">
            <DropdownMenuTrigger as-child><button type="button">更多操作</button></DropdownMenuTrigger>
            <DropdownMenuContent><DropdownMenuItem @select="onSelect">移到顶部</DropdownMenuItem></DropdownMenuContent>
          </DropdownMenu>
        </DialogContent>
      </DialogRoot>`,
    }), { attachTo: document.body }))
    await settle()
    menuOpen.value = true
    await settle()
    // 对话框锁住 body 时，后打开的菜单层仍可点击
    expect(document.body.style.pointerEvents).toBe('none')
    expect(menu()!.closest<HTMLElement>('[data-dismissable-layer]')!.style.pointerEvents).toBe('auto')

    const item = document.querySelector<HTMLElement>('[role="menuitem"]')!
    pointerDown(item)
    item.click()
    await settle()
    expect(onSelect).toHaveBeenCalledTimes(1)
    expect(menuOpen.value).toBe(false)
    expect(dialogOpen.value).toBe(true)
  })
})
