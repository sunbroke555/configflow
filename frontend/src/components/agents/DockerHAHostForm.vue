<script setup lang="ts">
import { computed } from 'vue'
import { Input } from '@/components/ui/input'
import { Checkbox } from '@/components/ui/checkbox'
import { RadioGroup, RadioGroupItem } from '@/components/ui/radio-group'
import FormField from '@/components/common/FormField.vue'
import type { DockerHAHostForm, DockerHANetwork } from '@/types/dockerHAGuide'

const props = defineProps<{ host: DockerHAHostForm; index: number; network: DockerHANetwork }>()
const prefix = computed(() => `docker-ha-host-${props.index + 1}`)
function identityChanged(value: boolean | 'indeterminate') {
  props.host.create_host_identity = value === true
  if (value === true && props.host.ha_host_id_path === '/etc/machine-id') props.host.ha_host_id_path = ''
  else if (value === false && !props.host.ha_host_id_path) props.host.ha_host_id_path = '/etc/machine-id'
}
</script>

<template>
  <div class="flex min-w-0 flex-col gap-4 [&_input]:min-h-11">
    <FormField label="这台主机的安装方式">
      <RadioGroup v-model="host.mode" class="flex flex-wrap gap-4" :aria-label="`主机 ${index + 1} 安装方式`">
        <label class="flex min-h-11 cursor-pointer items-center gap-2"><RadioGroupItem value="new" />新安装</label>
        <label class="flex min-h-11 cursor-pointer items-center gap-2"><RadioGroupItem value="migrate" />迁移已有 OxiDNS 容器</label>
      </RadioGroup>
    </FormField>
    <p v-if="host.mode === 'migrate'" class="m-0 rounded-md border border-warning/30 bg-warning-soft p-3 text-[12px] leading-relaxed">
      先读取旧容器信息，再填写原挂载和 Agent 完整名称。原 Agent 身份、配置、规则和 state 必须复用；不要填写新建的空目录或新卷。已经加入 HA 的设备，先在原高可用设置中处理迁移条件；不要直接停止持有 VIP 的容器。两台逐一迁移，先处理当前未持有 VIP 的一台。
    </p>
    <div class="grid min-w-0 grid-cols-1 gap-4 sm:grid-cols-2">
      <FormField v-if="host.mode === 'new'" label="Agent 名称前缀" :html-for="`${prefix}-name`" :hint="`安装后登记为 ${host.agent_name || '名称'}-oxidns。`">
        <Input :id="`${prefix}-name`" v-model="host.agent_name" placeholder="例如：dns-a" />
      </FormField>
      <template v-else>
        <FormField label="旧容器精确名称" :html-for="`${prefix}-container`">
          <Input :id="`${prefix}-container`" v-model="host.container_name" placeholder="例如：configflow-oxidns" />
        </FormField>
        <FormField label="原 Agent 完整名称" :html-for="`${prefix}-existing-name`" hint="从下方只读检查命令输出核对，不是 Docker 容器名。">
          <Input :id="`${prefix}-existing-name`" v-model="host.existing_agent_name" placeholder="例如：dns-a-oxidns" />
        </FormField>
      </template>
      <FormField :label="network === 'host' ? '宿主机固定 IPv4' : '容器固定 IPv4'" :html-for="`${prefix}-ip`" :hint="network === 'host' ? '填写宿主机现有的静态局域网地址。' : '为容器预留独立地址，不能与宿主机、网关或 VIP 相同。'">
        <Input :id="`${prefix}-ip`" v-model="host.agent_ip" class="font-mono" placeholder="192.168.1.21" />
      </FormField>
      <FormField label="Agent 端口" :html-for="`${prefix}-port`">
        <Input :id="`${prefix}-port`" v-model.number="host.oxidns_port" class="font-mono" type="number" min="1024" max="65535" />
      </FormField>
      <FormField v-if="host.mode === 'new'" label="宿主持久数据目录（绝对路径）" :html-for="`${prefix}-data`" persistent-hint hint="使用这台主机真实的持久目录；NAS 可用共享文件夹路径。不要填写 /tmp 或容器内路径。" class="sm:col-span-2">
        <Input :id="`${prefix}-data`" v-model="host.data_dir" class="font-mono" placeholder="例如：/opt/configflow-oxidns 或 /volume1/docker/oxidns" />
      </FormField>
      <template v-else>
        <FormField v-for="mount in (['agent_mount', 'core_mount'] as const)" :key="mount" :label="mount === 'agent_mount' ? '原 Agent 身份挂载' : '原 OxiDNS 数据挂载'" :html-for="`${prefix}-${mount}`" class="sm:col-span-2">
          <RadioGroup v-model="host[mount].type" class="flex flex-wrap gap-4" :aria-label="`${prefix}-${mount} 类型`">
            <label class="flex min-h-11 items-center gap-2"><RadioGroupItem value="bind" />宿主目录</label>
            <label class="flex min-h-11 items-center gap-2"><RadioGroupItem value="volume" />Docker 数据卷</label>
          </RadioGroup>
          <Input :id="`${prefix}-${mount}`" v-model="host[mount].source" class="font-mono" :placeholder="host[mount].type === 'bind' ? '填写 inspect 输出中的原宿主绝对路径' : '填写 inspect 输出中的原数据卷 Name'" />
        </FormField>
        <FormField label="HA 持久目录（绝对路径）" :html-for="`${prefix}-ha-data`"><Input :id="`${prefix}-ha-data`" v-model="host.ha_data_dir" class="font-mono" placeholder="/opt/configflow-ha" /></FormField>
        <FormField label="迁移备份目录（绝对路径）" :html-for="`${prefix}-backup`"><Input :id="`${prefix}-backup`" v-model="host.backup_dir" class="font-mono" placeholder="/opt/configflow-ha-backup" /></FormField>
      </template>
      <template v-if="network === 'macvlan'">
        <FormField label="宿主机网卡" :html-for="`${prefix}-parent`"><Input :id="`${prefix}-parent`" v-model="host.ha_parent" class="font-mono" placeholder="例如：eth0 或 enp1s0" /></FormField>
        <FormField label="局域网 CIDR" :html-for="`${prefix}-subnet`"><Input :id="`${prefix}-subnet`" v-model="host.ha_subnet" class="font-mono" placeholder="192.168.1.0/24" /></FormField>
        <FormField label="网关（可选）" :html-for="`${prefix}-gateway`" hint="建议填写实际路由器地址；留空由 Docker 选择默认网关。"><Input :id="`${prefix}-gateway`" v-model="host.ha_gateway" class="font-mono" placeholder="192.168.1.1" /></FormField>
      </template>
    </div>
    <div class="flex flex-col gap-3 border-t border-border pt-4">
      <label :for="`${prefix}-create-id`" class="flex min-h-11 cursor-pointer items-center gap-2 text-[13px]">
        <Checkbox :id="`${prefix}-create-id`" :model-value="host.create_host_identity" @update:model-value="identityChanged" />这台宿主机缺少稳定身份文件，需要创建
      </label>
      <FormField :label="host.create_host_identity ? '新身份文件的持久绝对路径' : '现有宿主机身份文件'" :html-for="`${prefix}-identity`" persistent-hint :hint="host.create_host_identity ? '命令只在文件不存在时创建一次 UUID，不覆盖已有文件。同一宿主机的所有 HA 容器复用它，不复制给对端。' : '先检查 /etc/machine-id，再检查 /var/lib/dbus/machine-id。使用宿主机上的文件，不是容器内新生成的身份。'">
        <Input :id="`${prefix}-identity`" v-model="host.ha_host_id_path" class="font-mono" :placeholder="host.create_host_identity ? '例如：/volume1/docker/configflow-host-id' : '/etc/machine-id'" />
      </FormField>
      <p class="m-0 text-[12px] leading-relaxed text-muted-foreground">宿主身份用于排除把两台容器放在同一主机的情况。内容需为单行 16–128 字节；创建命令使用 UUID 和私有文件权限。</p>
    </div>
  </div>
</template>
