import { describe, expect, it } from 'vitest'

import {
  ADMIN_TABS,
  type DigestConfigDraft,
  type DigestRun,
  type DigestSettings,
  DIGEST_LANGUAGES,
  checkNewRecipient,
  checkRecipientLanguage,
  describeNextSend,
  dirtyConfigFields,
  draftFromSettings,
  formatMib,
  isCalendarDate,
  isClockTime,
  lastRunLabel,
  localDateIn,
  nextSendLabel,
  normaliseEmail,
  normaliseSendTime,
  putBodyFromDraft,
  ranOn,
  recipientChangeGuard,
  summariseLastRun,
  tabFromQuery,
  validateConfigDraft,
} from '@/lib/digest'

function settings(over: Partial<DigestSettings> = {}): DigestSettings {
  return {
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
    ...over,
  }
}

function draft(over: Partial<DigestConfigDraft> = {}): DigestConfigDraft {
  return { ...draftFromSettings(settings()), ...over }
}

function run(over: Partial<DigestRun> = {}): DigestRun {
  return {
    id: 1,
    groups_attempted: [],
    groups_accepted: [],
    trigger: 'scheduled',
    started_at: '2026-08-30T05:00:00Z',
    finished_at: '2026-08-30T05:01:00Z',
    status: 'sent',
    memos_sent: 7,
    parts: 2,
    parts_sent: 2,
    parts_claimed: 2,
    recipients: 6,
    error: null,
    local_date: '2026-08-30',
    ...over,
  }
}

describe('tabFromQuery', () => {
  it('defaults to general when the query says nothing', () => {
    expect(tabFromQuery(undefined, true)).toBe('general')
    expect(tabFromQuery(null, true)).toBe('general')
    expect(tabFromQuery('', true)).toBe('general')
  })

  it('gives an admin the mail tab, from a bare value or a repeated one', () => {
    expect(tabFromQuery('mail', true)).toBe('mail')
    expect(tabFromQuery(['mail', 'general'], true)).toBe('mail')
  })

  it('falls back to general for an unknown value', () => {
    expect(tabFromQuery('Mail', true)).toBe('general')
    expect(tabFromQuery('billing', true)).toBe('general')
    expect(tabFromQuery(7, true)).toBe('general')
  })

  it('lands a non-admin asking for mail on general', () => {
    expect(tabFromQuery('mail', false)).toBe('general')
    expect(tabFromQuery(['mail'], false)).toBe('general')
    expect(tabFromQuery('general', false)).toBe('general')
  })

  it('gives an admin the export tab, from a bare value or a repeated one', () => {
    expect(tabFromQuery('export', true)).toBe('export')
    expect(tabFromQuery(['export', 'mail'], true)).toBe('export')
  })

  it('lands a non-admin asking for export on general', () => {
    expect(tabFromQuery('export', false)).toBe('general')
    expect(tabFromQuery(['export'], false)).toBe('general')
  })

  it('still falls back to general for a value close to an admin tab', () => {
    expect(tabFromQuery('Export', true)).toBe('general')
    expect(tabFromQuery('exports', true)).toBe('general')
    expect(tabFromQuery({ tab: 'export' }, true)).toBe('general')
  })

  it('names every admin-only tab, so a new one cannot be forgotten here', () => {
    expect([...ADMIN_TABS].sort()).toEqual(['export', 'mail'])
    for (const tab of ADMIN_TABS) {
      expect(tabFromQuery(tab, true)).toBe(tab)
      expect(tabFromQuery(tab, false)).toBe('general')
    }
  })
})

describe('normaliseEmail', () => {
  it('trims and lower-cases an address it will store', () => {
    expect(normaliseEmail('  Fabian@Example.TEST ')).toBe('fabian@example.test')
    expect(normaliseEmail("o'neil+tag@sub.example.test")).toBe("o'neil+tag@sub.example.test")
  })

  it('rejects every shape the API would not store', () => {
    expect(normaliseEmail('')).toBeNull()
    expect(normaliseEmail('   ')).toBeNull()
    expect(normaliseEmail('nobody')).toBeNull()
    expect(normaliseEmail('nobody@localhost')).toBeNull() // no dotted domain
    expect(normaliseEmail('.leading@example.test')).toBeNull()
    expect(normaliseEmail('two@@example.test')).toBeNull()
    expect(normaliseEmail('spaced address@example.test')).toBeNull()
    expect(normaliseEmail('short@example.t')).toBeNull() // tld < 2
    expect(normaliseEmail(`${'a'.repeat(320)}@example.test`)).toBeNull() // over 320
  })
})

describe('checkNewRecipient', () => {
  it('accepts a fresh address and returns its stored form', () => {
    expect(checkNewRecipient(' New@Example.test ', ['other@example.test'])).toEqual({
      ok: true,
      email: 'new@example.test',
    })
  })

  it('names each reason it refuses', () => {
    expect(checkNewRecipient('   ', [])).toEqual({ ok: false, reason: 'Enter an e-mail address.' })
    expect(checkNewRecipient('nope', [])).toEqual({
      ok: false,
      reason: 'That is not a valid e-mail address.',
    })
    expect(checkNewRecipient(`${'a'.repeat(330)}@example.test`, []).ok).toBe(false)
    expect(checkNewRecipient(`${'a'.repeat(330)}@example.test`, [])).toEqual({
      ok: false,
      reason: 'An address may be at most 320 characters.',
    })
  })

  it('treats a differently-cased duplicate as a duplicate', () => {
    expect(checkNewRecipient('A@Example.test', ['a@example.test'])).toEqual({
      ok: false,
      reason: 'That address is already a recipient.',
    })
  })
})

describe('recipientChangeGuard', () => {
  const armed = true
  it('allows a change to an already-disabled recipient', () => {
    const target = { id: 1, enabled: false }
    expect(recipientChangeGuard(target, [target], armed)).toEqual({ allowed: true })
  })

  it('allows anything while the digest is off', () => {
    const target = { id: 1, enabled: true }
    expect(recipientChangeGuard(target, [target], false)).toEqual({ allowed: true })
  })

  it('allows it while another enabled recipient remains', () => {
    const target = { id: 1, enabled: true }
    const rest = [target, { id: 2, enabled: true }]
    expect(recipientChangeGuard(target, rest, armed)).toEqual({ allowed: true })
  })

  it('refuses the last enabled recipient of an armed digest, with the reason', () => {
    const target = { id: 1, enabled: true }
    const rest = [target, { id: 2, enabled: false }]
    const verdict = recipientChangeGuard(target, rest, armed)
    expect(verdict.allowed).toBe(false)
    expect(verdict.allowed === false && verdict.reason).toMatch(/last enabled recipient/)
  })
})

describe('normaliseSendTime', () => {
  it('drops a zero seconds field so the API value and an input agree', () => {
    expect(normaliseSendTime('07:00:00')).toBe('07:00')
    expect(normaliseSendTime('07:00')).toBe('07:00')
  })
  it('keeps seconds that mean something', () => {
    expect(normaliseSendTime('07:00:30')).toBe('07:00:30')
  })
  it('leaves something it does not recognise alone', () => {
    expect(normaliseSendTime(' 7am ')).toBe('7am')
  })
})

describe('the grouped config', () => {
  it('is not dirty when it came straight off the server, seconds and all', () => {
    expect(dirtyConfigFields(draftFromSettings(settings()), settings())).toEqual([])
  })

  it('names exactly the fields that changed', () => {
    expect(dirtyConfigFields(draft({ mail_title: 'Other' }), settings())).toEqual(['mail_title'])
    expect(dirtyConfigFields(draft({ send_time: '08:00', retention_days: 30 }), settings())).toEqual(
      ['send_time', 'retention_days'],
    )
  })

  it('never reads the enabled switch as a grouped change', () => {
    expect(dirtyConfigFields(draftFromSettings(settings()), settings({ enabled: false }))).toEqual(
      [],
    )
  })

  it('builds a PUT body whose enabled comes from the server, not the draft', () => {
    const body = putBodyFromDraft(draft({ send_time: '08:30:00' }), false)
    expect(body.enabled).toBe(false)
    expect(body.send_time).toBe('08:30')
    expect(body.mail_title).toBe('Daily memos')
    expect(Object.keys(body).sort()).toEqual(
      [
        'dropbox_folder',
        'enabled',
        'from_email',
        'from_name',
        'grace_minutes',
        'hosting_enabled',
        'link_images_full',
        'mail_title',
        'max_email_bytes',
        'max_memos_per_run',
        'max_videos_per_memo',
        'retention_days',
        'send_time',
        'start_date',
        'timezone',
        'visibility_scope',
      ].sort(),
    )
  })
})

describe('isClockTime / isCalendarDate', () => {
  it('accepts real clock times and refuses impossible ones', () => {
    expect(isClockTime('00:00')).toBe(true)
    expect(isClockTime('23:59:59')).toBe(true)
    expect(isClockTime('24:00')).toBe(false)
    expect(isClockTime('07:60')).toBe(false)
    expect(isClockTime('7:00')).toBe(false)
    expect(isClockTime('')).toBe(false)
  })

  it('accepts real days and refuses a day that does not exist', () => {
    expect(isCalendarDate('2026-06-01')).toBe(true)
    expect(isCalendarDate('2024-02-29')).toBe(true) // leap year
    expect(isCalendarDate('2026-02-29')).toBe(false)
    expect(isCalendarDate('2026-02-30')).toBe(false)
    expect(isCalendarDate('2026-13-01')).toBe(false)
    expect(isCalendarDate('01.06.2026')).toBe(false)
  })
})

describe('validateConfigDraft', () => {
  it('passes a draft the API would accept', () => {
    expect(validateConfigDraft(draft())).toEqual({})
    expect(validateConfigDraft(draft({ from_email: 'digest@example.test' }))).toEqual({})
  })

  it('rejects a malformed time, zone and date', () => {
    expect(validateConfigDraft(draft({ send_time: '25:00' })).send_time).toBeDefined()
    expect(validateConfigDraft(draft({ timezone: '  ' })).timezone).toBe('A time zone is required.')
    expect(validateConfigDraft(draft({ timezone: 'z'.repeat(65) })).timezone).toBe(
      'At most 64 characters.',
    )
    expect(validateConfigDraft(draft({ timezone: 'Mars/Olympus' })).timezone).toMatch(/not a time/i)
    expect(validateConfigDraft(draft({ start_date: '2026-02-30' })).start_date).toBeDefined()
  })

  it('holds every numeric cap to its API bounds, below, above and non-integer', () => {
    expect(validateConfigDraft(draft({ grace_minutes: -1 })).grace_minutes).toBe(
      'Between 0 and 1440.',
    )
    expect(validateConfigDraft(draft({ grace_minutes: 1441 })).grace_minutes).toBeDefined()
    expect(validateConfigDraft(draft({ grace_minutes: 1.5 })).grace_minutes).toBe(
      'A whole number is required.',
    )
    expect(validateConfigDraft(draft({ max_email_bytes: 1048575 })).max_email_bytes).toBeDefined()
    expect(validateConfigDraft(draft({ max_email_bytes: 104857601 })).max_email_bytes).toBeDefined()
    expect(validateConfigDraft(draft({ max_videos_per_memo: -1 })).max_videos_per_memo).toBeDefined()
    expect(validateConfigDraft(draft({ max_videos_per_memo: 51 })).max_videos_per_memo).toBeDefined()
    expect(validateConfigDraft(draft({ max_memos_per_run: 0 })).max_memos_per_run).toBeDefined()
    expect(validateConfigDraft(draft({ max_memos_per_run: 1001 })).max_memos_per_run).toBeDefined()
    expect(validateConfigDraft(draft({ retention_days: 0 })).retention_days).toBeDefined()
    expect(validateConfigDraft(draft({ retention_days: 3651 })).retention_days).toBeDefined()
    // the edges themselves are legal
    expect(validateConfigDraft(draft({ grace_minutes: 0, max_email_bytes: 1048576 }))).toEqual({})
  })

  it('requires the bounded strings and refuses an over-long one', () => {
    expect(validateConfigDraft(draft({ mail_title: ' ' })).mail_title).toBe('This is required.')
    expect(validateConfigDraft(draft({ from_name: 'x'.repeat(256) })).from_name).toBe(
      'At most 255 characters.',
    )
    expect(validateConfigDraft(draft({ dropbox_folder: '' })).dropbox_folder).toBeDefined()
  })

  it('accepts an empty sender but not a malformed one', () => {
    expect(validateConfigDraft(draft({ from_email: '' })).from_email).toBeUndefined()
    expect(validateConfigDraft(draft({ from_email: 'not-an-address' })).from_email).toMatch(
      /e-mail address/,
    )
  })
})

describe('localDateIn', () => {
  it('reads the calendar day in the configured zone', () => {
    // 22:30 UTC on the 29th is already the 30th in Zurich (UTC+2 in August).
    expect(localDateIn('Europe/Zurich', new Date('2026-08-29T22:30:00Z'))).toBe('2026-08-30')
    expect(localDateIn('UTC', new Date('2026-08-29T22:30:00Z'))).toBe('2026-08-29')
  })
  it('answers null for a zone it cannot resolve', () => {
    expect(localDateIn('Mars/Olympus', new Date('2026-08-29T22:30:00Z'))).toBeNull()
  })
})

describe('describeNextSend', () => {
  it('says nothing is scheduled while the digest is off', () => {
    expect(describeNextSend(settings({ enabled: false }), new Date('2026-08-30T04:00:00Z'))).toEqual(
      { kind: 'off' },
    )
  })

  it('says today while the send time is still ahead in that zone', () => {
    // 04:00 UTC = 06:00 Zurich, before 07:00.
    expect(describeNextSend(settings(), new Date('2026-08-30T04:00:00Z'))).toEqual({
      kind: 'scheduled',
      when: 'today',
      time: '07:00',
      timezone: 'Europe/Zurich',
    })
  })

  it('says tomorrow once it has passed', () => {
    // 08:00 UTC = 10:00 Zurich, after 07:00.
    expect(describeNextSend(settings(), new Date('2026-08-30T08:00:00Z'))).toMatchObject({
      kind: 'scheduled',
      when: 'tomorrow',
    })
  })

  it('says tomorrow when a scheduled run already took today, however early it is', () => {
    // 04:00 UTC = 06:00 Zurich, still before 07:00 — but the day is already spent.
    const early = new Date('2026-08-30T04:00:00Z')
    expect(describeNextSend(settings(), early, [run({ local_date: '2026-08-30' })])).toMatchObject({
      when: 'tomorrow',
    })
    expect(describeNextSend(settings(), early, [])).toMatchObject({ when: 'today' })
  })

  it('counts a manual run and a FAILED run as having taken the day, but not a test or an import', () => {
    const early = new Date('2026-08-30T04:00:00Z')
    const taken = (over: Partial<DigestRun>) =>
      describeNextSend(settings(), early, [run({ local_date: '2026-08-30', ...over })])
    expect(taken({ trigger: 'manual' })).toMatchObject({ when: 'tomorrow' })
    expect(taken({ status: 'failed' })).toMatchObject({ when: 'tomorrow' })
    expect(taken({ status: null })).toMatchObject({ when: 'tomorrow' })
    expect(taken({ trigger: 'test' })).toMatchObject({ when: 'today' })
    expect(taken({ trigger: 'import' })).toMatchObject({ when: 'today' })
    // yesterday's run says nothing about today
    expect(
      describeNextSend(settings(), early, [run({ local_date: '2026-08-29' })]),
    ).toMatchObject({ when: 'today' })
  })

  it('ranOn answers for one day at a time', () => {
    const runs = [run({ local_date: '2026-08-30' }), run({ local_date: '2026-08-29', trigger: 'test' })]
    expect(ranOn(runs, '2026-08-30')).toBe(true)
    expect(ranOn(runs, '2026-08-29')).toBe(false) // a test send is not a delivery
    expect(ranOn(runs, '2026-08-28')).toBe(false)
    expect(ranOn(undefined, '2026-08-30')).toBe(false)
  })

  it('names the start date while it is still in the future', () => {
    expect(
      describeNextSend(settings({ start_date: '2026-09-15' }), new Date('2026-08-30T08:00:00Z')),
    ).toEqual({ kind: 'first', date: '2026-09-15', time: '07:00', timezone: 'Europe/Zurich' })
  })

  it('degrades honestly when the zone cannot be resolved', () => {
    expect(
      describeNextSend(settings({ timezone: 'Mars/Olympus' }), new Date('2026-08-30T08:00:00Z')),
    ).toEqual({ kind: 'unknown-timezone', time: '07:00', timezone: 'Mars/Olympus' })
  })

  it('labels every case', () => {
    expect(nextSendLabel({ kind: 'off' })).toMatch(/off/)
    expect(
      nextSendLabel({ kind: 'scheduled', when: 'today', time: '07:00', timezone: 'Europe/Zurich' }),
    ).toBe('Next send today at 07:00 (Europe/Zurich).')
    expect(nextSendLabel({ kind: 'first', date: '2026-09-15', time: '07:00', timezone: 'UTC' })).toBe(
      'First send on 2026-09-15 at 07:00 (UTC).',
    )
    expect(nextSendLabel({ kind: 'unknown-timezone', time: '07:00', timezone: 'Mars/Olympus' })).toMatch(
      /cannot resolve/,
    )
  })
})

describe('summariseLastRun', () => {
  const now = new Date('2026-08-30T08:00:00Z') // 2026-08-30 in Zurich

  it('reports no run at all', () => {
    expect(summariseLastRun([], 'Europe/Zurich', now)).toEqual({ kind: 'none' })
    expect(summariseLastRun(undefined, 'Europe/Zurich', now)).toEqual({ kind: 'none' })
  })

  it('classifies a run in flight, a sent one, a failed one and an unknown status', () => {
    expect(summariseLastRun([run({ status: null })], 'Europe/Zurich', now)).toMatchObject({
      status: 'running',
      severity: 'info',
    })
    expect(summariseLastRun([run()], 'Europe/Zurich', now)).toMatchObject({
      status: 'sent',
      severity: 'success',
    })
    expect(summariseLastRun([run({ status: 'failed' })], 'Europe/Zurich', now)).toMatchObject({
      status: 'failed',
      severity: 'danger',
    })
    expect(summariseLastRun([run({ status: 'skipped' })], 'Europe/Zurich', now)).toMatchObject({
      status: 'other',
      severity: 'secondary',
    })
  })

  it("calls for a same-day recovery only when TODAY's run failed", () => {
    expect(
      summariseLastRun([run({ status: 'failed', local_date: '2026-08-30' })], 'Europe/Zurich', now),
    ).toMatchObject({ needsSameDayRecovery: true })
    expect(
      summariseLastRun([run({ status: 'failed', local_date: '2026-08-29' })], 'Europe/Zurich', now),
    ).toMatchObject({ needsSameDayRecovery: false })
    // yesterday failed, today already went out — nothing to recover
    expect(
      summariseLastRun(
        [run({ id: 2 }), run({ id: 1, status: 'failed', local_date: '2026-08-29' })],
        'Europe/Zurich',
        now,
      ),
    ).toMatchObject({ needsSameDayRecovery: false })
  })

  it('labels the chip', () => {
    expect(lastRunLabel({ kind: 'none' })).toBe('No run yet.')
    expect(lastRunLabel(summariseLastRun([run()], 'Europe/Zurich', now))).toBe(
      '2026-08-30 · sent · 7 memos · 2/2 parts',
    )
    expect(
      lastRunLabel(
        summariseLastRun([run({ memos_sent: 1, parts: 1, parts_sent: 1 })], 'Europe/Zurich', now),
      ),
    ).toBe('2026-08-30 · sent · 1 memo · 1/1 part')
    expect(lastRunLabel(summariseLastRun([run({ status: null })], 'Europe/Zurich', now))).toMatch(
      /^Running since /,
    )
  })
})

describe('formatMib', () => {
  it('reads a byte cap back as MiB', () => {
    expect(formatMib(20971520)).toBe('20.0 MiB')
    expect(formatMib(1048576)).toBe('1.0 MiB')
  })
})

describe('checkRecipientLanguage', () => {
  it.each(['de', 'es', 'en'])('accepts %s', (language) => {
    expect(checkRecipientLanguage(language)).toEqual({ ok: true, language })
  })

  it.each(['fr', 'DE', ' de', 'de ', '', 'deu', 'english', null, undefined, 1, ['de']])(
    'refuses %j',
    (value) => {
      expect(checkRecipientLanguage(value).ok).toBe(false)
    },
  )

  it('names the three languages the API knows, in send order', () => {
    expect(DIGEST_LANGUAGES).toEqual(['de', 'es', 'en'])
  })
})
