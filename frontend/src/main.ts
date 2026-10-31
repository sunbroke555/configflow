import { createApp } from 'vue'
import 'vue-sonner/style.css'
import './styles/theme.css'
import './styles/tokens.css'
import router, { pageLoadRecovery } from './router'
import './stores/theme'
import App from './App.vue'
import PageLoadFailure from './components/PageLoadFailure.vue'
import { installScrollingMarker } from './lib/scrollingMarker'
import { markListItemEntered } from './lib/motion'

installScrollingMarker()

const app = createApp(App).use(router)
router.isReady().then(() => app.mount('#app')).catch(async () => {
  if (await pageLoadRecovery !== 'reloading') createApp(PageLoadFailure).mount('#app')
})

// 列表卡片进场只播一次，见 lib/motion.ts
document.addEventListener('animationend', markListItemEntered)
document.addEventListener('animationcancel', markListItemEntered)
