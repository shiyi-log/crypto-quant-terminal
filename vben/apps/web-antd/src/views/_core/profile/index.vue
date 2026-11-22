<script setup lang="ts">
import { computed, ref } from 'vue';

import { useUserStore } from '@vben/stores';

import ProfileBase from './base-setting.vue';
import ProfilePasswordSetting from './password-setting.vue';
import ProfileSecuritySetting from './security-setting.vue';
import './profile.css';

const userStore = useUserStore();
const username = computed(() => userStore.userInfo?.username ?? '尚未确认');
const selected = ref('basic');
const tabs = [
  { key: 'basic', label: '基本信息' },
  { key: 'security', label: '安全状态' },
  { key: 'password', label: '修改密码' },
];
</script>
<template>
  <main class="identity-workspace">
    <header class="identity-page-heading">
      <div>
        <h1>个人身份</h1>
        <p>工作站用户信息与登录安全。</p>
      </div>
    </header>
    <div class="identity-workspace-grid">
      <aside class="identity-summary">
        <span class="identity-summary-label">当前登录身份</span
        ><strong>{{ username }}</strong>
        <p>登录身份不代表交易账户所有权或策略上线批准。</p>
        <nav class="identity-tabs" aria-label="身份设置">
          <button
            v-for="tab in tabs"
            :key="tab.key"
            :aria-current="selected === tab.key ? 'page' : undefined"
            type="button"
            @click="selected = tab.key"
          >
            {{ tab.label }}
          </button>
        </nav>
      </aside>
      <section class="identity-content" aria-live="polite">
        <ProfileBase v-if="selected === 'basic'" /><ProfileSecuritySetting
          v-else-if="selected === 'security'"
        /><ProfilePasswordSetting v-else />
      </section>
    </div>
  </main>
</template>
