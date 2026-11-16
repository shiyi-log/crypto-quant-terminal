<script lang="ts" setup>
import SystemState from './SystemState.vue';

defineOptions({ name: 'SystemInternalError' });
</script>

<template>
  <SystemState status="500" />
</template>
