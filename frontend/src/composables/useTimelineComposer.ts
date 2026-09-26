import type { InjectionKey, Ref } from 'vue'

// Whether the Timeline's new-memo editor is open. The shell owns it (its header carries the
// new-memo button on `/`) and the Timeline view reads it to show the editor inline on md+; below md
// the header's own dialog shows it instead.
export const TIMELINE_COMPOSER_KEY: InjectionKey<Ref<boolean>> = Symbol('timelineComposer')

/** Below Tailwind's `md`: where the editor opens as a dialog rather than inline. */
export const PHONE_QUERY = '(max-width: 767px)'
