<template>
  <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
    <template v-if="name === 'more'"><circle cx="5" cy="12" r="1" /><circle cx="12" cy="12" r="1" /><circle cx="19" cy="12" r="1" /></template>
    <path v-else :d="paths[name]" />
  </svg>
</template>

<script setup lang="ts">
defineProps<{ name: 'left' | 'right' | 'down' | 'edit' | 'chat' | 'download' | 'more' | 'check' }>()
const paths = {
  left: 'm14 6-6 6 6 6', right: 'm10 6 6 6-6 6', down: 'm6 9 6 6 6-6',
  edit: 'm14 5 5 5M4 20l4-1L20 7a2 2 0 0 0-4-4L4 15v5Z',
  chat: 'M20 11a8 8 0 0 1-8 8H5l-3 3V11a9 9 0 0 1 18 0Z',
  download: 'M12 3v12m-4-4 4 4 4-4M4 16v4h16v-4', check: 'm5 12 4 4L19 6'
}
</script>

<style scoped>
svg { width: 18px; height: 18px; flex: 0 0 18px; }
</style>
