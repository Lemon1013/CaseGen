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
    <rect x="1.5" y="1.5" width="29" height="29" rx="7" fill="#09090b" stroke="#27272a" stroke-width="1.5" />
    <path d="M8.5 11.5L14 16L8.5 20.5" stroke="#ffffff" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round" />
    <path d="M16.5 20.5H23.5" stroke="#a1a1aa" stroke-width="2.2" stroke-linecap="round" />
    <circle cx="21" cy="11.5" r="2.5" fill="#10b981" />
  </svg>
</template>
