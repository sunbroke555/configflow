/** Normalize a message ending before appending another sentence; keep its body intact. */
export function stripTrailingPunctuation(message: string): string {
  return message.replace(/[。.!！?？…；;，,：:\s]+$/u, '')
}
