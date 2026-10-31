import type { InjectionKey, Ref } from "vue"

/** Tabs 的 keep-visited 开关，由 TabsContent 读取。 */
export const TABS_KEEP_VISITED: InjectionKey<Readonly<Ref<boolean>>> = Symbol("tabs-keep-visited")
