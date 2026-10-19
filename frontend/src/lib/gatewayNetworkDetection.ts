import type { LinuxGatewaySpec } from '@/api/linuxGateway'
import { cleanGatewaySpec, gatewaySpecErrors } from './linuxGateway'

const networkFields = ['interface', 'address', 'lan_cidr', 'upstream_gateway'] as const
const shellQuote = (value: string) => `'${value.replace(/'/g, "'\\''")}'`

export function buildGatewayNetworkProbeCommand(serverUrl: string): string {
  let url: URL
  try {
    url = new URL(serverUrl.trim())
    if (!['http:', 'https:'].includes(url.protocol) || url.username || url.password || url.search || url.hash
      || url.pathname !== '/' || ['localhost', '127.0.0.1', '[::1]'].includes(url.hostname)) throw new Error()
  } catch {
    throw new Error('请填写 Linux 能访问的 ConfigFlow 服务地址，不带路径或登录凭据。')
  }
  const endpoint = shellQuote(`${url.origin}/api/agents/gateway-network-probe.sh`)
  return `(
  cf_probe=$(mktemp) || exit 1
  trap 'rm -f "$cf_probe"' 0
  trap 'exit 1' 1 2 15
  curl -fsSL ${endpoint} -o "$cf_probe" && sh "$cf_probe"
)`
}

export function parseGatewayNetworkProbe(text: string): LinuxGatewaySpec {
  if (!text.trim()) throw new Error('请粘贴检测命令输出的 JSON。')
  if (text.length > 8192) throw new Error('检测结果过长，请只复制命令输出的 JSON。')
  let result: unknown
  try { result = JSON.parse(text.trim()) } catch {
    throw new Error('无法读取检测结果，请完整复制命令输出的 JSON。')
  }
  if (!result || typeof result !== 'object' || Array.isArray(result)) {
    throw new Error('检测结果格式不正确，请重新运行检测命令。')
  }
  const data = result as Record<string, unknown>
  if (data.version !== 1) throw new Error('检测结果版本不支持，请复制页面上的命令重新检测。')
  if (networkFields.some(field => typeof data[field] !== 'string')) {
    throw new Error('检测结果缺少网络信息，请完整复制命令输出的 JSON。')
  }
  const spec = cleanGatewaySpec({
    interface: data.interface as string,
    address: data.address as string,
    lan_cidr: data.lan_cidr as string,
    upstream_gateway: data.upstream_gateway as string,
  })
  const errors = gatewaySpecErrors(spec)
  if (errors.length) throw new Error(errors.join(' '))
  return spec
}
