<template>
  <MotionConfig reduced-motion="user" :skip-animations="reduceMotion">
  <!-- 登录页不套用应用壳 -->
  <template v-if="isLoginPage">
    <router-view />
  </template>

  <div v-else class="cf-workbench relative flex min-h-screen min-h-dvh flex-col bg-background [--cf-shell-header-h:calc(var(--cf-topbar-h)+env(safe-area-inset-top))] max-[700px]:[--cf-shell-header-h:calc(105px+env(safe-area-inset-top))]">
    <a href="#main-content" class="sr-only fixed top-2 left-3 z-40 rounded-lg bg-primary px-4 py-3 font-medium text-primary-foreground focus:not-sr-only focus:outline-none focus:ring-2 focus:ring-ring focus:ring-offset-2">
      跳转到主要内容
    </a>

    <header
      class="sticky top-0 z-30 flex min-h-[calc(var(--cf-topbar-h)+env(safe-area-inset-top))] shrink-0 items-center gap-3 border-b border-border bg-card px-5 pt-[env(safe-area-inset-top)] max-[900px]:gap-2 max-[900px]:px-3 max-[700px]:flex-wrap max-[700px]:pb-2"
    >
      <router-link
        to="/dashboard"
        class="group flex min-h-11 min-w-0 shrink-0 items-center gap-2.5 rounded-lg text-[15px] font-semibold tracking-[-0.015em] text-foreground no-underline focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
      >
        <span class="relative flex size-7 items-center justify-center">
          <span
            class="absolute inset-0 rounded-[9px] bg-linear-to-br from-primary/50 to-accent-2-fill/40 opacity-70 blur-[7px] transition-opacity duration-300 group-hover:opacity-100"
            aria-hidden="true"
          />
          <img src="/icon.png" alt="" class="relative size-6.5 rounded-[8px]" />
        </span>
        <span class="truncate max-[360px]:hidden">ConfigFlow</span>
      </router-link>

      <!-- 命令面板入口：桌面显示快捷键，移动端退化为图标按钮 -->
      <button
        type="button"
        class="ml-3 hidden h-9 min-w-[180px] cursor-pointer items-center gap-2 rounded-lg border border-border bg-background px-3 text-[12.5px] text-muted-foreground transition-colors hover:border-border-strong hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring lg:flex"
        @click="palette?.show()"
      >
        <Search class="size-3.5" aria-hidden="true" />
        <span>快速跳转…</span>
        <kbd class="ml-auto rounded border border-border/70 px-1.5 py-0.5 font-mono text-[10px]">
          {{ metaKeyLabel }}K
        </kbd>
      </button>

      <ProfileSwitcher class="ml-auto max-[700px]:order-last max-[700px]:ml-0 max-[700px]:w-[calc(100%-5rem)]" />
      <Badge variant="outline" class="shrink-0 rounded-md border-border font-mono text-[11px] text-muted-foreground max-[700px]:order-last max-[700px]:ml-auto max-[700px]:max-w-16 max-[700px]:truncate" :title="versionInfo">
        {{ versionInfo }}
      </Badge>

      <div class="flex shrink-0 items-center gap-1 max-[700px]:ml-auto">
        <Button
          variant="ghost"
          size="icon-sm"
          class="size-11 lg:hidden"
          title="快速跳转"
          aria-label="快速跳转"
          @click="palette?.show()"
        >
          <Search class="size-[17px]" />
        </Button>

        <Button
          variant="ghost"
          size="icon-sm"
          class="size-11"
          :title="theme === 'dark' ? '切换到浅色' : '切换到深色'"
          :aria-label="theme === 'dark' ? '切换到浅色' : '切换到深色'"
          @click="toggleTheme"
        >
          <component :is="theme === 'dark' ? Sun : Moon" class="size-[17px]" />
        </Button>

        <Button
          variant="ghost"
          size="icon-sm"
          class="size-11"
          title="查看文档"
          aria-label="查看文档"
          @click="openGithub"
        >
          <FileText class="size-[17px]" />
        </Button>

        <DropdownMenu v-if="showUserInfo">
          <DropdownMenuTrigger as-child>
            <Button variant="ghost" size="icon-sm" class="size-11" :title="username" :aria-label="`用户 ${username}`">
              <User class="size-[17px]" />
            </Button>
          </DropdownMenuTrigger>
          <DropdownMenuContent align="end">
            <DropdownMenuLabel class="text-[12px] font-normal text-muted-foreground">
              已登录 · {{ username }}
            </DropdownMenuLabel>
            <DropdownMenuSeparator />
            <DropdownMenuItem @select="handleCommand('logout')">
              <LogOut class="size-4" />
              <span>退出登录</span>
            </DropdownMenuItem>
          </DropdownMenuContent>
        </DropdownMenu>
      </div>
    </header>

    <div class="relative flex min-h-0 flex-1">
      <AppRail
        class="max-[900px]:hidden"
        :active-path="route.path"
        :profile-name="currentProfileName"
        :subscription-aggregation-enabled="subscriptionAggregationEnabled"
      />

      <main id="main-content" tabindex="-1" class="relative z-10 min-w-0 flex-1 scroll-mt-32 overflow-x-hidden focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-ring">
        <div
          class="mx-auto max-w-(--cf-content-max) px-8 pt-7 pb-10 max-[900px]:px-4 max-[900px]:pt-4 max-[900px]:pb-[calc(env(safe-area-inset-bottom)+var(--cf-tabbar-h)+var(--cf-sp-5))]"
        >
          <MobileGroupNav
            :active-path="route.path"
            :subscription-aggregation-enabled="subscriptionAggregationEnabled"
          />
          <!-- 不做整页过渡：out-in 会在两页之间留一帧空白，观感是闪一下。
               进场动效交给页面内的卡片与列表逐项播放。 -->
          <router-view :key="pageKey" />
        </div>
      </main>
    </div>

    <MobileTabBar :active-scope="activeScope" @select="openGroup" />

    <CommandPalette
      ref="palette"
      :subscription-aggregation-enabled="subscriptionAggregationEnabled"
    />
  </div>

  <Toaster position="top-center" rich-colors close-button :duration="3000" />
  <ConfirmHost />
  <PromptHost />
  </MotionConfig>
</template>

<script setup lang="ts">
import { ref, computed, onMounted, onUnmounted, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { FileText, LogOut, Moon, Search, Sun, User } from '@lucide/vue'
import { useMediaQuery } from '@vueuse/core'
import { MotionConfig } from 'motion-v'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuSeparator,
  DropdownMenuTrigger
} from '@/components/ui/dropdown-menu'
import { Toaster } from '@/components/ui/sonner'
import { systemApi } from './api'
import api from './api'
import ConfirmHost from './components/feedback/ConfirmHost.vue'
import PromptHost from './components/feedback/PromptHost.vue'
import ProfileSwitcher from './components/ProfileSwitcher.vue'
import AppRail from './components/shell/AppRail.vue'
import CommandPalette from './components/shell/CommandPalette.vue'
import MobileTabBar from './components/shell/MobileTabBar.vue'
import MobileGroupNav from './components/shell/MobileGroupNav.vue'
import { confirm, notify } from './lib/feedback'
import { useProfileStore } from './stores/profile'
import { useThemeStore } from './stores/theme'
import { scopeOfPath, type NavGroup, type NavItem } from './navigation'

const reduceMotion = useMediaQuery('(prefers-reduced-motion: reduce)')

const route = useRoute()
const router = useRouter()
const profileStore = useProfileStore()
const { activeProfileId } = profileStore
const { theme, toggleTheme } = useThemeStore()

const versionInfo = ref('v1.0')
const subscriptionAggregationEnabled = ref(false)
const showUserInfo = ref(false)
const username = ref('')
const palette = ref<InstanceType<typeof CommandPalette> | null>(null)
const isLoginPage = computed(() => route.path === '/login')
const pageKey = computed(() =>
  scopeOfPath(route.path) === 'profile' || route.path === '/dashboard'
    ? `${activeProfileId.value}:${route.path}`
    : route.path
)

// 快捷键提示按平台显示，Windows/Linux 上写 ⌘ 会误导
const metaKeyLabel = /Mac|iPhone|iPad/.test(navigator.platform) ? '⌘' : 'Ctrl+'

const currentProfileName = computed(
  () => profileStore.activeProfile.value?.name || activeProfileId.value || '默认'
)

/* ---------- 移动端分组入口 ---------- */
const activeScope = computed(() => scopeOfPath(route.path))

const itemsOf = (group: NavGroup): NavItem[] =>
  group.items.filter(
    item => item.flag !== 'subscriptionAggregation' || subscriptionAggregationEnabled.value
  )

/**
 * 底栏切换分组：直接进入该分组的第一个页面。
 * 分组内的页面切换交给内容区顶部的分段控件，避免多一次点击和一层模态。
 */
const openGroup = (group: NavGroup): void => {
  const items = itemsOf(group)
  if (!items.length) return
  // 已在该分组内时停留在当前页，不打断用户
  if (scopeOfPath(route.path) === group.scope) return
  router.push(items[0].path)
}

/* ---------- 既有业务逻辑 ---------- */
const loadVersion = async () => {
  try {
    const response = await systemApi.getVersion()
    if (response.data && response.data.version) {
      const ver = response.data.version
      versionInfo.value = ver.startsWith('v') ? ver : `v${ver}`
    }
  } catch (error) {
    console.error('Failed to load version:', error)
  }
}

const loadSubscriptionAggregationSetting = async () => {
  try {
    const response = await api.get('/settings/subscription-aggregation')
    subscriptionAggregationEnabled.value = response.data.enabled || false
  } catch (error) {
    console.error('Failed to load subscription aggregation setting:', error)
    subscriptionAggregationEnabled.value =
      localStorage.getItem('subscriptionAggregationEnabled') === 'true'
  }
}

const handleSubscriptionAggregationChange = (event: CustomEvent) => {
  subscriptionAggregationEnabled.value = event.detail.enabled
}

const checkAuthStatus = async () => {
  try {
    const storedUsername = localStorage.getItem('username')
    const token = localStorage.getItem('token')

    if (storedUsername && token) {
      showUserInfo.value = true
      username.value = storedUsername
      return
    }

    const response = await api.get('/auth/status')
    const authEnabled = response.data.authEnabled

    if (authEnabled && storedUsername) {
      showUserInfo.value = true
      username.value = storedUsername
    } else {
      showUserInfo.value = false
    }
  } catch (error) {
    console.error('Failed to check auth status:', error)
    const storedUsername = localStorage.getItem('username')
    const token = localStorage.getItem('token')
    if (storedUsername && token) {
      showUserInfo.value = true
      username.value = storedUsername
    } else {
      showUserInfo.value = false
    }
  }
}

watch(isLoginPage, login => {
  if (login) return
  checkAuthStatus()
  profileStore.refreshProfiles().catch(() => undefined)
  loadSubscriptionAggregationSetting()
}, { immediate: true })

const openGithub = () => {
  window.open('https://github.com/thsrite/configflow', '_blank')
}

const handleCommand = async (command: string) => {
  if (command !== 'logout') return
  const ok = await confirm('确定要退出登录吗？', { title: '退出登录', confirmText: '退出' })
  if (!ok) return

  localStorage.removeItem('token')
  localStorage.removeItem('username')
  notify.success('已退出登录')
  router.push('/login')
}

onMounted(async () => {
  loadVersion()
  window.addEventListener(
    'subscription-aggregation-changed',
    handleSubscriptionAggregationChange as EventListener
  )
})

onUnmounted(() => {
  window.removeEventListener(
    'subscription-aggregation-changed',
    handleSubscriptionAggregationChange as EventListener
  )
})
</script>
