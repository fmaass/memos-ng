// The reading column shared by every memo feed (Home, Explore, Archived, Search). One rule in one
// place: the views each carried their own literal and drifted. Resources is a gallery rather than a
// reading column and deliberately keeps its own wider cap.
export const CONTENT_COLUMN = 'max-w-2xl xl:max-w-4xl'

// What that rule resolves to in px, plus the padding each view sets inside it (`p-4`). A responsive
// image has to state its rendered width (`sizes`) in CSS units before any layout exists, so these
// numbers live next to the class they mirror — change one, change the other.
export const COLUMN_MAX_PX = 672 // max-w-2xl (42rem)
export const COLUMN_MAX_XL_PX = 896 // max-w-4xl (56rem)
export const COLUMN_XL_MIN_PX = 1280 // Tailwind `xl` breakpoint (80rem)
export const COLUMN_PAD_PX = 16 // the views' `p-4`
export const REM_PX = 16 // root font size — what the rem caps above (and the media height cap) are in

// AppShell's persistent sidebar, which the column has to share the viewport with. Below `md` it is
// a drawer and takes no layout width at all; from `md` up it is an in-flow `shrink-0` aside, so the
// content column starts a sidebar's width in. Mirrors AppShell.vue's
// `hidden md:flex … w-60` — change the class, change these.
export const SIDEBAR_WIDTH_PX = 240 // `w-60` (border-box, so its `border-r` is inside the 240)
export const SIDEBAR_MIN_PX = 768 // Tailwind `md` — where `hidden md:flex` puts the aside in flow
