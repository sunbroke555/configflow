import type { InjectionKey, Ref } from "vue"
import { computed, inject } from "vue"

/** Tabs 的 keep-visited 开关，由 TabsContent 读取。 */
export const TABS_KEEP_VISITED: InjectionKey<Readonly<Ref<boolean>>> = Symbol("tabs-keep-visited")

/** TabsContent 下发：所在标签（含外层标签）是否为当前标签。 */
export const TABS_CONTENT_ACTIVE: InjectionKey<Readonly<Ref<boolean>>> = Symbol("tabs-content-active")

/**
 * 组件所在标签是否为当前标签。放在 Tabs 里时取 TabsContent 注入的值：切换标签不经过 props，
 * 保持挂载的隐藏面板不会因为 active 变化整组件重新渲染；单独使用时取 fallback。
 */
export function useTabActive(fallback: () => boolean = () => true): Readonly<Ref<boolean>> {
  return inject(TABS_CONTENT_ACTIVE, null) ?? computed(fallback)
}
