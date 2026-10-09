<template>
  <main class="flex min-h-dvh items-center justify-center bg-background px-5 py-10">
    <div class="grid w-full max-w-[920px] overflow-hidden rounded-xl border border-border bg-card md:grid-cols-[1.1fr_1fr]">
      <section class="flex flex-col justify-between gap-10 border-b border-border bg-secondary/40 p-8 md:border-r md:border-b-0 md:p-10">
        <div class="flex items-center gap-3">
          <img src="/icon.png" alt="" class="size-9 rounded-lg" />
          <span class="text-xl font-semibold tracking-tight">ConfigFlow</span>
        </div>
        <div>
          <h1 class="text-3xl font-semibold leading-tight tracking-tight text-foreground">代理配置管理系统</h1>
          <p class="mt-4 max-w-[32ch] text-sm leading-7 text-muted-foreground">管理订阅和节点，为不同设备或使用场景设置分流规则，生成可直接使用的配置。</p>
        </div>
        <div class="hidden border-t border-border pt-5 text-xs leading-6 text-muted-foreground md:block">
          <p>订阅和节点共用，策略和规则分别设置</p>
          <p class="mt-1 font-mono">Mihomo / Surge / MosDNS</p>
        </div>
      </section>
      <section class="p-8 md:px-10 md:py-12" aria-labelledby="login-title">
        <h2 id="login-title" class="text-xl font-semibold tracking-tight">登录</h2>
        <p class="mt-2 mb-7 text-sm text-muted-foreground">使用管理员账号进入工作台。</p>

      <form class="flex flex-col gap-4" @submit.prevent="handleLogin">
        <div class="flex flex-col gap-1.5">
          <Label for="login-username" class="text-[12.5px] text-muted-foreground">用户名</Label>
          <div class="relative">
            <User
              class="pointer-events-none absolute top-1/2 left-3 size-4 -translate-y-1/2 text-muted-foreground"
              aria-hidden="true"
            />
            <Input
              id="login-username"
              v-model="loginForm.username"
              autocomplete="username"
              placeholder="请输入用户名"
              class="h-11 bg-background/50 pl-9 text-[14px]"
              :disabled="loading"
              :aria-invalid="Boolean(errors.username)"
            />
          </div>
          <p v-if="errors.username" class="m-0 text-[12px] text-destructive-accent">
            {{ errors.username }}
          </p>
        </div>

        <div class="flex flex-col gap-1.5">
          <Label for="login-password" class="text-[12.5px] text-muted-foreground">密码</Label>
          <div class="relative">
            <Lock
              class="pointer-events-none absolute top-1/2 left-3 size-4 -translate-y-1/2 text-muted-foreground"
              aria-hidden="true"
            />
            <Input
              id="login-password"
              v-model="loginForm.password"
              type="password"
              autocomplete="current-password"
              placeholder="请输入密码"
              class="h-11 bg-background/50 pl-9 text-[14px]"
              :disabled="loading"
              :aria-invalid="Boolean(errors.password)"
            />
          </div>
          <p v-if="errors.password" class="m-0 text-[12px] text-destructive-accent">
            {{ errors.password }}
          </p>
        </div>

        <Button type="submit" class="mt-2 h-11 w-full text-[14px]" :disabled="loading">
          <Loader2 v-if="loading" class="size-4 animate-spin" aria-hidden="true" />
          {{ loading ? '登录中…' : '登录' }}
        </Button>
      </form>
      </section>
    </div>
  </main>
</template>

<script setup lang="ts">
import { reactive, ref } from 'vue'
import { useRouter } from 'vue-router'
import axios from 'axios'
import { Loader2, Lock, User } from '@lucide/vue'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { notify } from '@/lib/feedback'

const router = useRouter()
const loading = ref(false)

const loginForm = reactive({ username: '', password: '' })
const errors = reactive({ username: '', password: '' })

/* 表单校验就地实现：只有两个必填项，引入表单库不划算 */
const validate = (): boolean => {
  errors.username = loginForm.username.trim() ? '' : '请输入用户名'
  errors.password = loginForm.password ? '' : '请输入密码'
  return !errors.username && !errors.password
}

const handleLogin = async () => {
  if (!validate()) return

  loading.value = true
  try {
    const response = await axios.post('/api/auth/login', {
      username: loginForm.username,
      password: loginForm.password
    })

    if (response.data.success) {
      localStorage.setItem('token', response.data.token)
      localStorage.setItem('username', response.data.username)
      notify.success('登录成功')
      router.push('/')
    } else {
      notify.error(response.data.message || '登录失败')
    }
  } catch (error: any) {
    console.error('登录失败:', error)
    if (error.response?.data?.message) {
      notify.error(error.response.data.message)
    } else if (error.response?.status === 401) {
      notify.error('用户名或密码错误')
    } else {
      notify.error('登录失败，请稍后重试')
    }
  } finally {
    loading.value = false
  }
}
</script>
