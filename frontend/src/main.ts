import { createApp } from 'vue'
import 'vue-sonner/style.css'
import './styles/theme.css'
import './styles/tokens.css'
import router, { pageLoadRecovery } from './router'
import './stores/theme'
import App from './App.vue'
import PageLoadFailure from './components/PageLoadFailure.vue'

// 滚动条只在滚动时显示：滚动期间给滚动容器打标记，停止滚动 800ms 后移除
const scrollTimers = new WeakMap<Element, number>()
document.addEventListener('scroll', event => {
  const el = event.target === document ? document.documentElement : event.target
  if (!(el instanceof Element)) return
  if (!el.hasAttribute('data-scrolling')) el.setAttribute('data-scrolling', '')
  window.clearTimeout(scrollTimers.get(el))
  scrollTimers.set(el, window.setTimeout(() => el.removeAttribute('data-scrolling'), 800))
}, { capture: true, passive: true })

const app = createApp(App).use(router)
router.isReady().then(() => app.mount('#app')).catch(async () => {
  if (await pageLoadRecovery !== 'reloading') createApp(PageLoadFailure).mount('#app')
})
