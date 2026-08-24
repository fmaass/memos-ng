import { describe, expect, it } from 'vitest'

import router from '@/router'

describe('routes', () => {
  it('resolves the memo permalink /m/:uid', () => {
    // The canonical memo URL: shared out of the app, claimed by the AASA applinks document, and
    // served by the SPA shell for a phone with no app installed. A route that stops resolving
    // turns every shared link into a blank shell.
    const resolved = router.resolve('/m/01J8Z3K9QW7X2E5R6T7Y8U9I0P')

    expect(resolved.name).toBe('memo')
    expect(resolved.params.uid).toBe('01J8Z3K9QW7X2E5R6T7Y8U9I0P')
    expect(resolved.matched).toHaveLength(1)
  })

  it('does not swallow the existing top-level routes', () => {
    expect(router.resolve('/').name).toBe('home')
    expect(router.resolve('/search').name).toBe('search')
  })
})
