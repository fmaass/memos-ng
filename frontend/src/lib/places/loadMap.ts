/**
 * The one loader the map pane and the idle warm-up both go through, so what was warmed is exactly
 * the chunk the pane later asks for.
 */
export const loadPlacesMap = () => import('@/components/places/PlacesMap.vue')
