import { mount } from '@vue/test-utils'
import PrimeVue from 'primevue/config'
import { describe, expect, it } from 'vitest'

import DigestRecipients from '@/components/DigestRecipients.vue'
import type { DigestRecipient } from '@/lib/digest'

function recipient(over: Partial<DigestRecipient> = {}): DigestRecipient {
  return {
    id: 1,
    email: 'one@example.test',
    display_name: null,
    enabled: true,
    added_by: 'fabian',
    added_at: '2026-08-30T10:00:00Z',
    ...over,
  }
}

function mountRecipients(props: Partial<InstanceType<typeof DigestRecipients>['$props']> = {}) {
  return mount(DigestRecipients, {
    global: { plugins: [PrimeVue] },
    props: { recipients: [], digestEnabled: true, busy: null, error: null, ...props },
  })
}

describe('DigestRecipients', () => {
  it('lists the addresses and says so when there are none', () => {
    expect(mountRecipients().find('[data-test="digest-recipients-empty"]').exists()).toBe(true)
    const wrapper = mountRecipients({
      recipients: [recipient(), recipient({ id: 2, email: 'two@example.test' })],
    })
    expect(wrapper.findAll('[data-test="digest-recipient-row"]')).toHaveLength(2)
  })

  it('emits a normalised address for a valid entry', async () => {
    const wrapper = mountRecipients({ recipients: [recipient()] })
    await wrapper.find('[data-test="digest-recipient-input"]').setValue('  New@Example.TEST ')
    await wrapper.find('[data-test="digest-recipient-name"]').setValue(' Ada ')
    await wrapper.find('[data-test="digest-recipient-add"]').trigger('click')

    expect(wrapper.emitted('add')).toEqual([[{ email: 'new@example.test', display_name: 'Ada' }]])
    // the form clears itself only once the entry was accepted
    expect(wrapper.find('[data-test="digest-recipient-local-error"]').exists()).toBe(false)
  })

  it('refuses a malformed address and a duplicate without emitting anything', async () => {
    const wrapper = mountRecipients({ recipients: [recipient({ email: 'one@example.test' })] })

    await wrapper.find('[data-test="digest-recipient-input"]').setValue('not-an-address')
    await wrapper.find('[data-test="digest-recipient-add"]').trigger('click')
    expect(wrapper.find('[data-test="digest-recipient-local-error"]').text()).toMatch(/not a valid/)

    await wrapper.find('[data-test="digest-recipient-input"]').setValue('ONE@example.test')
    await wrapper.find('[data-test="digest-recipient-add"]').trigger('click')
    expect(wrapper.find('[data-test="digest-recipient-local-error"]').text()).toMatch(
      /already a recipient/,
    )
    expect(wrapper.emitted('add')).toBeUndefined()
  })

  it('emits a toggle and a removal for one row', async () => {
    const wrapper = mountRecipients({
      recipients: [recipient({ id: 5 }), recipient({ id: 6, email: 'two@example.test' })],
    })
    await wrapper.findAll('[data-test="digest-recipient-remove"]')[0].trigger('click')
    expect(wrapper.emitted('remove')).toEqual([[5]])

    const toggle = wrapper.findAllComponents({ name: 'ToggleSwitch' })[0]
    toggle.vm.$emit('update:modelValue', false)
    await wrapper.vm.$nextTick()
    expect(wrapper.emitted('toggle')).toEqual([[{ id: 5, enabled: false }]])
  })

  it('blocks the last enabled recipient of an armed digest, and says why', () => {
    const wrapper = mountRecipients({
      recipients: [recipient({ id: 5 }), recipient({ id: 6, enabled: false })],
      digestEnabled: true,
    })
    expect(wrapper.find('[data-test="digest-recipient-guard"]').text()).toMatch(
      /last enabled recipient/,
    )
    const removes = wrapper.findAll('[data-test="digest-recipient-remove"]')
    expect(removes[0].attributes('disabled')).toBeDefined()
    // the disabled one may still be removed — it is not holding the digest up
    expect(removes[1].attributes('disabled')).toBeUndefined()
  })

  it('lets the last recipient go once the digest is off', () => {
    const wrapper = mountRecipients({ recipients: [recipient({ id: 5 })], digestEnabled: false })
    expect(wrapper.find('[data-test="digest-recipient-guard"]').exists()).toBe(false)
    expect(
      wrapper.find('[data-test="digest-recipient-remove"]').attributes('disabled'),
    ).toBeUndefined()
  })

  it('shows the API refusal it was handed', () => {
    const wrapper = mountRecipients({ error: 'that address is already a recipient' })
    expect(wrapper.find('[data-test="digest-recipient-error"]').text()).toMatch(/already/)
  })
})
