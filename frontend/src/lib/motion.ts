/**
 * motion-v 动效预设：全站进出场语汇的唯一来源。
 *
 * 统一用 spring，避免不同页面各写一套时长导致节奏不一致。
 * App 的 MotionConfig 跟随系统减少动效偏好；CSS 动效由 tokens.css 兜底。
 */
export const SPRING = { type: 'spring', stiffness: 320, damping: 30, mass: 0.7 } as const
export const SPRING_SOFT = { type: 'spring', stiffness: 200, damping: 26 } as const

/** 卡片 / 区块进场：轻微上浮 */
export const riseIn = {
  initial: { opacity: 0, y: 10 },
  animate: { opacity: 1, y: 0 },
  transition: SPRING
}

/**
 * 列表项进场：卡片加 class="cf-list-in"（theme.css 的 CSS 动画），再用 :style 绑定本函数做 stagger。
 * 只给前 12 项写 --i，其余沿用 CSS 里的最大延迟，长列表不必逐项写内联样式。
 * 列表卡片不要再包 motion-v 的 <Motion>：每张卡挂载都会强制一次布局，静止模式也关不掉。
 */
export const listItemStyle = (index: number) => (index < 12 ? { '--i': index } : undefined)

/**
 * 进场播完或被打断后给卡片打上 data-entered，之后不再播放进场动画（main.ts 在 document 上监听）。
 * DOM 节点被移动时 CSS 动画会从头重播：拖动排序、上移下移、筛选后重排都会让卡片重新从透明上浮；
 * Sortable 的拖动预览是克隆节点，同样会带着延迟从透明开始。
 * 被打断（进场途中切走缓存页、切到静止模式）也算播过，与原来 motion-v 只进场一次一致。
 */
export function markListItemEntered(event: AnimationEvent): void {
  if (event.animationName === 'cf-list-in' && event.target instanceof Element) {
    event.target.setAttribute('data-entered', '')
  }
}

/** 弹层 / 面板 */
export const popIn = {
  initial: { opacity: 0, scale: 0.97 },
  animate: { opacity: 1, scale: 1 },
  exit: { opacity: 0, scale: 0.97 },
  transition: SPRING
}

/** 数值滚动时长（秒） */
export const COUNT_DURATION = 0.9
