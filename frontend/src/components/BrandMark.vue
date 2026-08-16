<script setup lang="ts">
withDefaults(defineProps<{
  size?: number | string
  title?: string
}>(), {
  size: 32,
  title: '',
})
</script>

<template>
  <svg
    :width="size"
    :height="size"
    viewBox="0 0 32 32"
    fill="none"
    xmlns="http://www.w3.org/2000/svg"
    :role="title ? 'img' : undefined"
    :aria-label="title || undefined"
    :aria-hidden="title ? undefined : 'true'"
  >
    <title v-if="title">{{ title }}</title>
    <defs>
      <linearGradient id="casegen-brand-gradient" x1="3" y1="2" x2="29" y2="30" gradientUnits="userSpaceOnUse">
        <stop stop-color="#5B6DF9" />
        <stop offset="1" stop-color="#8B5CF6" />
      </linearGradient>
    </defs>
    <rect x="1" y="1" width="30" height="30" rx="8" fill="url(#casegen-brand-gradient)" />
    <path d="M10 6.5h12.5a2 2 0 0 1 2 2V18" stroke="white" stroke-width="2.1" stroke-linecap="round" opacity=".5" />
    <path d="M8 9.5h12.5a2 2 0 0 1 2 2V21" stroke="white" stroke-width="2.1" stroke-linecap="round" opacity=".72" />
    <rect x="6" y="12.5" width="14.5" height="13" rx="2.25" fill="white" fill-opacity=".14" stroke="white" stroke-width="2.1" />
    <path d="M10 17h6.5M10 21h4.5" stroke="white" stroke-width="2" stroke-linecap="round" />
  </svg>
</template>
