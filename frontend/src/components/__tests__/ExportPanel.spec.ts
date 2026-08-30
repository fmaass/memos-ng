import { flushPromises, mount } from '@vue/test-utils'
import PrimeVue from 'primevue/config'
import SelectButton from 'primevue/selectbutton'
import ToggleSwitch from 'primevue/toggleswitch'
import { ref } from 'vue'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import ExportPanel from '@/components/ExportPanel.vue'
import type { ExportSettings } from '@/lib/exportSettings'

const SERVER: ExportSettings = {
  print_long_edge: 4096,
  print_jpeg_quality: 92,
  print_metadata_policy: 'strip',
  print_grayscale_single_channel: true,
  mail_long_edge: 1600,
  mail_jpeg_quality: 80,
  mail_subsampling: '4:2:0',
  mail_progressive: false,
  updated_at: '2026-08-30T10:00:00Z',
  updated_by: 'fabian',
}

const store = vi.hoisted(() => ({
  settings: undefined as unknown,
  printError: undefined as unknown,
  mailError: undefined as unknown,
  notice: undefined as unknown,
  writerBusy: undefined as unknown,
  savePrint: vi.fn(),
  saveMail: vi.fn(),
  clearNotice: vi.fn(),
}))

vi.mock('@/composables/useExportSettings', () => ({
  EXPORT_SETTINGS_PATH: '/api/admin/export/settings',
  useExportSettings: () => ({
    settings: store.settings,
    isPending: ref(false),
    settingsError: ref(null),
    savingPrint: ref(false),
    savingMail: ref(false),
    writerBusy: store.writerBusy,
    printError: store.printError,
    mailError: store.mailError,
    notice: store.notice,
    clearNotice: store.clearNotice,
    savePrint: store.savePrint,
    saveMail: store.saveMail,
  }),
}))

function mountPanel() {
  return mount(ExportPanel, { global: { plugins: [PrimeVue] } })
}

const selectBy = (wrapper: ReturnType<typeof mountPanel>, test: string) =>
  wrapper.findAllComponents(SelectButton).find((c) => c.attributes('data-test') === test)!
const toggleBy = (wrapper: ReturnType<typeof mountPanel>, test: string) =>
  wrapper.findAllComponents(ToggleSwitch).find((c) => c.attributes('data-test') === test)!

describe('ExportPanel', () => {
  beforeEach(() => {
    store.settings = ref({ ...SERVER })
    store.printError = ref(null)
    store.mailError = ref(null)
    store.notice = ref(null)
    store.writerBusy = ref(false)
    store.savePrint.mockResolvedValue(true)
    store.saveMail.mockResolvedValue(true)
  })
  afterEach(() => vi.clearAllMocks())

  it('says it is loading until the row arrives', () => {
    store.settings = ref(undefined)
    const wrapper = mountPanel()
    expect(wrapper.find('[data-test="export-loading"]').exists()).toBe(true)
    expect(wrapper.find('[data-test="export-print"]').exists()).toBe(false)
  })

  it('renders both profile groups, seeded from the server row', () => {
    const wrapper = mountPanel()
    expect(wrapper.find('[data-test="export-print"]').exists()).toBe(true)
    expect(wrapper.find('[data-test="export-mail"]').exists()).toBe(true)
    expect(selectBy(wrapper, 'export-print-long-edge').props('modelValue')).toBe(4096)
    expect(selectBy(wrapper, 'export-mail-long-edge').props('modelValue')).toBe(1600)
    expect(toggleBy(wrapper, 'export-print-grayscale').props('modelValue')).toBe(true)
    expect(toggleBy(wrapper, 'export-mail-progressive').props('modelValue')).toBe(false)
  })

  it('shows no save bar at all until something is edited', () => {
    const wrapper = mountPanel()
    expect(wrapper.find('[data-test="export-print-save-bar"]').exists()).toBe(false)
    expect(wrapper.find('[data-test="export-mail-save-bar"]').exists()).toBe(false)
  })

  it('raises only the edited profile’s bar, and saves only that profile', async () => {
    const wrapper = mountPanel()
    selectBy(wrapper, 'export-print-long-edge').vm.$emit('update:modelValue', 5000)
    await flushPromises()

    expect(wrapper.find('[data-test="export-print-save-bar"]').exists()).toBe(true)
    expect(wrapper.find('[data-test="export-mail-save-bar"]').exists()).toBe(false)

    await wrapper.find('[data-test="export-print-save"]').trigger('click')
    await flushPromises()
    expect(store.savePrint).toHaveBeenCalledWith({
      print_long_edge: 5000,
      print_jpeg_quality: 92,
      print_metadata_policy: 'strip',
      print_grayscale_single_channel: true,
    })
    expect(store.saveMail).not.toHaveBeenCalled()
  })

  it('gives each bar the group role and a name that says which one it is', async () => {
    const wrapper = mountPanel()
    toggleBy(wrapper, 'export-mail-progressive').vm.$emit('update:modelValue', true)
    await flushPromises()
    const bar = wrapper.find('[data-test="export-mail-save-bar"]')
    expect(bar.attributes('role')).toBe('group')
    expect(bar.attributes('aria-label')).toBe('Unsaved mail profile')
  })

  it('discards back to the server row and puts the bar away', async () => {
    const wrapper = mountPanel()
    selectBy(wrapper, 'export-mail-subsampling').vm.$emit('update:modelValue', '4:4:4')
    await flushPromises()
    expect(wrapper.find('[data-test="export-mail-save-bar"]').exists()).toBe(true)

    await wrapper.find('[data-test="export-mail-discard"]').trigger('click')
    await flushPromises()
    expect(selectBy(wrapper, 'export-mail-subsampling').props('modelValue')).toBe('4:2:0')
    expect(wrapper.find('[data-test="export-mail-save-bar"]').exists()).toBe(false)
    expect(store.saveMail).not.toHaveBeenCalled()
  })

  it('re-seeds from the server row after a save, so the bar goes away', async () => {
    const wrapper = mountPanel()
    selectBy(wrapper, 'export-print-metadata').vm.$emit('update:modelValue', 'keep_dates')
    await flushPromises()
    await wrapper.find('[data-test="export-print-save"]').trigger('click')
    await flushPromises()
    expect(wrapper.find('[data-test="export-print-save-bar"]').exists()).toBe(false)
  })

  it('keeps the bar up and sends nothing when the save is refused', async () => {
    store.savePrint.mockResolvedValue(false)
    store.printError = ref('Not Found')
    const wrapper = mountPanel()
    selectBy(wrapper, 'export-print-long-edge').vm.$emit('update:modelValue', 0)
    await flushPromises()
    await wrapper.find('[data-test="export-print-save"]').trigger('click')
    await flushPromises()

    expect(wrapper.find('[data-test="export-print-save-bar"]').exists()).toBe(true)
    const error = wrapper.find('[data-test="export-print-error"]')
    expect(error.attributes('role')).toBe('alert')
    expect(error.text()).toBe('Not Found')
  })

  it('refuses to save an invalid draft, and says why on an alert', async () => {
    const wrapper = mountPanel()
    await wrapper.find('[data-test="export-print-quality"]').setValue('120')
    await flushPromises()

    const error = wrapper.find('[data-test="export-error-print_jpeg_quality"]')
    expect(error.attributes('role')).toBe('alert')
    expect(error.text()).toBe('Between 90 and 95.')

    await wrapper.find('[data-test="export-print-save"]').trigger('click')
    await flushPromises()
    expect(store.savePrint).not.toHaveBeenCalled()
  })

  it('refuses an emptied quality field rather than saving a zero', async () => {
    const wrapper = mountPanel()
    await wrapper.find('[data-test="export-mail-quality"]').setValue('')
    await flushPromises()
    expect(wrapper.find('[data-test="export-error-mail_jpeg_quality"]').text()).toBe(
      'A whole number is required.',
    )
    await wrapper.find('[data-test="export-mail-save"]').trigger('click')
    await flushPromises()
    expect(store.saveMail).not.toHaveBeenCalled()
  })

  it('will not start a second save while the store already has a writer', async () => {
    store.writerBusy = ref(true)
    const wrapper = mountPanel()
    selectBy(wrapper, 'export-mail-long-edge').vm.$emit('update:modelValue', 2048)
    await flushPromises()
    await wrapper.find('[data-test="export-mail-save"]').trigger('click')
    await flushPromises()
    expect(store.saveMail).not.toHaveBeenCalled()
  })

  it('announces a confirmation politely', () => {
    store.notice = ref('The print profile is saved.')
    const notice = mountPanel().find('[data-test="export-notice"]')
    expect(notice.attributes('role')).toBe('status')
    expect(notice.attributes('aria-live')).toBe('polite')
    expect(notice.text()).toBe('The print profile is saved.')
  })

  it('shows the export command for the chosen range, with no token in it', async () => {
    const wrapper = mountPanel()
    await wrapper.find('[data-test="export-query-from"]').setValue('2026-05-01')
    await wrapper.find('[data-test="export-query-to"]').setValue('2026-05-31')
    await flushPromises()

    const command = wrapper.find('[data-test="export-curl"]').text()
    expect(command).toContain('/api/exports/photobook?from=2026-05-01&to=2026-05-31&format=zip')
    expect(command).toContain('Authorization: Bearer $MEMOS_PAT')
    expect(command).not.toMatch(/Bearer\s+[A-Za-z0-9._-]{8,}/)
  })

  it('names the print profile the command would export with', () => {
    const line = mountPanel().find('[data-test="export-profile-line"]').text()
    expect(line).toBe(
      '4096 px on the long edge, JPEG quality 92, metadata stripped, neutral monochrome as single-channel',
    )
  })

  it('flags a backwards range on an alert', async () => {
    const wrapper = mountPanel()
    await wrapper.find('[data-test="export-query-from"]').setValue('2026-05-31')
    await wrapper.find('[data-test="export-query-to"]').setValue('2026-05-01')
    await flushPromises()
    const error = wrapper.find('[data-test="export-error-to"]')
    expect(error.attributes('role')).toBe('alert')
    expect(error.text()).toMatch(/before/)
  })
})
