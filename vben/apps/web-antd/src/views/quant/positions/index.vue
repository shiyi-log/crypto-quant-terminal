<script setup lang="ts">
import { onUnmounted } from 'vue';
import PositionsWorkspace from './PositionsWorkspace.vue';
import { createLegacyPositionsState } from './positionsApi';

const state = createLegacyPositionsState();
onUnmounted(() => state.dispose());
</script>

<template><PositionsWorkspace :state="state" /></template>
