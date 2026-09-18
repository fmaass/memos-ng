<script setup lang="ts">
import Button from 'primevue/button'
import SelectButton from 'primevue/selectbutton'
import { computed } from 'vue'

import {
  bucketLabel,
  type Histogram,
  type PlaybackSpeed,
  type TimelineAction,
  type TimelineMode,
  type TimelineState,
} from '@/lib/places/timeline'
import {
  BAR_HEIGHT,
  BAR_WIDTH,
  barControls,
  barGeometry,
  indexAtX,
  speedLabel,
  yearTickGeometry,
} from '@/lib/places/timelineBar'

const props = defineProps<{ histogram: Histogram; state: TimelineState; phone: boolean }>()
// The bar asks; the view's reducer decides. Keeping the state out of here is what lets the
// keyboard, the tick loop and the bar drive one playhead instead of three.
const emit = defineEmits<{ action: [TimelineAction] }>()

const controls = computed(() => barControls(props.state, props.phone))
const geometry = computed(() => barGeometry(props.histogram, { index: props.state.index }))
const years = computed(() => yearTickGeometry(props.histogram.buckets))
const empty = computed(() => props.histogram.buckets.length === 0)
const label = computed(() => {
  const bucket = props.histogram.buckets[props.state.index]
  return bucket ? bucketLabel(bucket, props.histogram.zone) : 'No dates'
})
const speedOptions = computed(() =>
  (controls.value.speeds ?? []).map((speed) => ({ label: speedLabel(speed), value: speed })),
)
const percent = (value: number) => `${(value / BAR_WIDTH) * 100}%`

function togglePlay() {
  emit('action', props.state.playing ? { type: 'pause' } : { type: 'play' })
}

function onSeek(event: MouseEvent) {
  const box = (event.currentTarget as Element).getBoundingClientRect()
  // A bar that has not been laid out yet has zero width, and a seek divided by it is NaN — the
  // viewBox is the same coordinate space, so falling back to it keeps the click meaningful.
  const width = box.width || BAR_WIDTH
  emit('action', {
    type: 'seek',
    index: indexAtX(event.clientX - box.left, width, props.histogram.buckets.length),
  })
}

function onSpeed(speed: PlaybackSpeed | null) {
  if (speed != null) emit('action', { type: 'setSpeed', speed })
}

function onMode(mode: TimelineMode | null) {
  if (mode != null) emit('action', { type: 'setMode', mode })
}
</script>

<template>
  <section
    class="places-timeline flex flex-nowrap items-center gap-3 rounded-lg bg-surface-0/85 px-3 py-2 shadow backdrop-blur dark:bg-surface-900/85"
    :class="props.phone ? 'text-xs' : 'text-sm'"
    data-test="timeline-bar"
    :data-variant="controls.variant"
    aria-label="Timeline playback"
  >
    <Button
      :icon="controls.playIcon"
      :aria-label="controls.playLabel"
      :disabled="empty"
      rounded
      size="small"
      data-test="timeline-play"
      @click="togglePlay"
    />

    <div class="min-w-0 flex-1">
      <svg
        class="block h-10 w-full cursor-pointer"
        :viewBox="`0 0 ${BAR_WIDTH} ${BAR_HEIGHT}`"
        preserveAspectRatio="none"
        role="presentation"
        data-test="timeline-chart"
        @click="onSeek"
      >
        <rect
          v-for="bar in geometry.bars"
          :key="bar.key"
          :x="bar.x"
          :y="bar.y"
          :width="bar.width"
          :height="bar.height"
          :class="bar.past ? 'timeline-bucket--past' : 'timeline-bucket--future'"
          data-test="timeline-bucket"
          :data-key="bar.key"
        />
        <line
          v-if="!empty"
          :x1="geometry.playheadX"
          :x2="geometry.playheadX"
          y1="0"
          :y2="BAR_HEIGHT"
          class="timeline-playhead"
          data-test="timeline-playhead"
          :data-x="geometry.playheadX"
        />
      </svg>
      <!-- Year ticks sit under the bar in the SAME proportional space, so the playhead's month
           and its year always read as one column. -->
      <div v-if="!props.phone" class="relative h-4 w-full">
        <span
          v-for="year in years"
          :key="year.year"
          class="absolute top-0 overflow-hidden border-l border-surface-300 pl-1 text-[10px] text-muted-color dark:border-surface-700"
          :style="{ left: percent(year.x), width: percent(year.width) }"
          data-test="timeline-year"
          :data-year="year.year"
        >
          {{ year.year }}
        </span>
      </div>
    </div>

    <span class="whitespace-nowrap tabular-nums" data-test="timeline-label">{{ label }}</span>

    <Button
      v-if="controls.chip"
      :label="controls.chip.label"
      size="small"
      outlined
      class="whitespace-nowrap"
      aria-label="Playback speed"
      data-test="timeline-speed-chip"
      @click="onSpeed(controls.chip.next)"
    />
    <SelectButton
      v-if="controls.speeds"
      :model-value="props.state.speed"
      :options="speedOptions"
      option-label="label"
      option-value="value"
      :allow-empty="false"
      size="small"
      aria-label="Playback speed"
      data-test="timeline-speed"
      @update:model-value="onSpeed"
    />
    <SelectButton
      v-if="controls.modes"
      :model-value="props.state.mode"
      :options="controls.modes"
      option-label="label"
      option-value="value"
      :allow-empty="false"
      size="small"
      aria-label="What the map shows"
      data-test="timeline-mode"
      @update:model-value="onMode"
    />
  </section>
</template>

<style scoped>
.timeline-bucket--past {
  fill: var(--p-primary-color, #6366f1);
}
.timeline-bucket--future {
  fill: var(--p-surface-400, #9ca3af);
  opacity: 0.55;
}
.timeline-playhead {
  stroke: var(--p-text-color, #111);
  stroke-width: 4;
  vector-effect: non-scaling-stroke;
}
</style>
