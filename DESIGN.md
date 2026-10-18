# ConfigFlow 设计规范

前端界面（`frontend/`，Vue 3 + shadcn-vue + Tailwind v4）的设计约定。改 UI 前先读本文；
本文与代码冲突时以代码为准，并顺手修正本文。

- 色彩正本：`frontend/src/styles/theme.css`（全站唯一色彩来源）
- 布局与页面 token：`frontend/src/styles/tokens.css`（`--cf-*`，全部反向引用 theme.css）
- 原型：`doc/design/flow-redesign/`；早期规格：`doc/design/ui-redesign-spec.md`

## 1. 风格定位

Claude 暖色系的「配置流」控制台：炭黑 / 象牙底 + 陶土橙主色，状态色取橄榄、牛皮纸、天空蓝，
全部低饱和、偏暖、同一色温。标题和大数字用衬线体，数据用等宽体，界面安静、信息密度中高。

- 深色（炭黑）为默认主题，浅色（象牙）为第二主题，另有「跟随系统」
- 不用高饱和色块、不用霓虹发光、不引入外部字体文件
- 视觉装饰（点阵、暖光、纸感颗粒）都可在 Tweaks 里关闭，不能承载信息

## 2. 颜色

**只用语义 token，不写裸色值。** Tailwind 里写 `bg-card`、`text-muted-foreground`、
`border-border-strong`、`text-primary-accent` 等；CSS 里写 `var(--card)` 或 `var(--cf-*)`。

| 语义 | 深色 | 浅色 | 用途 |
|---|---|---|---|
| `background` | `#191817` | `#f5f3ec` | 页面底 |
| `card` | `#242321` | `#fbfaf6` | 卡片、面板 |
| `popover` | `#2a2926` | `#fffefb` | 弹层、菜单 |
| `secondary` / `muted` | `#2c2b28` | `#efece3` | 次级表面、输入底 |
| `accent` | `#34322e` | `#e8e4d8` | 悬停、选中底 |
| `foreground` | `#f0eee6` | `#1f1e1b` | 正文 |
| `muted-foreground` | `#a9a598` | `#67645b` | 次要文字（不再额外降对比度） |
| `border` / `border-strong` | 前景 8% / 14% | 前景 9% / 16% | 分隔线 / 强调边 |

**主色成对使用，不能混用：**

| token | 用途 |
|---|---|
| `primary` + `primary-foreground` | 实心按钮填充与其文字（深色 7:1，浅色 4.6:1，满足 WCAG AA） |
| `primary-accent` | 文字、图标、链接、聚焦环上的主色 |
| `primary-soft` | 主色浅底（选中项、提示条） |

状态色同理各有三档：`success / warning / destructive / info` 是填充，
`*-accent` 是文字和图标，`*-soft` 是浅底。`info`（天空蓝）专用于区分资源作用域。

强调色变体（Tweaks）：`clay`（陶土，默认）/ `kraft`（牛皮纸）/ `sky`（天空蓝），
写在 `<html data-accent>`，只替换主色族，其余语义色不变。新增颜色时在 theme.css
的深浅两套里都要给值，并用 `color-mix` / `oklch(from …)` 从已有色派生，不另造色相。

## 3. 字体

| 角色 | token / 类 | 用在 |
|---|---|---|
| 正文 | `--font-sans`（系统 UI + 苹方 / 微软雅黑） | 默认 14px / 1.5 |
| 展示 | `.font-display`（`--font-serif`，字重 500，字距 -0.02em） | 页面 h1、统计大数字、品牌字 |
| 等宽 | `font-mono` / `.cf-mono` | 版本号、哈希、地址、端口、配置内容 |
| 数字 | `.num` / `.cf-num` | 所有会变化的数字，等宽防跳动 |

字号阶梯（px）：页面标题 34（移动端 26）· 区块标题 14 半粗 · 正文 13–14 · 辅助 12–12.5 · 标签 11–11.5。

## 4. 几何与间距

- 圆角单一来源 `--radius: 0.625rem`：`xs/sm/md/lg/xl` = 5 / 7.5 / 8.75 / 10 / 15px；
  大卡片 `SectionCard` 用 18px；胶囊 `999px`
- 间距 4px 网格：`--cf-sp-1…6` = 4 / 8 / 12 / 16 / 20 / 32
- 控件高：常规 34px，大 40px；触控目标至少 44px（`--cf-touch`）
- 布局：顶栏 60px、左侧导航 236px、移动端底部 Tab 56px、内容最大宽 1360px
- 阴影只有两档：`shadow-surface`（卡片）和 `shadow-overlay`（弹层）
- 页面与侧栏层级为 `z-10`，顶栏 `z-20`、移动底栏 `z-30`、Tweaks `z-40`；均低于弹窗与遮罩的 `z-50`，保证弹窗打开时整页背景一起变暗。
- Tweaks 使用顶部按钮锚定的 Popover，优先在按钮下方展开；点击外部或按 Esc 关闭，小屏空间不足时面板内部滚动。

## 5. 组件

优先复用，不重复造：

- **基础件**：`components/ui/` 下的 shadcn-vue（Button、Dialog、DropdownMenu、Select、Sheet、Tabs、Tooltip …），用 shadcn CLI 增改
- **页面骨架**：`PageHeader`（衬线大标题 + 右侧操作）、`SectionCard`（区块卡片）、`Toolbar`、`DataTableShell`、`EmptyState`、`LoadingRows`
- **配置编辑**：生成页设置复用 `SettingsWorkspace`，统一顶部返回、说明和展开。支持 Agent 的设置在底部使用 `SettingsPublishBar`，显示配置空间与目标设备，分别提供「保存草稿」和「保存并发布」；发布沿用对应软件的校验流程，失败保留草稿，已提交后允许离开页面。视口宽度小于 1024px 或高度不超过 760px 时自动全屏；内容区独立滚动，校验详情默认折叠，退出时保护未保存修改
- **数据展示**：`StatTile`、`Sparkline`、`AnimatedNumber`、`StatusDot`、`.chip`（`chip-acc / ok / warn / bad / sky`）
- **选择**：`Segmented`（定一个参数）、`ViewToggle`（列表 / 卡片切换）、`MultiSelect`、`button.chip[aria-pressed]`（筛选：点完列表变短）
- **反馈**：只走 `@/lib/feedback`：`notify.*`（vue-sonner 轻提示）、`confirm()` / `confirmDanger()` / `choose()`（由 App.vue 里的 `ConfirmHost` 渲染）。不要在页面里单独挂确认框
- **图标**：`@lucide/vue`，常规 16px（`size-4`），按钮内 14px（`size-3.5`）；纯图标按钮必须有 `aria-label`
- **表格**：数据密集的区域用 `.cf-table` / `.cf-table-wrap` 列表，不要做成卡片墙；宽表在自身容器内横向滚动。表头与该列内容保持同向对齐，右侧操作列的表头用 `text-right`；默认对齐放在组件层，允许列上的对齐类覆盖

手机布局（< 640px）沿用桌面的操作与状态：顶栏将配置空间独立成一行，页面操作按钮按两列排列，搜索与筛选独立分行；区块操作放到标题下方。路由器列表和可编辑来源列表使用 `.cf-mobile-records` 在同一份表格结构中纵向排布，保留表格语义、编辑值和事件；其余宽数据表仍在自身容器内滚动。DNS 排行在手机上统一为 288px 高，桌面高度保持不变。

短操作优先并排：Mihomo 的模式、内核与刷新按内容宽度排列，窄屏空间不足再换行；节点库全选放在搜索框右侧。节点筛选按来源、地区、协议各占一行，可横向滑动；胶囊视觉高 30px，含上下透明点击区仍为 44px。移动底栏「配置」对应整个「当前配置」分组，默认进入配置生成。

手机上的交互胶囊统一使用紧凑外观和 44px 点击区。工具栏多项分段筛选在自身区域横滑；策略规则列表也独立横滑，保证右侧编辑等操作可访问。少量筛选和短操作可同排，空的批量操作栏不占位。

手机上（< 768px），`FormField` 的辅助说明和 `SectionCard` 的区块说明收进标题旁的问号；桌面仍显示原说明。使用 `persistentHint` / `persistentDescription` 保留必须直接可见的警告、时间和状态；没有标题的说明也始终可见。问号沿用 `FieldHelp`，不触发表单提交或开关；带问号的标签行高至少 32px，点击区仍为 44px，与输入框至少留 6px 间距。同一网格内的字段保持标签行对齐，普通无说明字段不增加行高。

## 6. 动效

- 缓动：`--ease-tech` / `--cf-ease` = `cubic-bezier(0.32, 0.72, 0, 1)`，`--ease-flow` = `cubic-bezier(0.22, 1, 0.36, 1)`；常规时长 200ms
- 组件动画用 `motion-v`，弹层进出场用 `tw-animate-css`（shadcn 自带）
- 必须同时尊重两个开关：系统 `prefers-reduced-motion` 和 Tweaks 的 `data-motion='off'`
- 动效默认「静止」，保留用户已保存的选择。Canvas 流向图在静止时只响应数据、布局、配色和交互变化重绘；页面隐藏或图表移出视口时取消帧调度，恢复可见后合并更新。
- 关闭 CSS 动效时把动画时长压到 0.01ms，**不能**用 `animation-play-state: paused`：
  reka-ui 等不到 `animationend`，菜单关不掉、对话框透明且整页无法点击

## 7. 状态与无障碍

- 每个可交互元素都要有默认 / 悬停 / 聚焦 / 禁用四态；聚焦统一用 `:focus-visible` 的 2px 主色描边
- 状态不能只靠颜色：状态点旁配文字，chip 写清含义
- 加载中有加载态（`LoadingRows`、按钮内 `Loader2` 转圈并禁用），失败要给出可读原因，空数据用 `EmptyState`
- 异步操作必须有反馈：点了按钮后要么出现加载态，要么出现 `notify` 提示，不能静默
- 只给读屏的说明文字用 `.cf-sr`
- 页面不允许横向滚动；移动端（< md）布局要单独检查

## 8. 改 UI 时的检查清单

1. 颜色、圆角、阴影、间距全部来自 token，深浅两套主题都看过
2. 三种强调色（clay / kraft / sky）下文字对比度仍然够
3. 关闭动效、关闭纹理、系统减少动效时页面仍可正常操作
4. 移动端宽度（375px）无横向滚动，触控目标 ≥ 44px
5. 用真实数据验证：长名称、大数字、空列表、加载失败
