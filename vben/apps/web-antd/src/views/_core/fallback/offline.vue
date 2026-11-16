<script lang="ts" setup>
import SystemState from './SystemState.vue';

defineOptions({ name: 'SystemOffline' });
</script>

<template>
  <SystemState status="offline" />
</template>
