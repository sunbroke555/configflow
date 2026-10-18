import { afterEach, describe, expect, it, vi } from 'vitest'
import { AxiosError, CanceledError, type InternalAxiosRequestConfig } from 'axios'
import api from '@/api'
import { explainRequestError } from '@/lib/requestError'
import { panelError } from '@/types/mihomo'

vi.mock('@/router', () => ({ default: { push: vi.fn() } }))

const originalAdapter = api.defaults.adapter
afterEach(() => { api.defaults.adapter = originalAdapter })

describe('request error explanations', () => {
  it.each([
    ['Network Error', 'ERR_NETWORK', '网络连接失败'],
    ['timeout of 30000ms exceeded', 'ECONNABORTED', '请求超时'],
    ['timeout exceeded', 'ETIMEDOUT', '请求超时'],
  ])('adds Chinese context while retaining %s and its code', async (message, code, explanation) => {
    let original!: AxiosError
    api.defaults.adapter = async config => {
      original = new AxiosError(message, code, config)
      throw original
    }
    const error = await api.get('/stats').catch(error => error)
    expect(error).toBe(original)
    expect(error.message).toContain(explanation)
    expect(error.message).toContain(message)
    expect(error.message).toContain(code)
    expect(error.code).toBe(code)
    const once = error.message
    explainRequestError(error)
    expect(error.message).toBe(once)
  })

  it.each([
    'download failed: dial tcp: i/o timeout',
    'Network Error',
    'upstream returned HTTP 503: SERVFAIL',
  ])('preserves the source response and panel detail: %s', async message => {
    const body = Object.freeze({ success: false, code: 'CORE_ERROR', message })
    api.defaults.adapter = async config => {
      throw new AxiosError('Request failed with status code 503', 'ERR_BAD_RESPONSE', config, undefined, {
        data: body, status: 503, statusText: 'Service Unavailable', headers: {}, config,
      })
    }
    const error = await api.post('/agents/test/mihomo/upgrade').catch(error => error)
    expect(error.message).toContain('请求失败（HTTP 503）')
    expect(error.message).toContain('Request failed with status code 503')
    expect(error.response.data).toBe(body)
    expect(error.response.status).toBe(503)
    expect(error.code).toBe('ERR_BAD_RESPONSE')
    expect(panelError(error)).toBe(message)
  })

  it('leaves cancellation and arbitrary errors untouched', () => {
    for (const error of [new CanceledError('canceled'), new Error('source exception'),
      new AxiosError('certificate verify failed', 'ERR_BAD_RESPONSE')]) {
      const original = error.message
      explainRequestError(error)
      expect(error.message).toBe(original)
    }
  })

  it('does not replace the missing-profile message used by recovery', () => {
    const config = {} as InternalAxiosRequestConfig
    const data = { message: 'Profile not found: deleted-profile' }
    const error = new AxiosError('Request failed with status code 404', 'ERR_BAD_REQUEST', config, undefined, {
      data, status: 404, statusText: 'Not Found', headers: {}, config,
    })
    explainRequestError(error)
    expect(error.response?.data).toBe(data)
    expect(data.message).toBe('Profile not found: deleted-profile')
  })
})
