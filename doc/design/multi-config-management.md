# ConfigFlow 多配置管理可行性分析

> 仓库：`thsrite/configflow`
> 分析提交：`875d67c8fb32aa66710e918c899380ae16f4520e`
> 目标：在一个 ConfigFlow 实例中管理多套相互隔离的 Mihomo / MosDNS / Surge 配置，并将不同配置绑定到不同 Agent。

## 1. 结论

**可行，建议实现。**

但不能只在前端增加“配置下拉框”。当前系统把配置、缓存、生成产物和 Agent 注册全部放在单一全局作用域：

- 单一配置文件：`/data/config.json`
- 单一进程全局对象：`backend.common.config.config_data`
- 单一订阅缓存目录：`/data/subscribes`
- 单一规则/Provider 目录：`/data/rules`、`/data/providers`
- 单一生成产物：`/data/config.yaml`、`/data/config.conf`
- Agent 注册信息也存于同一个 `config_data['agents']`

代码规模约 28,952 行，后端约 8,031 行 Python。约 20 个 Python 文件、450 处引用直接依赖 `config_data`，另有 66 处 `save_config()` 调用。因此多配置属于**中等规模架构改造**，不是小功能。

推荐引入一等公民的 `Profile`（配置空间），把“系统级数据”和“配置级数据”拆开，并让每个 Agent 显式绑定一个 Profile。

## 2. 当前架构与耦合点

### 2.1 配置存储

核心文件：`backend/common/config.py`

当前：

```python
DATA_DIR = os.environ.get('DATA_DIR', '/data')
CONFIG_FILE = os.path.join(DATA_DIR, 'config.json')
config_data = get_default_config()
```

`get_config()` 永远返回同一个全局字典；`save_config()` 永远覆盖同一个 `config.json`。

主要问题：

1. 无法同时加载两套配置；
2. 并发请求切换全局 `config_data` 会串配置；
3. `AgentManager` 持有同一个字典引用；
4. 生成器、路由、MCP 内部调用都默认使用全局配置；
5. 当前写文件不是原子替换，也没有按 Profile 加锁。

另外，`backend/models/config.py` 中的 dataclass 与实际存储模型已经脱节：模型使用 `rules` / `rule_sets`，实际配置使用统一的 `rule_configs`；路由也全部直接操作 `dict/list`。因此不能只在 dataclass 上增加 `profile_id`，需要以 ConfigRepository 为真实数据入口。

### 2.2 缓存和生成产物

- `backend/utils/subscription_cache.py` 固定使用 `/data/subscribes/<sub_id>.json`
- `backend/common/config.py` 固定使用 `/data/providers`
- `backend/utils/rule_utils.py` 使用全局规则目录
- `backend/routes/generate.py` 固定保存 `/data/config.yaml` 和 `/data/config.conf`

即使只给 `config.json` 增加多配置数组，缓存和生成文件仍会互相覆盖。

### 2.3 REST API

现有 API 都没有配置上下文：

```text
/api/subscriptions
/api/rules
/api/proxy-groups
/api/generate/mihomo
/api/config/mihomo
/api/agents/<id>/push-config
```

所有路由内部直接 `get_config()` 或直接引用 `config_data`。

### 2.4 Agent 推送

核心：`backend/routes/agents.py::push_config_to_agent()`

当前根据 Agent 的 `service_type` 生成配置，但生成源永远是全局 `config_data`：

```python
generate_mihomo_config(config_data, ...)
generate_mosdns_config(config_data, ...)
```

因此目前不可能安全地让 `.28` 使用 `100.127.0.0/17`，同时让 `.29` 使用 `100.126.0.0/17` 并都由同一 ConfigFlow 管理。

### 2.5 MCP

MCP 工具通过 `backend/mcp_server/invoker.py` 进程内调用现有 REST API，这是好的设计，可继续复用。

问题是 MCP 当前无状态；工具没有 `profile_id` 参数，因此所有调用都落到唯一全局配置。

### 2.6 前端

前端没有集中式配置状态库，每个页面直接调用现有 API。`frontend/src/api/index.ts` 的 Axios 拦截器当前只注入 Authorization。

这反而使改造相对容易：可以在 Axios 请求拦截器中统一注入 Profile 上下文，并通过顶栏选择器切换。

## 3. 推荐领域模型

### 3.1 系统级数据

不属于任何 Profile：

```json
{
  "schema_version": 2,
  "active_profile_id": "default",
  "profiles": [
    {
      "id": "main-28",
      "name": "主代理 .28",
      "description": "Mihomo/MosDNS 主实例",
      "created_at": "...",
      "updated_at": "..."
    }
  ],
  "agents": [],
  "system_config": {
    "server_domain": "...",
    "github_proxy_domain": {},
    "config_token": "..."
  },
  "backup": {}
}
```

建议全局保存：

- Profile 索引；
- Agent 注册、心跳和监控元数据；
- 登录/认证相关系统设置；
- ConfigFlow 服务地址；
- 全局备份配置。

### 3.2 Profile 级数据

每个 Profile 独立保存：

```json
{
  "subscriptions": [],
  "nodes": [],
  "subscription_aggregations": [],
  "rule_configs": [],
  "rule_library": [],
  "proxy_groups": [],
  "mihomo": {},
  "mosdns": {},
  "surge": {}
}
```

### 3.3 Agent 绑定

为 Agent 增加：

```json
{
  "profile_id": "main-28",
  "service_type": "mihomo"
}
```

同一个 Profile 可绑定多个 Agent；一个 Agent 默认只绑定一个 Profile。

你的实际场景：

```text
main-28
  ├─ ros-mihomo
  └─ ros-mosdns

backup-29
  ├─ ros-mihomo29
  └─ ros-mosdns29

nas-aio
  ├─ aio-mihomo
  └─ aio-mosdns
```

`.28/.29` 可分别配置：

```text
main-28 fake-IP   = 100.127.0.0/17
backup-29 fake-IP = 100.126.0.0/17
```

这样 ConfigFlow 才能安全推送，不会把 `.28` 配置覆盖到 `.29`。

## 4. 推荐磁盘布局

```text
/data/
├─ system.json
├─ profiles/
│  ├─ main-28/
│  │  ├─ config.json
│  │  ├─ subscribes/
│  │  ├─ providers/
│  │  ├─ rules/
│  │  └─ generated/
│  │     ├─ config.yaml
│  │     └─ config.conf
│  ├─ backup-29/
│  │  └─ ...
│  └─ nas-aio/
│     └─ ...
├─ logs/
└─ app.log
```

不要让不同 Profile 共享：

- subscription cache；
- provider 文件；
- 本地规则缓存；
- 生成文件；
- MosDNS 自定义文件。

即使两个 Profile 的 subscription ID 相同，也不能覆盖彼此。

## 5. 后端实现方案

### 5.1 引入 ConfigRepository

新增：

```text
backend/common/config_repository.py
backend/common/profile_context.py
```

建议接口：

```python
class ConfigRepository:
    def list_profiles(self): ...
    def create_profile(self, metadata, clone_from=None): ...
    def get_profile(self, profile_id): ...
    def save_profile(self, profile_id, data): ...
    def delete_profile(self, profile_id): ...
    def export_profile(self, profile_id): ...
    def import_profile(self, profile_id, data): ...
    def profile_dir(self, profile_id): ...
```

写入要求：

1. 每个 Profile 独立锁；
2. 写入临时文件；
3. `fsync` 后 `os.replace()` 原子替换；
4. 保留最近一次有效副本；
5. 校验 profile ID，防止路径穿越。

### 5.2 请求级 ProfileContext

建议用 Flask `g` 或 `contextvars.ContextVar`，不要在请求开始时替换全局 `config_data`。

解析优先级：

```text
显式 URL profile_id
→ X-ConfigFlow-Profile 请求头
→ query 参数 profile
→ system.active_profile_id
→ default
```

推荐显式 URL 用于外部配置和危险操作；Header 用于前端普通 CRUD。

Profile 上下文最好同时进入请求日志，便于审计一次规则修改、生成或推送究竟作用于哪套配置。

### 5.3 兼容现有路由

生产级推荐两层兼容：

```text
旧：/api/rules
新：/api/profiles/<profile_id>/rules
```

旧路由解析到 `active_profile_id`，保证升级后旧客户端继续工作。

外部订阅 URL：

```text
/api/config/<profile_id>/mihomo?token=...
/api/config/<profile_id>/surge?token=...
/api/config/<profile_id>/mosdns?token=...
```

旧 URL：

```text
/api/config/mihomo
```

继续指向默认 Profile。

### 5.4 降低改造面

短期兼容可以保留 `get_config()`，改为：

```python
def get_config(profile_id=None):
    return repository.get_profile(resolve_profile_id(profile_id))
```

但应逐步消除模块级 `from ... import config_data`。全局代理对象虽然能减少代码改动，但在并发 Web/MCP/后台任务中容易串 Profile，不建议作为最终方案。

所有生成器本身已经接收 `config_data` 参数，因此生成器核心无需重写；只需在调用点传入正确 Profile 数据。

## 6. REST API 设计

### Profile 管理

```text
GET    /api/profiles
POST   /api/profiles
GET    /api/profiles/<id>
PUT    /api/profiles/<id>
DELETE /api/profiles/<id>
POST   /api/profiles/<id>/clone
POST   /api/profiles/<id>/activate
GET    /api/profiles/<id>/export
POST   /api/profiles/<id>/import
```

删除限制：

- 默认 Profile 不可删除；
- 被 Agent 绑定的 Profile 不可直接删除；
- 删除前必须确认生成产物和缓存范围；
- 不允许静默把 Agent 迁移到默认 Profile。

### Profile 内资源

推荐显式路径：

```text
/api/profiles/<id>/subscriptions
/api/profiles/<id>/nodes
/api/profiles/<id>/rules
/api/profiles/<id>/rule-library
/api/profiles/<id>/proxy-groups
/api/profiles/<id>/generate/mihomo
```

为了减少一次性改动，也可先让旧资源 API 接受：

```text
X-ConfigFlow-Profile: main-28
```

然后分阶段迁移到显式路径。

## 7. Agent 推送设计

修改 `push_config_to_agent()`：

```python
profile_id = agent['profile_id']
config_data = repository.get_profile(profile_id)
```

推送前响应/日志必须包含：

```text
Agent 名称
Agent 地址
Profile 名称和 ID
配置类型
配置版本/hash
是否重启
```

建议 Agent 增加：

```json
{
  "profile_id": "backup-29",
  "config_revision": "sha256:..."
}
```

推送安全检查：

- 未绑定 Profile 时禁止推送；
- 检测 fake-IP CIDR 与目标预期不一致时警告/阻止；
- 推送前 preview；
- 配置 hash 未变化时跳过；
- 保留 Agent 端上一版配置以回滚。

`backend/common/agent_manager.py` 当前是持有全局 `config_data` 引用的单例，必须拆成“全局 Agent 注册仓库 + 显式 Profile 配置仓库”，不能为每个请求重新绑定一个可变全局字典。

同一个 Agent 的配置更新应加互斥锁或任务队列：当 Web、MCP 或后台任务同时向同一 Agent 推送时，只允许一个更新任务执行；后续任务按配置 revision 去重或排队。Go Agent 回报的 `config_version` 应与目标 Profile revision 对应，服务端在推送完成后进行确认。

## 8. MCP 设计

MCP 目前无状态，因此不能依赖“上一次选择了哪个配置”。

### 新增工具

```text
list_profiles
get_profile
manage_profile
clone_profile
bind_agent_profile
```

### 现有工具参数

所有配置相关工具增加可选：

```json
{
  "profile_id": "main-28"
}
```

兼容逻辑：

- 未传 `profile_id` → 默认 Profile；
- 涉及写入或 Agent 推送时，结果中必须回显 Profile ID；
- 危险工具不可依赖一个全局“当前 Profile”会话状态。

MCP 内部调用：

```python
call_api(..., headers={'X-ConfigFlow-Profile': profile_id})
```

需扩展 `backend/mcp_server/invoker.py::call_api()` 支持额外 headers 或显式 Profile 路径。

## 9. 前端实现

### 9.1 Profile Store

新增轻量 store（可用 Pinia，或先用 `provide/inject`）：

```text
frontend/src/stores/profile.ts
frontend/src/components/ProfileSwitcher.vue
frontend/src/views/Profiles.vue
```

状态：

```ts
activeProfileId
profiles
loading
switchProfile(id)
refreshProfiles()
```

### 9.2 Axios 注入

`frontend/src/api/index.ts` 请求拦截器中增加：

```ts
config.headers['X-ConfigFlow-Profile'] = activeProfileId
```

配置生成和订阅 URL 使用显式 profile path，不能只靠 Header。

### 9.3 页面刷新

Profile 切换后：

- 清空当前页面缓存；
- 取消旧 Profile 未完成请求；
- 使用 `router-view :key="activeProfileId"` 重新挂载页面；
- 页面标题/危险操作对话框显示 Profile 名称；
- 在 Agent 页面显示绑定 Profile。

Profile 选择不应只保存在全局 `localStorage`。若用户同时打开两个标签页分别编辑 `.28` 和 `.29`，共享 localStorage 会让一个标签页的切换影响另一个标签页。推荐：

- Profile ID 放入 URL（例如 `/profiles/main-28/rules`），或
- 使用 `sessionStorage` 保存每个标签页的活动 Profile；
- Axios 请求仍显式携带 Profile ID；
- 服务器不依赖浏览器“当前配置”状态。

### 9.4 UX

顶栏显示：

```text
当前配置：[主代理 .28 ▼]
```

切换时明确提示未保存编辑。克隆 Profile 是重要入口：

```text
从“主代理 .28”克隆 → “备用代理 .29”
```

然后只调整 fake-IP、DNS、节点策略和 Agent 绑定。

## 10. 迁移方案

### 首次启动迁移

检测到旧 `/data/config.json` 且不存在 `/data/system.json`：

1. 完整复制为 `/data/migrations/<timestamp>/config.json`；
2. 建立 `default` Profile；
3. 把配置级字段迁到 `/data/profiles/default/config.json`；
4. 把 `agents`、`system_config`、`backup` 移入 `/data/system.json`；
5. 把旧缓存目录移动到 default Profile；
6. 写入 `schema_version=2`；
7. 验证数量和引用一致后再提交迁移标记。

迁移必须幂等；中途失败时继续使用旧配置，不能写出空配置。

### 兼容策略

- 默认 Profile ID 固定为 `default`；
- 老 API/订阅链接默认指向 `default`；
- 老 MCP 调用不传 profile 时仍工作；
- 配置导入默认导入到当前 Profile，不覆盖系统/Agent 数据；
- 增加“导出单个 Profile”和“导出完整系统”两种模式。

## 11. 两种实现档位

### 方案 A：多配置存储 + 全局活动配置

特点：一次只能有一个 active Profile，所有 Agent 仍使用活动配置。

优点：改动小。
缺点：不能安全同时管理 `.28/.29`；切换期间并发请求和 Agent 推送风险高。

**不建议作为最终实现。** 最多作为短期 MVP UI。

### 方案 B：请求级 Profile + Agent 显式绑定

特点：多个 Profile 可并存、并发生成、分别推送。

优点：真正解决主备、多设备、多环境。
缺点：需要改造配置仓库、缓存路径、REST、Agent、MCP、前端和迁移。

**推荐方案。**

## 12. 实施阶段

### Phase 0：测试与基线（1–2 天）

- 为配置加载/保存/导入补测试；
- 为生成器建立固定快照测试；
- 为 Agent 推送建立 fake Agent 测试；
- 记录现有 REST/MCP 兼容行为。

### Phase 1：ConfigRepository + 迁移（2–3 天）

- system/profile 存储；
- 原子写入和锁；
- 旧配置迁移；
- Profile CRUD；
- 缓存目录隔离。

### Phase 2：请求上下文和 REST（2–3 天）

- ProfileContext；
- 现有 API 兼容；
- 显式 Profile API；
- 配置生成/订阅 URL 多 Profile 化。

### Phase 3：前端（2–3 天）

- ProfileSwitcher；
- Profile 管理页；
- Axios 上下文；
- 切换刷新和危险操作提示。

### Phase 4：Agent + MCP（2–3 天）

- Agent profile_id；
- 按绑定配置推送；
- MCP profile_id 参数；
- MCP Profile 工具；
- 配置 hash 和误推送防护。

### Phase 5：回归和部署（2 天）

- default 兼容；
- `.28/.29` 并行验证；
- 导入导出、备份恢复；
- 并发请求测试；
- 回滚测试。

完整生产级实现预计约 **9–14 个开发日**。只做“可保存多份并手动切换”的 MVP 约 **3–5 天**，但不满足 `.28/.29` 同时管理的核心需求。

## 13. 关键风险

1. **全局字典并发串配置**：必须使用请求级上下文/显式传参；
2. **缓存文件串 Profile**：所有派生文件必须命名空间隔离；
3. **Agent 误推送**：必须绑定 Profile，并在推送确认中展示目标；
4. **配置令牌权限**：多 Profile URL 必须明确授权模型；
5. **备份语义变化**：区分单 Profile 与全系统备份；
6. **ID 冲突**：克隆时可保留内部 ID（独立命名空间）或统一重映射，必须保持引用完整；
7. **配置迁移写空**：迁移应先写新目录、验证后原子切换；
8. **后台任务上下文**：订阅刷新、Agent 心跳、MCP 调用不能依赖可变全局 active Profile；
9. **生成文件 URL**：规则和 Provider URL 必须携带 Profile；
10. **删除 Profile**：有 Agent 绑定或活动订阅链接时必须阻止。

## 14. 推荐落地路径

推荐先从现有配置克隆三套 Profile：

```text
main-28    → ros-mihomo + ros-mosdns
backup-29  → ros-mihomo29 + ros-mosdns29
nas-aio    → aio-mihomo + aio-mosdns
```

第一版完成后至少验收：

- 三个 Profile 的规则/订阅/策略组独立；
- 同时 preview 三套配置不串数据；
- `.28/.29` fake-IP 分别保持 `100.127/17` 与 `100.126/17`；
- Agent 推送严格按绑定 Profile；
- MCP 读写指定 Profile；
- 老 `/api/config/mihomo` 和老 MCP 调用仍指向 default；
- 删除/切换 Profile 不影响其他配置。

## 15. 最终建议

采用 **方案 B：请求级 Profile + Agent 显式绑定**。

不要通过“切换全局 config_data 后复用全部 API”的方式实现，因为 Web、MCP、Agent 心跳和后台任务可以并发，会有把 `.28` 配置推给 `.29` 的真实风险。

最佳技术路线是：

```text
ConfigRepository
+ ProfileContext
+ profile-scoped storage
+ explicit Agent binding
+ MCP profile_id
+ frontend ProfileSwitcher
+ backward-compatible default profile
```

生成器已经是接收 `config_data` 的纯函数风格，核心转换逻辑可复用；真正需要重构的是配置取得、持久化、缓存命名空间和调用上下文。整体工程可控，具备较高实施可行性。
