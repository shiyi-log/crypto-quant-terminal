<script lang="ts" setup>
import SystemState from './SystemState.vue';

defineOptions({ name: 'SystemForbidden' });
</script>

<template>
  <SystemState status="403" />
</template>
