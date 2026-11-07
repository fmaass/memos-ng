import { flushPromises, mount } from '@vue/test-utils'
import PrimeVue from 'primevue/config'
import { afterEach, describe, expect, it } from 'vitest'

import DigestPreviewDrawer from '@/components/DigestPreviewDrawer.vue'
import type { DigestPreview } from '@/composables/useDigestActions'

const PREVIEW: DigestPreview = {
  html: '<p>the mail</p>',
  plain: 'the mail',
  memo_count: 2,
  parts: 1,
  delivery_mode: 'attachments',
  selected: [
    { uid: 'aaa', memo_id: 1, created_at: '2026-08-30T05:00:00Z', local_day: '2026-08-30' },
  ],
  deferred: [{ uid: 'bbb', reason: 'card exceeds max_email_bytes after shedding' }],
}

// PrimeVue's Drawer teleports to the body and mounts that portal a tick after the component, so
// the assertions read the document rather than the wrapper and every mount is awaited.
async function mountDrawer(props: Partial<InstanceType<typeof DigestPreviewDrawer>['$props']> = {}) {
  const wrapper = mount(DigestPreviewDrawer, {
    attachTo: document.body,
    global: { plugins: [PrimeVue] },
    props: { visible: true, preview: PREVIEW, loading: false, error: null, ...props },
  })
  await flushPromises()
  return wrapper
}

afterEach(() => {
  document.body.innerHTML = ''
})

describe('DigestPreviewDrawer', () => {
  it('renders the mail in a sandboxed frame with its counts', async () => {
    await mountDrawer()
    const frame = document.querySelector('[data-test="digest-preview-frame"]')!
    expect(frame.getAttribute('srcdoc')).toBe('<p>the mail</p>')
    expect(frame.getAttribute('sandbox')).toBe('')
    expect(document.querySelector('[data-test="digest-preview-summary"]')!.textContent).toMatch(
      /2 memos · 1 part/,
    )
  })

  it('narrows the frame to the mobile width when asked', async () => {
    const wrapper = await mountDrawer()
    const before = document.querySelector<HTMLElement>('[data-test="digest-preview-frame"]')!
    expect(before.style.width).toBe('720px')

    const widths = wrapper.findComponent({ name: 'SelectButton' })
    widths.vm.$emit('update:modelValue', 390)
    await wrapper.vm.$nextTick()
    expect(
      document.querySelector<HTMLElement>('[data-test="digest-preview-frame"]')!.style.width,
    ).toBe('390px')
  })

  it('lists what was held back, with the reason', async () => {
    await mountDrawer()
    const rows = document.querySelectorAll('[data-test="digest-preview-deferred-row"]')
    expect(rows).toHaveLength(1)
    expect(rows[0].textContent).toMatch(/card exceeds max_email_bytes/)
    expect(document.querySelectorAll('[data-test="digest-preview-selected-row"]')).toHaveLength(1)
  })

  it('says so when nothing was held back', async () => {
    await mountDrawer({ preview: { ...PREVIEW, deferred: [] } })
    expect(document.querySelector('[data-test="digest-preview-deferred-empty"]')).not.toBeNull()
  })

  it('shows the render in progress and a failed render instead of a stale frame', async () => {
    await mountDrawer({ loading: true, preview: null })
    expect(
      document.querySelector('[data-test="digest-preview-loading"]')!.getAttribute('aria-live'),
    ).toBe('polite')
    expect(document.querySelector('[data-test="digest-preview-frame"]')).toBeNull()

    document.body.innerHTML = ''
    await mountDrawer({ loading: false, preview: null, error: 'rendering failed' })
    const error = document.querySelector('[data-test="digest-preview-error"]')!
    expect(error.textContent).toMatch(/rendering failed/)
    expect(error.getAttribute('role')).toBe('alert')
    
    expect(document.querySelector('[data-test="digest-preview-frame"]')).toBeNull()
  })

  it('names the linked day and the link in link mode (MEMNG-1246)', async () => {
    await mountDrawer({
      preview: {
        ...PREVIEW,
        delivery_mode: 'link',
        link_day: '2026-08-30',
        link_older_days: 2,
        link_url: 'https://dami.example/',
      },
    })
    expect(document.querySelector('[data-test="digest-preview-summary"]')!.textContent).toContain(
      'link mail to 2026-08-30 + 2 older days',
    )
    expect(document.querySelector('[data-test="digest-preview-link"]')!.textContent).toContain(
      'https://dami.example/',
    )
  })

  it('shows no link row for the attachments mail', async () => {
    await mountDrawer()
    expect(document.querySelector('[data-test="digest-preview-link"]')).toBeNull()
    expect(document.querySelector('[data-test="digest-preview-summary"]')!.textContent).toContain(
      '2 memos · 1 part',
    )
  })
})
