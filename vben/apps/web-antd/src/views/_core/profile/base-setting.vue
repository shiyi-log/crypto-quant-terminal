<script setup lang="ts">
import { computed } from 'vue';

import { useUserStore } from '@vben/stores';

const userStore = useUserStore();
const info = computed(() => userStore.userInfo);
const roles = computed(() =>
  Array.isArray(info.value?.roles)
    ? info.value.roles.filter(
        (role: unknown): role is string => typeof role === 'string',
      )
    : [],
);
</script>
<template>
  <div>
    <h2>基本信息</h2>
    <p class="identity-help">由认证服务维护，当前页面只读。</p>
    <dl class="identity-records">
      <div>
        <dt>用户名</dt>
        <dd>{{ info?.username || '尚未确认' }}</dd>
      </div>
      <div>
        <dt>姓名</dt>
        <dd>{{ info?.realName || '未提供' }}</dd>
      </div>
      <div>
        <dt>角色</dt>
        <dd>{{ roles.length ? roles.join('、') : '未提供' }}</dd>
      </div>
      <div>
        <dt>说明</dt>
        <dd>{{ info?.desc || '未提供' }}</dd>
      </div>
    </dl>
  </div>
</template>
