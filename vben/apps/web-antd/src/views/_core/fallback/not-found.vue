<script lang="ts" setup>
import SystemState from './SystemState.vue';

defineOptions({ name: 'SystemNotFound' });
</script>

<template>
  <SystemState status="404" />
</template>
