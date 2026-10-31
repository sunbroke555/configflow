// 滚动条只在滚动时显示：滚动期间给滚动容器打 data-scrolling，停止滚动 800ms 后移除。
// 页面本身（根元素）滚动时不打标记：根元素的样式一变，浏览器就要给整页重算样式，
// 规则、节点这类大列表页每次要几百毫秒；根滚动条改由 theme.css 常显一个低对比色。
export function installScrollingMarker() {
  const timers = new WeakMap<Element, ReturnType<typeof setTimeout>>()
  document.addEventListener('scroll', event => {
    const el = event.target
    if (!(el instanceof Element) || el === document.documentElement) return
    if (!el.hasAttribute('data-scrolling')) el.setAttribute('data-scrolling', '')
    clearTimeout(timers.get(el))
    timers.set(el, setTimeout(() => el.removeAttribute('data-scrolling'), 800))
  }, { capture: true, passive: true })
}
