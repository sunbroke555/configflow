/**
 * 解析 Agent 日志对话框里的原始文本。覆盖 OxiDNS（tracing）、Mihomo（logrus
 * 文本格式）和 Go Agent（标准库 log）三种格式；认不出的行原样保留，
 * 并作为上一条的续行显示，不推断级别。
 */

export type AgentLogLevel = 'TRACE' | 'DEBUG' | 'INFO' | 'WARN' | 'ERROR'

export interface AgentLogField {
  key: string
  value: string
}

export interface AgentLogEntry {
  id: number
  /** 原始行，用于悬停与复制 */
  raw: string
  /** YYYY-MM-DD；没有时间的行为空 */
  date: string
  /** HH:MM:SS */
  time: string
  level: AgentLogLevel | ''
  /** 完整来源（模块路径与行号） */
  source: string
  /** 来源的最后一段，列表中显示 */
  sourceShort: string
  message: string
  fields: AgentLogField[]
  /** 续行：认不出格式，接在上一条后显示 */
  continuation: boolean
}

const LEVEL_OF: Record<string, AgentLogLevel> = {
  TRACE: 'TRACE',
  DEBUG: 'DEBUG',
  INFO: 'INFO',
  WARN: 'WARN',
  WARNING: 'WARN',
  ERROR: 'ERROR',
  ERR: 'ERROR',
  FATAL: 'ERROR',
  PANIC: 'ERROR',
  CRITICAL: 'ERROR'
}

// 2026-10-20T23:22:20+08:00 T+341494.611 INFO oxidns::plugin::executor::download:184:download completed …
const TRACING_LINE = /^(\d{4}-\d{2}-\d{2})[T ](\d{2}:\d{2}:\d{2})(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?\s+(?:T\+[\d.]+\s+)?(TRACE|DEBUG|INFO|WARN|WARNING|ERROR)\s+(?:(\w+(?:::\w+)*:\d+):(?=\S))?\s*([\s\S]*)$/
// time="2026-10-20T23:22:20.123+08:00" level=info msg="[TCP] …"
const LOGRUS_LINE = /^time="(\d{4}-\d{2}-\d{2})T(\d{2}:\d{2}:\d{2})[^"]*"\s+level=(\w+)\s+msg="((?:[^"\\]|\\.)*)"\s*([\s\S]*)$/
// 2026/10/20 23:39:46 Starting API server on 0.0.0.0:22919
const GO_LOG_LINE = /^(\d{4})\/(\d{2})\/(\d{2}) (\d{2}:\d{2}:\d{2})(?:\.\d+)? ([\s\S]*)$/

const FIELD_START = /\s([A-Za-z_][\w.]*)=/
const FIELD = /([A-Za-z_][\w.]*)=("(?:[^"\\]|\\.)*"|\S*)/y

const unquote = (value: string) =>
  value.length >= 2 && value.startsWith('"') && value.endsWith('"')
    ? value.slice(1, -1).replace(/\\(["\\])/g, '$1')
    : value

/** 把 "正文 a=1 b=2" 拆成正文与参数；无法完整识别的尾部并回正文 */
export const splitFields = (text: string): { message: string; fields: AgentLogField[] } => {
  const start = FIELD_START.exec(text)
  if (!start) return { message: text, fields: [] }
  const fields: AgentLogField[] = []
  let index = start.index + 1
  while (index < text.length) {
    FIELD.lastIndex = index
    const match = FIELD.exec(text)
    if (!match) break
    fields.push({ key: match[1], value: unquote(match[2]) })
    index = FIELD.lastIndex
    while (text[index] === ' ') index++
  }
  if (index < text.length) {
    // 参数后面又出现普通文字，说明不是纯参数结构，保持原样
    return { message: text, fields: [] }
  }
  return { message: text.slice(0, start.index).trim(), fields }
}

const shortSource = (source: string) => {
  const parts = source.split('::')
  return parts[parts.length - 1]
}

let nextId = 1

export const parseAgentLogLine = (raw: string): AgentLogEntry => {
  const base = { id: nextId++, raw, date: '', time: '', level: '' as const, source: '', sourceShort: '', message: raw, fields: [], continuation: false }

  let m = TRACING_LINE.exec(raw)
  if (m) {
    const source = m[4] || ''
    return { ...base, date: m[1], time: m[2], level: LEVEL_OF[m[3]], source, sourceShort: shortSource(source), ...splitFields(m[5]) }
  }

  m = LOGRUS_LINE.exec(raw)
  if (m) {
    const { message, fields } = splitFields(unquote(`"${m[4]}"`))
    const extra = m[5] ? splitFields(` ${m[5]}`).fields : []
    return { ...base, date: m[1], time: m[2], level: LEVEL_OF[m[3].toUpperCase()] || '', message, fields: [...fields, ...extra] }
  }

  m = GO_LOG_LINE.exec(raw)
  if (m) {
    return { ...base, date: `${m[1]}-${m[2]}-${m[3]}`, time: m[4], ...splitFields(m[5]) }
  }

  return { ...base, continuation: true }
}

export const parseAgentLogs = (text: string): AgentLogEntry[] =>
  text
    .split(/\r?\n/)
    .filter(line => line.trim() !== '')
    .map(parseAgentLogLine)
