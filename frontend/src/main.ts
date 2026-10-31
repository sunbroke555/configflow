import { createApp } from 'vue'
import 'vue-sonner/style.css'
import './styles/theme.css'
import './styles/tokens.css'
import router, { pageLoadRecovery } from './router'
import './stores/theme'
import App from './App.vue'
import PageLoadFailure from './components/PageLoadFailure.vue'
import { installScrollingMarker } from './lib/scrollingMarker'

installScrollingMarker()

const app = createApp(App).use(router)
router.isReady().then(() => app.mount('#app')).catch(async () => {
  if (await pageLoadRecovery !== 'reloading') createApp(PageLoadFailure).mount('#app')
})
