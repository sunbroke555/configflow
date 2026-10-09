import { createApp } from 'vue'
import 'vue-sonner/style.css'
import './styles/theme.css'
import './styles/tokens.css'
import router from './router'
import './stores/theme'
import App from './App.vue'

const app = createApp(App).use(router)
router.isReady().then(() => app.mount('#app'))
