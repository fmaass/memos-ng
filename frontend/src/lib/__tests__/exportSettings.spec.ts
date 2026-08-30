import { describe, expect, it } from 'vitest'

import {
  type ExportSettings,
  type MailProfileDraft,
  type PrintProfileDraft,
  defaultExportQuery,
  describePrintProfile,
  dirtyMailFields,
  dirtyPrintFields,
  exportCurlCommand,
  mailDraftFromSettings,
  printDraftFromSettings,
  putBodyFromMailDraft,
  putBodyFromPrintDraft,
  validateExportQuery,
  validateMailDraft,
  validatePrintDraft,
} from '@/lib/exportSettings'

function settings(over: Partial<ExportSettings> = {}): ExportSettings {
  return {
    print_long_edge: 4096,
    print_jpeg_quality: 92,
    print_metadata_policy: 'strip',
    print_grayscale_single_channel: true,
    mail_long_edge: 1600,
    mail_jpeg_quality: 80,
    mail_subsampling: '4:2:0',
    mail_progressive: false,
    print_profile_key: 'p4096-q92-strip-mono',
    updated_at: '2026-08-30T10:00:00Z',
    updated_by: 'fabian',
    ...over,
  }
}

const printDraft = (over: Partial<PrintProfileDraft> = {}): PrintProfileDraft => ({
  ...printDraftFromSettings(settings()),
  ...over,
})
const mailDraft = (over: Partial<MailProfileDraft> = {}): MailProfileDraft => ({
  ...mailDraftFromSettings(settings()),
  ...over,
})

describe('drafts', () => {
  it('reads each profile out of the row, and nothing of the other one', () => {
    expect(printDraftFromSettings(settings())).toEqual({
      print_long_edge: 4096,
      print_jpeg_quality: 92,
      print_metadata_policy: 'strip',
      print_grayscale_single_channel: true,
    })
    expect(mailDraftFromSettings(settings())).toEqual({
      mail_long_edge: 1600,
      mail_jpeg_quality: 80,
      mail_subsampling: '4:2:0',
      mail_progressive: false,
    })
  })

  it('is clean against the row it came from', () => {
    expect(dirtyPrintFields(printDraft(), settings())).toEqual([])
    expect(dirtyMailFields(mailDraft(), settings())).toEqual([])
  })

  it('names exactly the fields that moved, per profile', () => {
    expect(dirtyPrintFields(printDraft({ print_long_edge: 5000 }), settings())).toEqual([
      'print_long_edge',
    ])
    expect(
      dirtyPrintFields(
        printDraft({ print_jpeg_quality: 95, print_grayscale_single_channel: false }),
        settings(),
      ),
    ).toEqual(['print_jpeg_quality', 'print_grayscale_single_channel'])
    expect(dirtyMailFields(mailDraft({ mail_subsampling: '4:4:4' }), settings())).toEqual([
      'mail_subsampling',
    ])
  })

  it('does not report the OTHER profile moving as this one being dirty', () => {
    const server = settings({ mail_jpeg_quality: 83 })
    expect(dirtyPrintFields(printDraft(), server)).toEqual([])
    expect(dirtyMailFields(mailDraft(), server)).toEqual(['mail_jpeg_quality'])
  })
})

/** The body, or a loud failure — a test that quietly accepted a refusal here would prove nothing. */
function unwrap(result: ReturnType<typeof putBodyFromPrintDraft>) {
  if (!result.ok) throw new Error(`expected a body, got a refusal: ${result.message}`)
  return result.body
}

describe('the PUT body', () => {
  it('is the whole row: a print save takes the mail fields from the server', () => {
    const server = settings({ mail_long_edge: 2048, mail_progressive: true })
    expect(unwrap(putBodyFromPrintDraft(printDraft({ print_long_edge: 5000 }), server))).toEqual({
      print_long_edge: 5000,
      print_jpeg_quality: 92,
      print_metadata_policy: 'strip',
      print_grayscale_single_channel: true,
      mail_long_edge: 2048,
      mail_jpeg_quality: 80,
      mail_subsampling: '4:2:0',
      mail_progressive: true,
    })
  })

  it('and a mail save takes the print fields from the server', () => {
    const server = settings({ print_long_edge: 0, print_jpeg_quality: 95 })
    expect(unwrap(putBodyFromMailDraft(mailDraft({ mail_jpeg_quality: 77 }), server))).toEqual({
      print_long_edge: 0,
      print_jpeg_quality: 95,
      print_metadata_policy: 'strip',
      print_grayscale_single_channel: true,
      mail_long_edge: 1600,
      mail_jpeg_quality: 77,
      mail_subsampling: '4:2:0',
      mail_progressive: false,
    })
  })

  it('carries no unsaved edit of the other profile along', () => {
    // The mail draft on screen says 2048; the server row still says 1600. A print save must send
    // the server's value, or saving one bar would silently commit the other bar's draft.
    const server = settings()
    const body = unwrap(putBodyFromPrintDraft(printDraft({ print_jpeg_quality: 90 }), server))
    expect(body.mail_long_edge).toBe(1600)
  })
})

describe('validatePrintDraft', () => {
  it('accepts every long edge the page offers', () => {
    for (const edge of [4096, 5000, 0] as const)
      expect(validatePrintDraft(printDraft({ print_long_edge: edge }))).toEqual({})
  })

  it('refuses a long edge the derivation cannot make', () => {
    expect(
      validatePrintDraft(printDraft({ print_long_edge: 3000 as unknown as 4096 })).print_long_edge,
    ).toMatch(/4096/)
  })

  it('accepts both ends of the quality band and refuses either side of it', () => {
    expect(validatePrintDraft(printDraft({ print_jpeg_quality: 90 }))).toEqual({})
    expect(validatePrintDraft(printDraft({ print_jpeg_quality: 95 }))).toEqual({})
    expect(validatePrintDraft(printDraft({ print_jpeg_quality: 89 })).print_jpeg_quality).toBe(
      'Between 90 and 95.',
    )
    expect(validatePrintDraft(printDraft({ print_jpeg_quality: 96 })).print_jpeg_quality).toBe(
      'Between 90 and 95.',
    )
  })

  it('refuses a quality that is not a whole number, an emptied field included', () => {
    expect(validatePrintDraft(printDraft({ print_jpeg_quality: NaN })).print_jpeg_quality).toBe(
      'A whole number is required.',
    )
    expect(validatePrintDraft(printDraft({ print_jpeg_quality: 92.5 })).print_jpeg_quality).toBe(
      'A whole number is required.',
    )
  })

  it('accepts both metadata policies and refuses a third', () => {
    for (const policy of ['strip', 'keep_dates'] as const)
      expect(validatePrintDraft(printDraft({ print_metadata_policy: policy }))).toEqual({})
    expect(
      validatePrintDraft(printDraft({ print_metadata_policy: 'keep_all' as unknown as 'strip' }))
        .print_metadata_policy,
    ).toBeTruthy()
  })

  it('accepts the monochrome toggle either way and refuses a non-boolean', () => {
    for (const on of [true, false])
      expect(validatePrintDraft(printDraft({ print_grayscale_single_channel: on }))).toEqual({})
    expect(
      validatePrintDraft(
        printDraft({ print_grayscale_single_channel: 'yes' as unknown as boolean }),
      ).print_grayscale_single_channel,
    ).toBeTruthy()
  })

  it('reports every broken field at once, not just the first', () => {
    const errors = validatePrintDraft(
      printDraft({
        print_long_edge: 1 as unknown as 4096,
        print_jpeg_quality: 10,
        print_metadata_policy: 'nope' as unknown as 'strip',
      }),
    )
    expect(Object.keys(errors).sort()).toEqual([
      'print_jpeg_quality',
      'print_long_edge',
      'print_metadata_policy',
    ])
  })
})

describe('validateMailDraft', () => {
  it('accepts every long edge the page offers', () => {
    for (const edge of [1200, 1600, 2048] as const)
      expect(validateMailDraft(mailDraft({ mail_long_edge: edge }))).toEqual({})
  })

  it('refuses a long edge the page does not offer', () => {
    expect(
      validateMailDraft(mailDraft({ mail_long_edge: 4096 as unknown as 1600 })).mail_long_edge,
    ).toMatch(/1600/)
  })

  it('accepts both ends of the mail quality band and refuses either side of it', () => {
    expect(validateMailDraft(mailDraft({ mail_jpeg_quality: 77 }))).toEqual({})
    expect(validateMailDraft(mailDraft({ mail_jpeg_quality: 83 }))).toEqual({})
    expect(validateMailDraft(mailDraft({ mail_jpeg_quality: 76 })).mail_jpeg_quality).toBe(
      'Between 77 and 83.',
    )
    expect(validateMailDraft(mailDraft({ mail_jpeg_quality: 84 })).mail_jpeg_quality).toBe(
      'Between 77 and 83.',
    )
    expect(validateMailDraft(mailDraft({ mail_jpeg_quality: NaN })).mail_jpeg_quality).toBe(
      'A whole number is required.',
    )
  })

  it('accepts both subsamplings and refuses a third', () => {
    for (const sub of ['4:2:0', '4:4:4'] as const)
      expect(validateMailDraft(mailDraft({ mail_subsampling: sub }))).toEqual({})
    expect(
      validateMailDraft(mailDraft({ mail_subsampling: '4:1:1' as unknown as '4:2:0' }))
        .mail_subsampling,
    ).toBeTruthy()
  })

  it('accepts the progressive toggle either way and refuses a non-boolean', () => {
    for (const on of [true, false])
      expect(validateMailDraft(mailDraft({ mail_progressive: on }))).toEqual({})
    expect(
      validateMailDraft(mailDraft({ mail_progressive: 1 as unknown as boolean })).mail_progressive,
    ).toBeTruthy()
  })
})

describe('the how-to-query block', () => {
  it('starts on the last thirty days, ending today, in local time', () => {
    // 2026-03-01 local, whatever the machine's zone: constructed from local parts on purpose.
    const now = new Date(2026, 2, 1, 23, 30)
    expect(defaultExportQuery(now)).toEqual({ from: '2026-01-31', to: '2026-03-01' })
  })

  it('writes a command with the range, and no token in it', () => {
    const command = exportCurlCommand({ from: '2026-05-01', to: '2026-05-31' }, 'https://memos.test')
    expect(command).toContain(
      'https://memos.test/api/exports/photobook?from=2026-05-01&to=2026-05-31&format=zip',
    )
    expect(command).toContain('Authorization: Bearer $MEMOS_PAT')
    // the literal token stays a shell variable — nothing that looks like a secret is interpolated
    expect(command).not.toMatch(/Bearer\s+[A-Za-z0-9._-]{8,}/)
  })

  it('does not double the slash when the origin carries one', () => {
    expect(exportCurlCommand({ from: '2026-05-01', to: '2026-05-31' }, 'https://memos.test/')).toContain(
      'https://memos.test/api/exports/photobook',
    )
  })

  it('refuses a date that is not a day, and a range that runs backwards', () => {
    expect(validateExportQuery({ from: '2026-02-30', to: '2026-03-01' }).from).toBeTruthy()
    expect(validateExportQuery({ from: '2026-05-01', to: 'soon' }).to).toBeTruthy()
    expect(validateExportQuery({ from: '2026-05-31', to: '2026-05-01' }).to).toMatch(/before/)
    expect(validateExportQuery({ from: '2026-05-01', to: '2026-05-01' })).toEqual({})
  })

  it('names what the print profile currently does', () => {
    expect(describePrintProfile(settings())).toBe(
      '4096 px on the long edge, JPEG quality 92, metadata stripped, neutral monochrome as single-channel',
    )
    expect(
      describePrintProfile(
        settings({
          print_long_edge: 0,
          print_metadata_policy: 'keep_dates',
          print_grayscale_single_channel: false,
        }),
      ),
    ).toBe('the original size, JPEG quality 92, capture dates kept, every image in colour')
  })
})
describe('the whole row is validated, not just the half being edited', () => {
  // A save replaces the entire row, so it carries the other profile's STORED values. A row that
  // already holds an out-of-band value would have the save re-assert it, and the API would answer
  // 422 about a field the admin never touched.
  it('refuses a Mail save when the stored print long edge is off the published rungs', () => {
    const server = settings({ print_long_edge: 3000 })
    const result = putBodyFromMailDraft(mailDraft({ mail_jpeg_quality: 77 }), server)
    expect(result.ok).toBe(false)
    if (result.ok) return
    expect(result.blockedBy).toBe('print')
    expect(result.message).toBe(
      'The Print profile holds a value outside its bands — open it and fix it first.',
    )
  })

  it('refuses a Mail save when the stored print quality is outside its band', () => {
    const result = putBodyFromMailDraft(mailDraft(), settings({ print_jpeg_quality: 40 }))
    expect(result.ok).toBe(false)
    if (!result.ok) expect(result.blockedBy).toBe('print')
  })

  it('refuses a Print save when the stored mail values are out of band, naming Mail', () => {
    const result = putBodyFromPrintDraft(printDraft(), settings({ mail_long_edge: 999 }))
    expect(result.ok).toBe(false)
    if (result.ok) return
    expect(result.blockedBy).toBe('mail')
    expect(result.message).toBe(
      'The Mail profile holds a value outside its bands — open it and fix it first.',
    )
  })

  it('points at the fields above when it is the edited profile that is broken', () => {
    const result = putBodyFromPrintDraft(printDraft({ print_jpeg_quality: 200 }), settings())
    expect(result.ok).toBe(false)
    if (!result.ok) expect(result.message).toMatch(/fix the fields above/)
  })

  it('sends both groups as before when the whole row is in band', () => {
    const server = settings()
    const print = putBodyFromPrintDraft(printDraft({ print_long_edge: 5000 }), server)
    const mail = putBodyFromMailDraft(mailDraft({ mail_progressive: true }), server)
    expect(print.ok).toBe(true)
    expect(mail.ok).toBe(true)
    if (print.ok) expect(print.body.print_long_edge).toBe(5000)
    if (mail.ok) expect(mail.body.mail_progressive).toBe(true)
    if (print.ok) expect(Object.keys(print.body).length).toBe(8)
  })
})
