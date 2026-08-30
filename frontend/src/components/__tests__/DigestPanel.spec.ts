import { flushPromises, mount } from '@vue/test-utils'
import PrimeVue from 'primevue/config'
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
    added_by: 'fabian',
    added_at: '2026-08-30T10:00:00Z',
  },
  {
    id: 6,
    email: 'two@example.test',
    display_name: 'Two',
    enabled: true,
    added_by: 'fabian',
    added_at: '2026-08-30T10:00:00Z',
  },
]

function aRun(over: Partial<DigestRun> = {}): DigestRun {
  return {
    id: 1,
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
    enabledError: ref(null),
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
  useDigestActions: () => ({
    preview: actions.preview,
    previewing: ref(false),
    previewError: ref(null),
    sendingTest: ref(false),
    runningNow: ref(false),
    actionError: ref(null),
    actionNotice: ref(null),
    clearActionMessages: vi.fn(),
    loadPreview: actions.loadPreview,
    sendTest: actions.sendTest,
    runNow: actions.runNow,
  }),
}))

const DrawerStub = {
  name: 'DigestPreviewDrawer',
  props: ['visible', 'preview', 'loading', 'error'],
  template: '<div data-test="drawer-stub" :data-visible="String(visible)" />',
}

function mountPanel() {
  return mount(DigestPanel, {
    global: { plugins: [PrimeVue], stubs: { DigestPreviewDrawer: DrawerStub } },
  })
}

describe('DigestPanel', () => {
  beforeEach(() => {
    digest.settings = ref({ ...SERVER })
    digest.runs = ref([aRun()])
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

  it('opens the preview drawer and asks for a render', async () => {
    const wrapper = mountPanel()
    expect(wrapper.find('[data-test="drawer-stub"]').attributes('data-visible')).toBe('false')

    await wrapper.find('[data-test="digest-preview-open"]').trigger('click')
    await flushPromises()

    expect(actions.loadPreview).toHaveBeenCalledTimes(1)
    expect(wrapper.find('[data-test="drawer-stub"]').attributes('data-visible')).toBe('true')
  })

  it('sends a test to the admin and starts a run, refreshing the runs table afterwards', async () => {
    const wrapper = mountPanel()
    await wrapper.find('[data-test="digest-test-send"]').trigger('click')
    await flushPromises()
    expect(actions.sendTest).toHaveBeenCalledWith(false)

    await wrapper.find('[data-test="digest-run-now"]').trigger('click')
    await flushPromises()
    expect(actions.runNow).toHaveBeenCalledTimes(1)
    expect(digest.refreshRuns).toHaveBeenCalledTimes(1)
  })

  it('leaves the runs table alone when the run was refused', async () => {
    actions.runNow.mockResolvedValue(null)
    const wrapper = mountPanel()
    await wrapper.find('[data-test="digest-run-now"]').trigger('click')
    await flushPromises()
    expect(digest.refreshRuns).not.toHaveBeenCalled()
  })
})
