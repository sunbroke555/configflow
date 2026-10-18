import { isAxiosError, isCancel } from 'axios'

/** Explain only Axios-generated transport errors; preserve server/core replies. */
export function explainRequestError(error: unknown): void {
  if (!isAxiosError(error) || isCancel(error)) return
  const original = error.message
  let explanation = ''
  if (error.code === 'ERR_NETWORK' && original === 'Network Error') {
    explanation = '网络连接失败，请检查网络和服务是否可用。'
  } else if (['ECONNABORTED', 'ETIMEDOUT'].includes(error.code || '') && /^timeout(?: of \d+ms)? exceeded$/.test(original)) {
    explanation = '请求超时，请稍后重试。'
  } else if (/^Request failed with status code \d{3}$/.test(original) && error.response) {
    explanation = `请求失败（HTTP ${error.response.status}），请查看服务器返回的详情。`
  }
  if (explanation) {
    const code = error.code ? `；错误码：${error.code}` : ''
    error.message = `${explanation}原始错误：${original}${code}`
  }
}
