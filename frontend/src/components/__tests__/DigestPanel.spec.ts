import { flushPromises, mount } from '@vue/test-utils'
import PrimeVue from 'primevue/config'
import SelectButton from 'primevue/selectbutton'
import { ref } from 'vue'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import DigestPanel from '@/components/DigestPanel.vue'
import type { DigestRecipient, DigestRun, DigestSettings } from '@/lib/digest'

const SERVER: DigestSettings = {
  enabled: true,
  send_time: '07:00:00',
  timezone: 'Europe/Zurich',
  visibility_scope: 'PUBLIC_PROTECTED',
  start_date: '2026-06-01',
  grace_minutes: 120,
  max_email_bytes: 20971520,
  max_videos_per_memo: 4,
  max_memos_per_run: 50,
  mail_title: 'Daily memos',
  from_name: 'memos',
  from_email: '',
  hosting_enabled: true,
  dropbox_folder: '/memos-digest',
  retention_days: 90,
  link_images_full: true,
  updated_at: '2026-08-30T10:00:00Z',
  updated_by: 'fabian',
}

const recipients: DigestRecipient[] = [
  {
    id: 5,
    email: 'one@example.test',
    display_name: null,
    enabled: true,
    language: 'de',
    added_by: 'fabian',
    added_at: '2026-08-30T10:00:00Z',
  },
  {
    id: 6,
    email: 'two@example.test',
    display_name: 'Two',
    enabled: true,
    language: 'de',
    added_by: 'fabian',
    added_at: '2026-08-30T10:00:00Z',
  },
]

function aRun(over: Partial<DigestRun> = {}): DigestRun {
  return {
    id: 1,
    groups_attempted: [],
    groups_accepted: [],
    trigger: 'scheduled',
    started_at: '2026-08-30T05:00:00Z',
    finished_at: '2026-08-30T05:01:00Z',
    status: 'sent',
    memos_sent: 7,
    parts: 1,
    parts_sent: 1,
    parts_claimed: 1,
    recipients: 2,
    error: null,
    local_date: '2026-08-30',
    ...over,
  }
}

const digest = vi.hoisted(() => ({
  settings: undefined as unknown,
  runs: undefined as unknown,
  enabledError: undefined as unknown,
  saveConfig: vi.fn(),
  setEnabled: vi.fn(),
  addRecipient: vi.fn(),
  setRecipientEnabled: vi.fn(),
  removeRecipient: vi.fn(),
  refreshRuns: vi.fn(),
}))
const actions = vi.hoisted(() => ({
  loadPreview: vi.fn(),
  sendTest: vi.fn(),
  runNow: vi.fn(),
  preview: undefined as unknown,
  options: undefined as { onRunChanged?: () => unknown } | undefined,
}))

vi.mock('@/composables/useDigest', () => ({
  useDigest: () => ({
    settings: digest.settings,
    recipients: ref(recipients),
    runs: digest.runs,
    hosting: ref({ configured: true, account: 'household@example.test' }),
    savingConfig: ref(false),
    togglingEnabled: ref(false),
    recipientBusy: ref(null),
    configError: ref(null),
    enabledError: digest.enabledError,
    recipientError: ref(null),
    notice: ref(null),
    clearNotice: vi.fn(),
    saveConfig: digest.saveConfig,
    setEnabled: digest.setEnabled,
    addRecipient: digest.addRecipient,
    setRecipientEnabled: digest.setRecipientEnabled,
    removeRecipient: digest.removeRecipient,
    refreshRuns: digest.refreshRuns,
  }),
}))

vi.mock('@/composables/useDigestActions', () => ({
  useDigestActions: (options: { onRunChanged?: () => unknown }) => {
    actions.options = options
    return {
      preview: actions.preview,
      previewing: ref(false),
      previewError: ref(null),
      sendingTest: ref(false),
      runningNow: ref(false),
      watchedRun: ref(null),
      actionError: ref(null),
      actionNotice: ref(null),
      clearActionMessages: vi.fn(),
      loadPreview: actions.loadPreview,
      sendTest: actions.sendTest,
      runNow: actions.runNow,
    }
  },
}))

const DrawerStub = {
  name: 'DigestPreviewDrawer',
  props: ['visible', 'preview', 'loading', 'error'],
  emits: ['update:visible'],
  template: '<div data-test="drawer-stub" :data-visible="String(visible)" />',
}

function mountPanel(options: { attach?: boolean } = {}) {
  return mount(DigestPanel, {
    ...(options.attach ? { attachTo: document.body } : {}),
    global: { plugins: [PrimeVue], stubs: { DigestPreviewDrawer: DrawerStub } },
  })
}

describe('DigestPanel', () => {
  beforeEach(() => {
    digest.settings = ref({ ...SERVER })
    digest.runs = ref([aRun()])
    digest.enabledError = ref(null)
    actions.preview = ref(null)
    digest.saveConfig.mockResolvedValue(true)
    digest.setEnabled.mockResolvedValue(true)
    digest.addRecipient.mockResolvedValue(true)
    digest.setRecipientEnabled.mockResolvedValue(true)
    digest.removeRecipient.mockResolvedValue(true)
    actions.runNow.mockResolvedValue({ id: 12 })
    actions.loadPreview.mockResolvedValue(null)
    actions.sendTest.mockResolvedValue(true)
  })
  afterEach(() => vi.clearAllMocks())

  it('waits for the settings row before it renders a form', () => {
    digest.settings = ref(undefined)
    const wrapper = mountPanel()
    expect(wrapper.find('[data-test="digest-loading"]').exists()).toBe(true)
    expect(wrapper.find('[data-test="digest-schedule"]').exists()).toBe(false)
  })

  it('shows the status row, the sections and the runs table', () => {
    const wrapper = mountPanel()
    expect(wrapper.find('[data-test="digest-status"]').exists()).toBe(true)
    expect(wrapper.find('[data-test="digest-schedule"]').exists()).toBe(true)
    expect(wrapper.find('[data-test="digest-recipients"]').exists()).toBe(true)
    expect(wrapper.find('[data-test="digest-advanced"]').exists()).toBe(true)
    expect(wrapper.find('[data-test="digest-hosting"]').exists()).toBe(true)
    expect(wrapper.find('[data-test="digest-runs"]').exists()).toBe(true)
    expect(wrapper.find('[data-test="digest-next-send"]').text()).toMatch(/Europe\/Zurich/)
    expect(wrapper.find('[data-test="digest-hosting-status"]').text()).toMatch(/Configured/)
  })

  it('has no video-attachment cap field (decision 4)', () => {
    const html = mountPanel().html()
    expect(html).not.toMatch(/max_video_attach_bytes/)
    expect(html).not.toMatch(/attach/i)
  })

  it('shows the Save bar only once a grouped field is dirty, and Discard restores the server value', async () => {
    const wrapper = mountPanel()
    expect(wrapper.find('[data-test="digest-save-bar"]').exists()).toBe(false)

    await wrapper.find('[data-test="digest-mail-title"]').setValue('Something else')
    expect(wrapper.find('[data-test="digest-save-bar"]').exists()).toBe(true)

    await wrapper.find('[data-test="digest-discard"]').trigger('click')
    expect(wrapper.find('[data-test="digest-save-bar"]').exists()).toBe(false)
    expect(
      (wrapper.find('[data-test="digest-mail-title"]').element as HTMLInputElement).value,
    ).toBe('Daily memos')
    expect(digest.saveConfig).not.toHaveBeenCalled()
  })

  it('saves the whole grouped draft in one request', async () => {
    const wrapper = mountPanel()
    await wrapper.find('[data-test="digest-mail-title"]').setValue('Something else')
    await wrapper.find('[data-test="digest-save"]').trigger('click')
    await flushPromises()

    expect(digest.saveConfig).toHaveBeenCalledTimes(1)
    expect(digest.saveConfig.mock.calls[0][0]).toMatchObject({
      mail_title: 'Something else',
      send_time: '07:00',
      timezone: 'Europe/Zurich',
    })
    expect(digest.setEnabled).not.toHaveBeenCalled()
  })

  it('refuses to save a draft the API would reject, and says which field', async () => {
    const wrapper = mountPanel()
    await wrapper.find('[data-test="digest-max-memos"]').setValue('0')
    expect(wrapper.find('[data-test="digest-error-max_memos_per_run"]').text()).toBe(
      'Between 1 and 1000.',
    )
    expect(wrapper.find('[data-test="digest-save"]').attributes('disabled')).toBeDefined()

    await wrapper.find('[data-test="digest-save"]').trigger('click')
    await flushPromises()
    expect(digest.saveConfig).not.toHaveBeenCalled()
  })

  it('treats an emptied number as invalid rather than as zero', async () => {
    const wrapper = mountPanel()
    await wrapper.find('[data-test="digest-grace-minutes"]').setValue('')
    expect(wrapper.find('[data-test="digest-error-grace_minutes"]').text()).toBe(
      'A whole number is required.',
    )
  })

  it('puts the enabled switch on its own mutation, touching no grouped field', async () => {
    const wrapper = mountPanel()
    await wrapper.find('[data-test="digest-mail-title"]').setValue('Unsaved edit')

    const toggle = wrapper.findAllComponents({ name: 'ToggleSwitch' })[0]
    toggle.vm.$emit('update:modelValue', false)
    await flushPromises()

    expect(digest.setEnabled).toHaveBeenCalledWith(false)
    expect(digest.saveConfig).not.toHaveBeenCalled()
    // the unsaved edit is still on screen, and still unsaved
    expect(wrapper.find('[data-test="digest-save-bar"]').exists()).toBe(true)
  })

  it('gives a recipient add, toggle and removal their own mutations', async () => {
    const wrapper = mountPanel()
    await wrapper.find('[data-test="digest-recipient-input"]').setValue('three@example.test')
    await wrapper.find('[data-test="digest-recipient-add"]').trigger('click')
    await flushPromises()
    expect(digest.addRecipient).toHaveBeenCalledWith({
      email: 'three@example.test',
      language: 'de',
      display_name: null,
    })

    await wrapper.findAll('[data-test="digest-recipient-remove"]')[0].trigger('click')
    await flushPromises()
    expect(digest.removeRecipient).toHaveBeenCalledWith(5)

    const rowToggle = wrapper.findAllComponents({ name: 'ToggleSwitch' })[1]
    rowToggle.vm.$emit('update:modelValue', false)
    await flushPromises()
    expect(digest.setRecipientEnabled).toHaveBeenCalledWith(5, false)

    expect(digest.saveConfig).not.toHaveBeenCalled()
    expect(digest.setEnabled).not.toHaveBeenCalled()
  })

  it("names Send now as today's recovery when today's run failed", () => {
    vi.setSystemTime(new Date('2026-08-30T08:00:00Z'))
    digest.runs = ref([aRun({ status: 'failed', error: 'smtp refused', local_date: '2026-08-30' })])
    const wrapper = mountPanel()
    expect(wrapper.find('[data-test="digest-recovery"]').text()).toMatch(/Send now/)
    vi.useRealTimers()
  })

  it('does not call for a recovery when the failure was yesterday', () => {
    vi.setSystemTime(new Date('2026-08-30T08:00:00Z'))
    digest.runs = ref([aRun({ status: 'failed', local_date: '2026-08-29' })])
    const wrapper = mountPanel()
    expect(wrapper.find('[data-test="digest-recovery"]').exists()).toBe(false)
    vi.useRealTimers()
  })

  it('reads the next send off the runs list, not the clock alone', async () => {
    vi.setSystemTime(new Date('2026-08-30T04:00:00Z')) // 06:00 in Zurich, before the 07:00 send
    digest.runs = ref([aRun({ local_date: '2026-08-29' })])
    expect(mountPanel().find('[data-test="digest-next-send"]').text()).toMatch(/today at 07:00/)

    digest.runs = ref([aRun({ local_date: '2026-08-30' })]) // today already ran
    expect(mountPanel().find('[data-test="digest-next-send"]').text()).toMatch(/tomorrow at 07:00/)
    vi.useRealTimers()
  })

  it('gives the unsaved-changes bar a name of its own', async () => {
    const wrapper = mountPanel()
    await wrapper.find('[data-test="digest-mail-title"]').setValue('Something else')
    const bar = wrapper.find('[data-test="digest-save-bar"]')
    expect(bar.attributes('role')).toBe('group')
    expect(bar.attributes('aria-label')).toBeTruthy()
  })

  it('announces every refusal instead of only colouring it red — async and field alike', async () => {
    digest.enabledError = ref('Another settings change is still saving')
    const wrapper = mountPanel()
    expect(wrapper.find('[data-test="digest-enabled-error"]').attributes('role')).toBe('alert')

    // a validation message the admin never sent anywhere must announce itself the same way
    await wrapper.find('[data-test="digest-max-memos"]').setValue('0')
    const fieldError = wrapper.find('[data-test="digest-error-max_memos_per_run"]')
    expect(fieldError.text()).toBe('Between 1 and 1000.')
    expect(fieldError.attributes('role')).toBe('alert')

    await wrapper.find('[data-test="digest-from-email"]').setValue('not-an-address')
    expect(wrapper.find('[data-test="digest-error-from_email"]').attributes('role')).toBe('alert')
  })

  it('hands focus back to the Preview button when the drawer closes', async () => {
    const wrapper = mountPanel({ attach: true })
    const trigger = wrapper.find('[data-test="digest-preview-open"]').element as HTMLElement

    await wrapper.find('[data-test="digest-preview-open"]').trigger('click')
    await flushPromises()
    ;(document.activeElement as HTMLElement | null)?.blur() // the drawer took focus
    expect(document.activeElement).not.toBe(trigger)

    wrapper.findComponent(DrawerStub).vm.$emit('update:visible', false) // Escape or Close
    await flushPromises()

    expect(wrapper.find('[data-test="drawer-stub"]').attributes('data-visible')).toBe('false')
    expect(document.activeElement).toBe(trigger)
    wrapper.unmount()
  })

  it('opens the preview drawer and asks for a render', async () => {
    const wrapper = mountPanel()
    expect(wrapper.find('[data-test="drawer-stub"]').attributes('data-visible')).toBe('false')

    await wrapper.find('[data-test="digest-preview-open"]').trigger('click')
    await flushPromises()

    expect(actions.loadPreview).toHaveBeenCalledTimes(1)
    expect(wrapper.find('[data-test="drawer-stub"]').attributes('data-visible')).toBe('true')
  })

  it('sends the test mail in the language the selector names', async () => {
    const wrapper = mountPanel()
    const selector = wrapper
      .findAllComponents(SelectButton)
      .find((c) => c.attributes('data-test') === 'digest-test-language')
    selector?.vm.$emit('update:modelValue', 'es')
    await flushPromises()
    await wrapper.find('[data-test="digest-test-send"]').trigger('click')
    await flushPromises()
    expect(actions.sendTest).toHaveBeenCalledWith(false, 'es')
  })

  it('sends a test to the admin and claims a run', async () => {
    const wrapper = mountPanel()
    await wrapper.find('[data-test="digest-test-send"]').trigger('click')
    await flushPromises()
    expect(actions.sendTest).toHaveBeenCalledWith(false, 'de')

    await wrapper.find('[data-test="digest-run-now"]').trigger('click')
    await flushPromises()
    expect(actions.runNow).toHaveBeenCalledTimes(1)
  })

  it('refreshes the Runs table whenever the claimed run changes', async () => {
    // `POST /run` only claims the row; the worker finishes it. So the panel does not refresh once
    // after the click — it hands the composable a callback that fires on the claim AND on the
    // terminal status.
    mountPanel()
    expect(digest.refreshRuns).not.toHaveBeenCalled()
    await actions.options?.onRunChanged?.()
    expect(digest.refreshRuns).toHaveBeenCalledTimes(1)
  })
})
