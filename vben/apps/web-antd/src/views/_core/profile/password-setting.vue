<script setup lang="ts">
import { computed } from 'vue';

import { useUserStore } from '@vben/stores';

import PasswordWorkbench from '../../quant/security/PasswordWorkbench.vue';

const userStore = useUserStore();
const username = computed(() => userStore.userInfo?.username ?? '');
</script>
<template>
  <PasswordWorkbench
    :key="username"
    :username="username"
    :allow-recovery="false"
  />
</template>
