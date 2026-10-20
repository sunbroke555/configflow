export function optionHelp(conditions: string, behavior: string): string {
  return `【适用条件】\n${conditions}\n\n【工作原理】\n${behavior}`
}

export function optionCopy(hint: string, conditions: string, behavior: string) {
  return { hint, help: optionHelp(conditions, behavior) }
}
