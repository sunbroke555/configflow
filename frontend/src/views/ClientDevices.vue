<template>
  <div class="min-w-0">
    <PageHeader title="客户端设备" :mobile-help="pageHelp">
      <template #title-extra>
        <span v-if="state && (fromAgent || fromProfile)" class="min-w-0 max-w-48 truncate rounded-md bg-muted px-2 py-1 text-xs text-muted-foreground" :title="`配置空间：${state.profile_name}`">配置：{{ state.profile_name }}</span>
      </template>
      <template #actions>
        <Button v-if="fromAgent" variant="outline" class="min-h-11" @click="router.push(oxidnsMonitorPath(fromAgent))"><ArrowLeft class="size-4" />返回 OxiDNS 面板</Button>
        <Button v-else-if="backTo" variant="outline" class="min-h-11" @click="router.push(backTo)"><ArrowLeft class="size-4" />{{ fromSetup ? '返回配置向导' : '返回上游路由' }}</Button>
        <Button variant="outline" class="min-h-11 max-sm:size-11 max-sm:gap-0" aria-label="重新载入" title="重新载入设备和路由器" :disabled="loading || publishing" @click="reload">
          <RefreshCw class="size-4" :class="{ 'animate-spin motion-reduce:animate-none': loading || discovering }" /><span class="max-sm:sr-only">重新载入</span>
        </Button>
      </template>
    </PageHeader>
    <p class="mb-5 mt-0 max-w-3xl text-[13px] leading-relaxed text-muted-foreground max-md:hidden">{{ pageHelp }}</p>

    <LoadingRows v-if="loading && !state" :rows="4" />
    <SectionCard v-else-if="!state">
      <EmptyState :icon="MonitorSmartphone" title="暂时无法读取客户端设备" :description="error || '正在读取配置空间。'">
        <Button variant="outline" class="min-h-11" :disabled="loading" @click="load">重试</Button>
      </EmptyState>
    </SectionCard>
    <template v-else>
      <SectionCard title="设备列表" :help="phone ? `${listHelp}\n\n${modeHelp}` : listHelp" class="mb-5">
        <template #actions>
          <span class="flex min-h-11 items-center gap-1.5 text-xs text-muted-foreground" role="status" aria-live="polite">
            <template v-if="savingIp"><Loader2 class="size-3.5 animate-spin motion-reduce:animate-none" />正在保存…</template>
            <template v-else-if="savedOnce"><Check class="size-3.5" />已保存</template>
            <template v-else>修改自动保存</template>
          </span>
        </template>
        <p v-if="!state.editable" role="alert" class="mb-4 mt-0 rounded-lg bg-warning-soft p-3 text-sm text-warning-accent">{{ state.reason }} 名称仍可修改。</p>
        <p v-else-if="fromSetup" role="status" class="mb-4 mt-0 rounded-lg bg-info-soft p-3 text-sm text-info-accent" data-setup-names-only>配置向导进行中，这里只用于命名。DNS 策略会改动向导刚发布的 OxiDNS 配置，请完成向导后再设置 DNS 策略。</p>
        <p v-if="error" role="alert" class="mb-4 mt-0 text-sm text-destructive-accent">{{ error }}</p>
        <div v-if="reminder" role="status" class="mb-4 flex flex-wrap items-start gap-x-3 gap-y-2 rounded-lg bg-primary-soft p-3 text-sm max-sm:p-2.5">
          <div class="min-w-0 flex-1">
            <p class="m-0 font-medium text-primary-accent">已添加 {{ reminder }} 台设备</p>
            <p class="mb-0 mt-1 text-[13px]">DNS 策略要在下方「发布 DNS 策略」发布到 OxiDNS 后生效。<template v-if="policyRouting">已启用 RouterOS 策略路由，智能分流设备还需在<RouterLink to="/routers" class="text-primary-accent underline-offset-2 hover:underline">上游路由</RouterLink>重新发布。</template></p>
          </div>
          <div class="flex items-center gap-1">
            <Button v-if="state.agents.length" variant="outline" class="min-h-11" @click="toPublish">前往发布</Button>
            <Button variant="ghost" size="icon" class="size-11" aria-label="关闭提醒" @click="reminder = 0"><X class="size-4" /></Button>
          </div>
        </div>

        <Tabs :model-value="tab" class="min-w-0" @update:model-value="changeTab(String($event))">
          <TabsList class="mb-3 min-h-11 max-sm:grid max-sm:h-auto max-sm:w-full max-sm:grid-cols-3">
            <TabsTrigger v-for="item in tabItems" :key="item.value" :value="item.value" class="min-h-11 px-4 max-sm:px-2">
              {{ item.label }}
              <Loader2 v-if="item.count === null && discovering" class="size-3.5 animate-spin motion-reduce:animate-none" aria-label="正在读取" />
              <span v-else-if="item.count !== null" class="num text-xs opacity-80">{{ item.count }}</span>
            </TabsTrigger>
          </TabsList>

          <div v-if="!unsupported || tab === 'added'" class="mb-3 flex min-h-5 flex-wrap items-center gap-x-3 gap-y-1 text-xs text-muted-foreground" aria-live="polite">
            <template v-if="discovering"><Loader2 class="size-3.5 animate-spin motion-reduce:animate-none" />正在读取路由器上的设备，最长约 20 秒…</template>
            <template v-else-if="discoveryError">
              <span role="alert" class="text-destructive-accent">暂时无法读取路由器：{{ discoveryError }}</span>
            </template>
            <template v-else-if="discovery?.state === 'unsupported'">{{ discovery.message }}</template>
            <template v-else-if="discovery">
              <span>设备来源</span>
              <StatusDot v-for="source in discovery.sources" :key="source.id" :tone="source.ok ? 'success' : 'danger'"
                :label="`${source.name}（${source.type === 'ikuai' ? 'iKuai' : 'RouterOS'}）`" />
              <span v-if="checkedAt" class="num">{{ checkedAt }} 读取</span>
            </template>
          </div>
          <ul v-if="sourceProblems.length" class="mb-3 mt-0 list-none space-y-1 p-0 text-xs" aria-label="设备来源提示">
            <li v-for="item in sourceProblems" :key="item.key" :role="item.error ? 'alert' : undefined"
              :class="['flex items-start gap-1.5', item.error ? 'text-destructive-accent' : 'text-muted-foreground']">
              <TriangleAlert v-if="item.error" class="mt-px size-3.5 shrink-0" /><Info v-else class="mt-px size-3.5 shrink-0" />
              <span class="min-w-0">{{ item.text }}</span>
            </li>
          </ul>

          <div v-if="!unsupported || tab === 'added'" class="mb-3 flex min-w-0 flex-wrap items-center gap-2">
            <div class="relative min-w-0 flex-1 basis-56 sm:max-w-sm">
              <Search class="pointer-events-none absolute left-3 top-1/2 size-4 -translate-y-1/2 text-muted-foreground" />
              <Input v-model="search" class="min-h-11 pl-9" aria-label="搜索设备" placeholder="搜索名称、IP 或 MAC…" />
            </div>
            <div v-if="tab === 'added'" class="flex min-w-0 items-center gap-1 max-sm:w-full sm:ml-auto">
              <Input id="client-device-ip" v-model="newIP" aria-label="手动添加 IP / 网段" class="min-h-11 min-w-0 flex-1 font-mono sm:w-60" :placeholder="phone ? 'IP / 网段' : '192.168.50.0/24 或 IPv6'"
                @keydown.enter.prevent="add(newIP)" />
              <FieldHelp label="手动添加 IP / 网段" :text="rangeHelp" />
              <Button variant="outline" class="min-h-11" :disabled="!newIP.trim()" @click="add(newIP)"><Plus class="size-4" />添加</Button>
            </div>
            <div v-else-if="tab === 'new' && picked.length" class="flex items-center gap-1 max-sm:w-full sm:ml-auto">
              <Button class="min-h-11 max-sm:flex-1" :disabled="publishing || !!adding" @click="addFound(pickedFound)">
                <Loader2 v-if="adding" class="size-4 animate-spin motion-reduce:animate-none" /><Plus v-else class="size-4" />{{ adding ? `正在添加（${adding}）…` : `添加所选（${picked.length}）` }}
              </Button>
              <Button variant="ghost" class="min-h-11" :disabled="!!adding" @click="picked = []">取消选择</Button>
            </div>
          </div>

          <TabsContent v-for="value in savedTabs" :key="value" :value="value" class="mt-0 min-w-0">
            <EmptyState v-if="value === 'offline' && unsupported" :icon="Router" title="无法判断设备是否离线" :description="discovery!.message">
              <Button as-child variant="outline" class="min-h-11"><RouterLink to="/routers">前往上游路由</RouterLink></Button>
            </EmptyState>
            <LoadingRows v-else-if="value === 'offline' && !discovery && discovering" :rows="3" />
            <div v-else-if="visibleRows.length" class="cf-table-wrap">
              <table class="cf-table cf-mobile-records client-device-records w-full min-w-[760px]" role="table">
                <thead role="rowgroup">
                  <tr role="row">
                    <th role="columnheader" scope="col">设备</th>
                    <th role="columnheader" scope="col">IP / 网段</th>
                    <th role="columnheader" scope="col">状态</th>
                    <th role="columnheader" scope="col"><span class="inline-flex items-center gap-1">DNS 策略<FieldHelp label="DNS 策略" :text="modeHelp" /></span></th>
                    <th role="columnheader" scope="col" class="text-right"><span class="sr-only">操作</span></th>
                  </tr>
                </thead>
                <tbody role="rowgroup">
                  <tr v-for="row in visibleRows" :key="row.ip" role="row" :data-client-ip="row.ip">
                    <td role="cell">
                      <Input v-model="row.name" :aria-label="row.ip + ' 名称'" maxlength="80" :placeholder="infoOf(row.ip)?.hostname || '设备或网段名称'" class="min-h-11 min-w-36 max-sm:min-w-0"
                        :disabled="publishing" @blur="save(row.ip)" @keydown.enter.prevent="save(row.ip)" />
                      <p v-if="infoOf(row.ip)?.mac" class="mb-0 mt-1 font-mono text-[11px] text-muted-foreground">{{ infoOf(row.ip)!.mac }}</p>
                    </td>
                    <td role="cell">
                      <span class="whitespace-nowrap font-mono text-xs max-sm:text-[13px] max-sm:font-medium">{{ row.ip }}</span>
                      <span v-if="!saved(row.ip)" class="chip ml-2">未保存</span>
                      <ClientSightingChips :sources="infoOf(row.ip)?.sources" />
                    </td>
                    <td role="cell" data-label="状态">
                      <StatusDot v-bind="statusOf(row.ip)" />
                      <p v-if="hintOf(row.ip)" class="mb-0 mt-0.5 text-[11.5px] text-muted-foreground">{{ hintOf(row.ip) }}</p>
                    </td>
                    <td role="cell" data-label="DNS 策略">
                      <div class="flex flex-wrap items-center gap-2">
                        <Select :model-value="row.mode || 'default'" :disabled="publishing || !policyEditable" @update:model-value="value => setMode(row, String(value))">
                          <SelectTrigger :aria-label="row.ip + ' DNS 策略'" class="min-h-11 w-36 max-sm:w-full"><SelectValue /></SelectTrigger>
                          <SelectContent>
                            <SelectItem value="default">默认</SelectItem>
                            <SelectItem value="smart">智能分流</SelectItem>
                            <SelectItem value="direct">直连</SelectItem>
                          </SelectContent>
                        </Select>
                        <span v-if="routesSource(row)" class="chip chip-acc" title="RouterOS 策略路由按智能分流的 IPv4 来源转发">参与策略路由</span>
                      </div>
                    </td>
                    <td role="cell" class="text-right">
                      <div class="flex items-center justify-end gap-2">
                        <Loader2 v-if="savingIp === row.ip" class="size-4 animate-spin text-muted-foreground motion-reduce:animate-none" aria-label="正在保存" />
                        <span v-else-if="failures[row.ip]" class="flex items-center gap-1 text-xs text-destructive-accent" role="alert">
                          {{ failures[row.ip] }}<Button variant="ghost" class="min-h-11 px-2 text-xs" @click="save(row.ip)">重试</Button>
                        </span>
                        <Button variant="ghost" size="icon" class="size-11" :aria-label="'移除 ' + row.ip" :disabled="publishing || (!policyEditable && !!saved(row.ip)?.mode)" @click="remove(row)"><Trash2 class="size-4" /></Button>
                      </div>
                    </td>
                  </tr>
                </tbody>
              </table>
            </div>
            <p v-else-if="search.trim() && tabRows.length" class="m-0 py-6 text-center text-sm text-muted-foreground">没有匹配「{{ search }}」的设备。</p>
            <EmptyState v-else-if="value === 'offline'" :icon="MonitorSmartphone" title="没有离线的设备" description="已添加的设备都在线，或路由器无法判断。" />
            <EmptyState v-else :icon="MonitorSmartphone" title="尚未添加客户端设备" description="在「未添加」中选择路由器发现的设备，或输入 IP / 网段。可为设备命名，或设置 DNS 策略。" />
          </TabsContent>

          <TabsContent value="new" class="mt-0 min-w-0">
            <EmptyState v-if="unsupported" :icon="Router" title="无法发现设备" :description="discovery!.message">
              <Button as-child variant="outline" class="min-h-11"><RouterLink to="/routers">前往上游路由</RouterLink></Button>
            </EmptyState>
            <LoadingRows v-else-if="!discovery && discovering" :rows="4" />
            <div v-else-if="visibleFound.length" class="cf-table-wrap">
              <table class="cf-table cf-mobile-records client-discovery-records w-full min-w-[760px]" role="table">
                <thead role="rowgroup">
                  <tr role="row">
                    <th role="columnheader" scope="col" class="w-11">
                      <Checkbox :model-value="allPicked ? true : picked.length ? 'indeterminate' : false" :disabled="!pickable.length" :aria-label="search.trim() ? '选择全部搜索结果' : '选择全部设备'"
                        @update:model-value="value => pickAll(value === true)" />
                    </th>
                    <th role="columnheader" scope="col">设备</th>
                    <th role="columnheader" scope="col">IP</th>
                    <th role="columnheader" scope="col">状态</th>
                    <th role="columnheader" scope="col"><span class="inline-flex items-center gap-1">DNS 策略<FieldHelp label="DNS 策略" :text="foundHelp" /></span></th>
                    <th role="columnheader" scope="col" class="text-right"><span class="sr-only">操作</span></th>
                  </tr>
                </thead>
                <tbody role="rowgroup">
                  <tr v-for="item in visibleFound" :key="item.ip" role="row" :data-found-ip="item.ip">
                    <td role="cell">
                      <label class="-m-2.5 flex size-11 cursor-pointer items-center justify-center">
                        <Checkbox :model-value="picked.includes(item.ip)" :disabled="!canAdd(item) || !!adding" :aria-label="`选择 ${item.ip}`"
                          @update:model-value="value => pick(item.ip, value === true)" />
                      </label>
                    </td>
                    <td role="cell">
                      <span :class="['block break-all', item.hostname ? 'font-medium' : 'text-muted-foreground']">{{ item.hostname || '未命名设备' }}</span>
                      <span v-if="item.mac" class="block font-mono text-[11px] text-muted-foreground">{{ item.mac }}</span>
                    </td>
                    <td role="cell">
                      <span class="whitespace-nowrap font-mono text-xs max-sm:text-[13px]">{{ item.ip }}</span>
                      <ClientSightingChips :sources="item.sources" />
                    </td>
                    <td role="cell">
                      <StatusDot v-bind="statusOf(item.ip)" />
                      <p v-if="hintOf(item.ip)" class="mb-0 mt-0.5 text-[11.5px] text-muted-foreground">{{ hintOf(item.ip) }}</p>
                    </td>
                    <td role="cell" :class="{ 'client-found-default': !covering(item.ip) }">
                      <span v-if="covering(item.ip)" class="chip max-w-full" :title="`落在网段 ${covering(item.ip)!.ip}${covering(item.ip)!.name ? `（${covering(item.ip)!.name}）` : ''}，沿用它的 DNS 策略`">
                        <span class="truncate">随网段 <span class="font-mono">{{ covering(item.ip)!.ip }}</span> · {{ modeLabel(covering(item.ip)!.mode) }}</span>
                      </span>
                      <span v-else class="text-xs text-muted-foreground">默认</span>
                    </td>
                    <td role="cell" class="text-right">
                      <Button variant="outline" class="min-h-11" :disabled="publishing || !canAdd(item) || !!adding" :title="canAdd(item) ? '' : 'DNS 策略只读，且没有主机名可作名称'"
                        :aria-label="`添加 ${item.hostname || item.ip}`" @click="addFound([item])"><Plus class="size-4" />添加</Button>
                    </td>
                  </tr>
                </tbody>
              </table>
            </div>
            <p v-else-if="search.trim() && found.length" class="m-0 py-6 text-center text-sm text-muted-foreground">没有匹配「{{ search }}」的设备。</p>
            <EmptyState v-else-if="discovery && discovery.state === 'failed'" :icon="Router" title="暂时无法发现设备" :description="discovery.message" />
            <EmptyState v-else-if="discovery" :icon="MonitorSmartphone" title="没有未添加的设备" description="路由器看到的局域网设备都已添加。" />
            <EmptyState v-else :icon="Router" title="暂时无法发现设备" :description="discoveryError || '正在读取路由器。'">
              <Button variant="outline" class="min-h-11" :disabled="discovering" @click="rediscover">重试</Button>
            </EmptyState>
          </TabsContent>
        </Tabs>
      </SectionCard>

      <SectionCard id="client-device-publish" title="发布 DNS 策略" :help="publishHelp">
        <p v-if="!state.agents.length" class="m-0 text-sm text-muted-foreground">此配置空间没有 OxiDNS Agent。DNS 策略会保存在草稿中，接入 OxiDNS 并发布后生效。</p>
        <ul v-else class="m-0 list-none divide-y divide-border/70 p-0" aria-label="OxiDNS 发布目标">
          <li v-for="agent in state.agents" :key="agent.id" class="py-2">
            <div class="flex min-h-11 flex-wrap items-center gap-x-3 gap-y-1">
              <label class="flex min-h-11 min-w-0 cursor-pointer items-center gap-3">
                <Checkbox :model-value="selected.includes(agent.id)" :disabled="publishing" :aria-label="`发布到 ${agent.name}`"
                  @update:model-value="value => toggle(agent.id, value === true)" />
                <span class="min-w-0 break-all font-medium">{{ agent.name }}</span>
              </label>
              <span class="min-w-0 break-all font-mono text-xs text-muted-foreground">{{ agent.host || '—' }}</span>
              <StatusDot :tone="agent.status === 'online' ? 'success' : 'muted'" :label="agent.status === 'online' ? '在线' : '离线'" />
              <Button v-if="deployments[agent.id]?.status === 'ready'" class="ml-auto min-h-11" :disabled="publishing" @click="activate(agent.id)">应用并启动</Button>
            </div>
            <AgentDeploymentStatus v-if="deployments[agent.id]" :task="deployments[agent.id]" compact>
              <template v-if="needsNotice(agent.id)" #notice>
                <AgentOperationSteps :task="deployments[agent.id]" :show-current="false" :show-steps="false" />
                <AgentFailureDetails :show-progress-message="false" :showDNSChecks="false" :task="deployments[agent.id]" />
              </template>
              <template #details><AgentOperationSteps :task="deployments[agent.id]" :show-current="false" :show-message="!needsNotice(agent.id)" embedded /></template>
            </AgentDeploymentStatus>
            <AgentStatusRow v-else-if="rejected[agent.id]" label="配置推送" status="失败" tone="danger">
              <template #notice><AgentFailureDetails :show-progress-message="false" :showDNSChecks="false" :task="rejected[agent.id]" /></template>
            </AgentStatusRow>
            <AgentDNSChecks :checks="(deployments[agent.id] || rejected[agent.id])?.dns_checks" :checked-at="(deployments[agent.id] || rejected[agent.id])?.dns_checked_at" compact />
            <p v-if="deployments[agent.id]?.status === 'ready'" class="mb-0 mt-1 text-xs text-muted-foreground">待激活的是已上传的配置快照，不包含之后保存的草稿修改。</p>
          </li>
        </ul>
        <div v-if="state.agents.length" class="mt-4 flex flex-wrap items-center justify-between gap-3 border-t pt-4">
          <p class="m-0 min-w-0 flex-1 text-xs text-muted-foreground">发布的是「{{ state.profile_name }}」的完整已保存配置，包含其他草稿修改。</p>
          <Button class="min-h-11" :disabled="!selected.length || publishing || !!savingIp || selected.some(busy)" @click="publish">
            <Loader2 v-if="publishing" class="size-4 animate-spin" /><Upload v-else class="size-4" />{{ publishing ? '发布中…' : selected.length ? `发布到 ${selected.length} 台 OxiDNS` : '选择要发布的 OxiDNS' }}
          </Button>
        </div>
      </SectionCard>
    </template>
  </div>
</template>

<script setup lang="ts">
import { computed, nextTick, onMounted, onUnmounted, reactive, ref, watch } from 'vue'
import { onBeforeRouteLeave, onBeforeRouteUpdate, useRoute, useRouter } from 'vue-router'
import { useMediaQuery } from '@vueuse/core'
import { ArrowLeft, Check, Info, Loader2, MonitorSmartphone, Plus, RefreshCw, Router, Search, Trash2, TriangleAlert, Upload, X } from '@lucide/vue'
import { agentApi, clientDeviceApi, type ClientDevice, type ClientDeviceMode, type ClientDevicesState, type ClientDiscovery, type ClientPresenceInfo } from '@/api'
import { Button } from '@/components/ui/button'
import { Checkbox } from '@/components/ui/checkbox'
import { Input } from '@/components/ui/input'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs'
import PageHeader from '@/components/common/PageHeader.vue'
import SectionCard from '@/components/common/SectionCard.vue'
import LoadingRows from '@/components/common/LoadingRows.vue'
import EmptyState from '@/components/common/EmptyState.vue'
import FieldHelp from '@/components/common/FieldHelp.vue'
import StatusDot from '@/components/common/StatusDot.vue'
import ClientSightingChips from '@/components/clients/ClientSightingChips.vue'
import AgentDeploymentStatus from '@/components/agents/AgentDeploymentStatus.vue'
import AgentOperationSteps from '@/components/agents/AgentOperationSteps.vue'
import AgentFailureDetails from '@/components/agents/AgentFailureDetails.vue'
import AgentStatusRow from '@/components/agents/AgentStatusRow.vue'
import AgentDNSChecks from '@/components/agents/AgentDNSChecks.vue'
import { createDeploymentId, deploymentNeedsNotice, useAgentDeployments, type AgentDeployment } from '@/composables/useAgentDeployments'
import { oxidnsMonitorPath } from '@/composables/useOxiDNSDevices'
import { confirm, confirmDanger, notify } from '@/lib/feedback'
import { normalizeClientRange } from '@/lib/oxidnsClientRange'
import { usePageReloadGuard } from '@/lib/pageReloadGuard'

const pageHelp = '为局域网设备命名，并按需设置 OxiDNS 的 DNS 策略。设备从 RouterOS / iKuai 主路由实时读取；名称在本配置空间共用，Mihomo 连接、数据统计和 OxiDNS 查询记录都按它显示。'
const listHelp = '「未添加」是主路由看到、还没有添加的设备，「已离线」是路由器判断已离线的已添加设备；每次打开或重新载入都重新读取，不保存。修改名称立即生效，不需要发布。DNS 策略保存为草稿，发布到 OxiDNS 后生效；已启用策略路由时，智能分流 IPv4 的变化还需在上游路由页重新发布。名称和 DNS 策略都清空后，设备从列表移除。'
const modeHelp = [
  '默认：没有单独策略。地址落在已设策略的网段内时沿用该网段，否则按 DNS 规则处理。',
  '智能分流：按 DNS 规则处理，可让「直连」网段中的单个设备例外；IPv4 地址还是上游路由「策略路由」的来源，改动后需在上游路由页重新发布。',
  '直连：用本地 DNS 上游解析全部域名；是否走代理仍取决于路由器分流。',
  '设了策略的范围重叠时，单个 IP 优先，其次是更小的网段，与添加顺序无关。',
].join('\n\n')
const rangeHelp = [
  '支持单个 IPv4 / IPv6 和 CIDR 网段，例如 192.168.50.50、192.168.50.0/24，网段会整理为标准写法。',
  '设备需使用 OxiDNS 作为 DNS，且 OxiDNS 能识别真实客户端 IP；填写地址不会修改设备的 DNS。',
].join('\n\n')
const foundHelp = '添加后默认设为智能分流，名称取设备主机名。地址落在已设策略的网段内时，未添加前沿用该网段的策略。'
const publishHelp = '只发布到勾选的 OxiDNS。未勾选的设备保持现有配置；同一配置空间的 OxiDNS 共用这份草稿。'

const route = useRoute()
const router = useRouter()
const agentOf = (value: unknown) => typeof value === 'string' ? value : ''
/** 从 OxiDNS 面板进入时固定使用该设备绑定的配置空间，不切换全局空间 */
const fromAgent = ref(agentOf(route.query.agent))
/** 从上游路由或配置向导进入时的返回地址，只接受站内路径 */
const backTo = computed(() => { const value = route.query.from; return typeof value === 'string' && value.startsWith('/') && !value.startsWith('//') ? value : '' })
/** 配置向导进行中只命名：DNS 策略会改动向导已发布并检查过的 OxiDNS 配置 */
const fromSetup = computed(() => !fromAgent.value && backTo.value.startsWith('/setup'))
/** 从上游路由等页面进入其他配置空间时同样不切换全局空间 */
const fromProfile = ref(agentOf(route.query.profile))
// Matches the breakpoint where .cf-mobile-records hides the table header and its help.
const phone = useMediaQuery('(width < 640px)')
const state = ref<ClientDevicesState | null>(null)
const policyEditable = computed(() => !!state.value?.editable && !fromSetup.value)
const rows = ref<ClientDevice[]>([])
const loading = ref(false)
const error = ref('')
const newIP = ref('')
const search = ref('')
const savingIp = ref('')
const savedOnce = ref(false)
const failures = reactive<Record<string, string>>({})
const selected = ref<string[]>([])
const publishing = ref(false)
const rejected = reactive<Record<string, Partial<AgentDeployment>>>({})
const discovery = ref<ClientDiscovery | null>(null)
const discovering = ref(false)
const discoveryError = ref('')
/** 未添加标签中勾选的地址；添加后移出 */
const picked = ref<string[]>([])
const adding = ref(0)
/** 刚添加的设备数，用于提醒发布 */
const reminder = ref(0)
let discoveryRun = 0
let disposed = false
let queue: Promise<unknown> = Promise.resolve()

const { deployments, busy, track, beginSubmission, endSubmission, activate } = useAgentDeployments(() => undefined)
usePageReloadGuard(() => publishing.value || !!savingIp.value || unsaved().length > 0)

const saved = (ip: string) => state.value?.devices.find(row => row.ip === ip)
const unsaved = () => rows.value.filter(row => {
  const previous = saved(row.ip)
  return previous ? previous.name !== row.name.trim() || previous.mode !== row.mode : !!(row.name.trim() || row.mode)
})

// 「已添加 / 未添加 / 已离线」与地址栏 ?tab= 同步。
type Tab = 'added' | 'new' | 'offline'
const savedTabs: Tab[] = ['added', 'offline']
const tabOf = (value: unknown): Tab => value === 'new' || value === 'offline' ? value : 'added'
const tab = ref<Tab>(tabOf(route.query.tab))
watch(() => route.query.tab, value => { tab.value = tabOf(value) })
function changeTab(value: string) {
  tab.value = tabOf(value)
  void router.replace({ query: { ...route.query, tab: tab.value === 'added' ? undefined : tab.value } })
}

/** 路由器对每个地址的判断：已保存的行和新发现的地址 */
const seen = computed(() => {
  const map = new Map<string, ClientPresenceInfo>()
  for (const row of discovery.value?.devices || []) map.set(row.ip, row)
  for (const row of discovery.value?.discovered || []) map.set(row.ip, row)
  return map
})
const infoOf = (ip: string) => seen.value.get(ip)
const unsupported = computed(() => discovery.value?.state === 'unsupported')
const policyRouting = computed(() => !!discovery.value?.sources.some(source => source.policy_routing))
const presenceOf = (ip: string) => infoOf(ip)?.presence || (ip.includes('/') ? 'network' : 'unknown')
const listed = computed(() => new Set(rows.value.map(row => row.ip)))
const ipKey = (ip: string) => ip.split('.').reduce((total, part) => total * 256 + Number(part), 0)
/** 未添加：路由器看到、本页还没有这一行的 IPv4 地址（删除的行回到这里） */
const found = computed(() => [...seen.value.values()]
  .filter(item => !listed.value.has(item.ip) && !item.ip.includes(':') && !item.ip.includes('/') && item.basis !== 'router'
    && item.sources.length > 0 && (!!item.mac || item.sources.some(source => source !== 'arp')))
  .sort((a, b) => ipKey(a.ip) - ipKey(b.ip)))
const tabRows = computed(() => rows.value.filter(row => (presenceOf(row.ip) === 'offline') === (tab.value === 'offline')))
const matches = (term: string, ...values: (string | undefined)[]) => values.some(value => value?.toLowerCase().includes(term))
const visibleRows = computed(() => {
  const term = search.value.trim().toLowerCase()
  return term ? tabRows.value.filter(row => matches(term, row.ip, row.name, infoOf(row.ip)?.hostname, infoOf(row.ip)?.mac)) : tabRows.value
})
const visibleFound = computed(() => {
  const term = search.value.trim().toLowerCase()
  return term ? found.value.filter(item => matches(term, item.ip, item.hostname, item.mac)) : found.value
})
const tabItems = computed(() => {
  const ready = !!discovery.value && !unsupported.value
  const offline = rows.value.filter(row => presenceOf(row.ip) === 'offline').length
  return [
    { value: 'added', label: '已添加', count: rows.value.length - offline },
    { value: 'new', label: '未添加', count: ready ? found.value.length : null },
    { value: 'offline', label: '已离线', count: ready ? offline : null }
  ]
})
const checkedAt = computed(() => {
  const time = discovery.value?.checked_at ? new Date(discovery.value.checked_at) : null
  return time && !Number.isNaN(time.getTime()) ? time.toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' }) : ''
})
/** 路由器读取失败与来源说明，逐台列出 */
const sourceProblems = computed(() => (discovery.value?.sources || []).flatMap(source => [
  ...(source.ok ? [] : [{ key: `${source.id}:error`, error: true, text: `「${source.name}」读取失败：${source.error || '未知原因'}` }]),
  ...source.notes.map((note, index) => ({ key: `${source.id}:${index}`, error: false, text: `「${source.name}」${note}` }))
]))

const PRESENCE: Record<string, { label: string; tone: 'success' | 'warning' | 'muted' }> = {
  online: { label: '在线', tone: 'success' }, recent: { label: '近期见过', tone: 'warning' }, offline: { label: '离线', tone: 'muted' },
  unknown: { label: '未知', tone: 'muted' }, network: { label: '网段', tone: 'muted' }
}
function statusOf(ip: string) {
  if (!infoOf(ip) && !ip.includes('/') && !ip.includes(':') && discovering.value) return { label: '读取中', tone: 'muted' as const }
  return PRESENCE[presenceOf(ip)]
}
function ago(seconds: number) {
  if (seconds < 60) return '刚刚'
  if (seconds < 3600) return `${Math.round(seconds / 60)} 分钟前`
  if (seconds < 86400) return `${Math.floor(seconds / 3600)} 小时前`
  return `${Math.floor(seconds / 86400)} 天前`
}
/** 状态下方的依据与时间 */
function hintOf(ip: string) {
  const info = infoOf(ip)
  if (!info) {
    if (ip.includes('/') || discovering.value) return ''
    if (ip.includes(':')) return '只检测 IPv4'
    if (unsupported.value) return '未接入 RouterOS / iKuai'
    return discovery.value ? '重新载入后判断' : ''
  }
  const renewed = info.last_seen === null ? '' : `${ago(info.last_seen)}续租`
  const hint = {
    arp: info.presence === 'offline' ? 'ARP 无应答' : 'ARP 可达',
    lease: info.presence === 'offline' ? (renewed ? `${renewed}，已错过续租` : '租约已过期') : renewed || 'DHCP 租约有效',
    probe: info.presence === 'offline' ? '路由器探测无应答' : '路由器探测有应答',
    terminal: '近 2 分钟有流量经过路由器',
    stale: renewed ? `未能确认，${renewed}` : '路由器有记录，未能确认',
    absent: '路由器没有看到这个地址',
    router: '路由器自己的地址',
    outside: '不在路由器扫描的网段内',
    '': info.presence !== 'unknown' ? '' : ip.includes(':') ? '只检测 IPv4' : unsupported.value ? '未接入 RouterOS / iKuai'
      : discovery.value?.state !== 'ok' ? '路由器未全部读取成功' : ''
  }[info.basis] || ''
  const router = (discovery.value?.sources.length || 0) > 1 && info.router_id ? discovery.value!.sources.find(source => source.id === info.router_id)?.name : ''
  return [router, hint].filter(Boolean).join(' · ')
}
const ipv4Number = (ip: string) => {
  const parts = ip.split('.').map(Number)
  return parts.length === 4 && parts.every(part => Number.isInteger(part) && part >= 0 && part <= 255) ? ipKey(ip) : null
}
/** 对这个地址生效的网段策略：设了策略的网段中前缀最长的一条，与 OxiDNS 和策略路由一致 */
function covering(ip: string): ClientDevice | null {
  const address = ipv4Number(ip)
  let best: ClientDevice | null = null, longest = -1
  if (address === null) return null
  for (const row of state.value?.devices || []) {
    const [base, bits] = row.ip.split('/')
    const prefix = Number(bits), network = ipv4Number(base)
    if (!row.mode || bits === undefined || network === null || prefix <= longest) continue
    const size = 2 ** (32 - prefix)
    if (Math.floor(address / size) === Math.floor(network / size)) { best = row; longest = prefix }
  }
  return best
}
const modeLabel = (mode: ClientDeviceMode) => ({ smart: '智能分流', direct: '直连', '': '默认' })[mode]
/** RouterOS 策略路由的来源：智能分流的 IPv4 设备或网段 */
const routesSource = (row: ClientDevice) => policyRouting.value && row.mode === 'smart' && !!saved(row.ip) && !row.ip.includes(':')
const cleanName = (value: string) => value.replace(/[\u0000-\u001f\u007f]/g, '').trim().slice(0, 80)
/** 没有主机名时只能靠 DNS 策略保存这一行 */
const canAdd = (item: ClientPresenceInfo) => !!state.value && (policyEditable.value || !!cleanName(item.hostname))
const pickable = computed(() => visibleFound.value.filter(canAdd).map(item => item.ip))
const allPicked = computed(() => pickable.value.length > 0 && pickable.value.every(ip => picked.value.includes(ip)))
const pickedFound = computed(() => found.value.filter(item => picked.value.includes(item.ip)))
watch(found, items => {
  const present = new Set(items.map(item => item.ip))
  if (picked.value.some(ip => !present.has(ip))) picked.value = picked.value.filter(ip => present.has(ip))
})
function pick(ip: string, value: boolean) {
  picked.value = value ? [...new Set([...picked.value, ip])] : picked.value.filter(item => item !== ip)
}
/** 有搜索词时只作用于匹配项，保留其他已选项 */
function pickAll(value: boolean) {
  const visible = new Set(pickable.value)
  picked.value = value ? [...new Set([...picked.value, ...pickable.value])] : picked.value.filter(ip => !visible.has(ip))
}
const needsNotice = (id: string) => !!deployments[id] && deploymentNeedsNotice(deployments[id])
const messageOf = (failure: any) => failure?.response?.data?.message || failure?.message || '操作失败，请重试。'

function adopt(data: ClientDevicesState) {
  const latest = new Map(data.devices.map(row => [row.ip, row]))
  const before = new Map((state.value?.devices || []).map(row => [row.ip, row]))
  state.value = data
  // Rows keep their place. Edits still being typed stay, and rows only added
  // here stay until they get a name or a policy; rows removed elsewhere leave.
  const local = rows.value.flatMap(row => {
    const next = latest.get(row.ip), previous = before.get(row.ip)
    if (!next) return previous ? [] : [row]
    if (!previous) return [{ ...next }]
    // Only the fields edited here stay local; the rest follows the server, so a
    // later save never sends back a value changed elsewhere meanwhile.
    return [{ ...next, name: row.name.trim() !== previous.name ? row.name : next.name, mode: row.mode !== previous.mode ? row.mode : next.mode }]
  })
  const listed = new Set(local.map(row => row.ip))
  rows.value = [...local, ...data.devices.filter(row => !listed.has(row.ip)).map(row => ({ ...row }))]
}

/** keep：保留本页未保存的修改，只合并最新数据（单行冲突后使用） */
async function load(keep = false) {
  loading.value = true
  error.value = ''
  try {
    const { data } = await clientDeviceApi.get({ agentId: fromAgent.value || undefined, profileId: fromProfile.value || undefined })
    if (disposed) return
    const merge = keep && data.profile_id === state.value?.profile_id
    if (merge) {
      adopt(data)
      return
    }
    for (const ip of Object.keys(failures)) delete failures[ip]
    rows.value = []
    state.value = null
    discovery.value = null
    picked.value = []
    reminder.value = 0
    adopt(data)
    const online = data.agents.filter(agent => agent.status === 'online').map(agent => agent.id)
    selected.value = fromAgent.value ? data.agents.filter(agent => agent.id === fromAgent.value).map(agent => agent.id) : online
    for (const agent of data.agents) if (agent.deployment) track(agent.id, agent.deployment, false)
    void discover(data.profile_id)
  } catch (failure) {
    if (!disposed) error.value = messageOf(failure)
  } finally {
    loading.value = false
  }
}

/** 实时读取主路由；只采用最后一次、且仍是当前配置空间的结果 */
async function discover(profileId: string) {
  const run = ++discoveryRun
  discovering.value = true
  discoveryError.value = ''
  try {
    const { data } = await clientDeviceApi.discover(profileId)
    if (disposed || run !== discoveryRun || state.value?.profile_id !== profileId) return
    discovery.value = data
  } catch (failure) {
    if (!disposed && run === discoveryRun) discoveryError.value = messageOf(failure)
  } finally {
    if (run === discoveryRun) discovering.value = false
  }
}

function rediscover() {
  if (state.value) void discover(state.value.profile_id)
}

async function reload() {
  await queue
  if (!unsaved().length || await confirm('重新载入会丢弃尚未保存的修改。', { title: '重新载入客户端设备', confirmText: '重新载入' })) await load()
}

function add(value: string) {
  let ip = value.trim()
  if (!ip) return
  try {
    ip = normalizeClientRange(ip)
  } catch {
    notify.error('请输入有效的客户端 IPv4、IPv6 或 CIDR 网段，例如 192.168.50.0/24。')
    return
  }
  newIP.value = ''
  search.value = ''
  if (!rows.value.some(row => row.ip === ip)) rows.value.unshift({ ip, name: '', mode: '' })
  else notify.info('此设备已在列表中。')
  // 离线的地址出现在「已离线」，切到它所在的标签再聚焦名称。
  const target: Tab = presenceOf(ip) === 'offline' ? 'offline' : 'added'
  if (tab.value !== target) changeTab(target)
  // A tab that was just opened renders its rows a moment later.
  const focus = () => [...document.querySelectorAll<HTMLElement>('[data-client-ip]')]
    .find(row => row.dataset.clientIp === ip)?.querySelector('input')?.focus()
  void nextTick(() => setTimeout(focus))
}

/** 添加路由器发现的设备：名称取主机名，默认智能分流，逐行排队保存 */
async function addFound(items: ClientPresenceInfo[]) {
  if (!state.value || adding.value) return
  const mode: ClientDeviceMode = policyEditable.value ? 'smart' : ''
  const added = items.filter(item => canAdd(item) && !rows.value.some(row => row.ip === item.ip)).map(item => item.ip)
  if (!added.length) return
  for (const ip of [...added].reverse()) rows.value.unshift({ ip, name: cleanName(infoOf(ip)?.hostname || ''), mode })
  picked.value = picked.value.filter(ip => !added.includes(ip))
  adding.value = added.length
  try {
    for (const ip of added) {
      await save(ip)
      adding.value = Math.max(adding.value - 1, 0)
    }
  } finally {
    adding.value = 0
  }
  if (disposed) return
  const done = added.filter(ip => saved(ip) && !failures[ip]).length
  const failed = added.length - done
  if (failed) notify.error(`${failed} 台设备未能添加`, '失败的设备保留在「已添加」中，可在该行重试。')
  if (!done) return
  if (mode) reminder.value += done
  notify.success(`已添加 ${done} 台设备`, mode ? 'DNS 策略需发布到 OxiDNS 后生效。' : undefined)
}

function toPublish() {
  document.getElementById('client-device-publish')?.scrollIntoView({ behavior: 'smooth', block: 'start' })
}

function setMode(row: ClientDevice, value: string) {
  row.mode = (value === 'default' ? '' : value) as ClientDeviceMode
  void save(row.ip)
}

async function remove(row: ClientDevice) {
  const previous = saved(row.ip)
  if (previous && !await confirmDanger(`将清除「${previous.name || row.ip}」的名称${previous.mode ? '和 DNS 策略' : ''}。${previous.mode ? 'DNS 策略的修改需要发布后生效。' : ''}`,
    { title: '移除客户端设备', confirmText: '移除' })) return
  if (!previous) {
    delete failures[row.ip]
    rows.value = rows.value.filter(item => item.ip !== row.ip)
    return
  }
  row.name = ''
  row.mode = ''
  await save(row.ip)
}

/** 逐行保存，排队执行，避免同一行的两次修改乱序 */
function save(ip: string): Promise<unknown> {
  queue = queue.then(() => saveNow(ip))
  return queue
}

async function saveNow(ip: string) {
  const row = rows.value.find(item => item.ip === ip)
  if (!row || !state.value || disposed) return
  const previous = saved(ip) || { name: '', mode: '' as ClientDeviceMode }
  const device = { ip, name: row.name.trim(), mode: row.mode }
  delete failures[ip]
  if (device.name === previous.name && device.mode === previous.mode) return
  savingIp.value = ip
  try {
    const { data } = await clientDeviceApi.save(state.value.profile_id, device, { name: previous.name, mode: previous.mode })
    if (disposed) return
    adopt(data)
    savedOnce.value = true
  } catch (failure: any) {
    if (disposed) return
    if (failure?.response?.status === 409) {
      notify.error('设备已在其他页面修改', messageOf(failure))
      // Drop only this row's edit; other rows keep what is still being typed.
      Object.assign(row, saved(ip) || { name: '', mode: '' })
      if (!saved(ip)) rows.value = rows.value.filter(item => item.ip !== ip)
      await load(true)
    } else {
      failures[ip] = messageOf(failure)
    }
  } finally {
    if (savingIp.value === ip) savingIp.value = ''
  }
}

function toggle(id: string, value: boolean) {
  selected.value = value ? [...new Set([...selected.value, id])] : selected.value.filter(item => item !== id)
}

async function publish() {
  await queue
  const failing = rows.value.some(row => failures[row.ip])
  if (!state.value || publishing.value || failing) {
    if (failing) notify.error('有设备未保存', '先处理保存失败的设备，再发布。')
    return
  }
  const targets = state.value.agents.filter(agent => selected.value.includes(agent.id))
  const expected = { expected_profile_id: state.value.profile_id, expected_oxidns_revision: state.value.oxidns_revision }
  if (!targets.length || !await confirm(`将发布「${state.value.profile_name}」的完整已保存配置到 ${targets.map(agent => `「${agent.name}」`).join('、')}，并按现有发布流程校验、备份和应用配置。`,
    { title: '发布 DNS 策略', confirmText: '发布' })) return
  publishing.value = true
  try {
    for (const agent of targets) {
      if (disposed) return
      delete rejected[agent.id]
      const deploymentId = createDeploymentId()
      if (!beginSubmission(agent.id, deploymentId)) continue
      try {
        const { data } = await agentApi.pushConfig(agent.id, deploymentId, expected)
        endSubmission(agent.id, deploymentId, data)
      } catch (failure: any) {
        if (failure.response?.data?.deployment_id) endSubmission(agent.id, deploymentId, failure.response.data)
        else if (!failure.response || failure.response.status >= 500) endSubmission(agent.id, deploymentId, 'unknown')
        else if (!endSubmission(agent.id, deploymentId, 'rejected')) rejected[agent.id] = { ...failure.response.data, error: messageOf(failure) }
        // A changed draft rejects every device; a busy or rolled-back device fails alone.
        if (failure.response?.data?.code === 'draft_changed') {
          notify.error('发布已停止', messageOf(failure))
          return
        }
      }
    }
  } finally {
    publishing.value = false
  }
}

onBeforeRouteLeave(async () => {
  await queue
  return !unsaved().length || await confirm('有设备尚未保存，离开会丢失这些修改。', { title: '离开客户端设备', confirmText: '放弃修改' })
})
onBeforeRouteUpdate(async to => {
  const next = agentOf(to.query.agent), profile = agentOf(to.query.profile)
  if (next === fromAgent.value && profile === fromProfile.value) return
  await queue
  if (unsaved().length && !await confirm('有设备尚未保存，切换后会丢失这些修改。', { title: '离开客户端设备', confirmText: '放弃修改' })) return false
  fromAgent.value = next
  fromProfile.value = profile
  void load()
})
onMounted(() => load())
onUnmounted(() => { disposed = true })
</script>
