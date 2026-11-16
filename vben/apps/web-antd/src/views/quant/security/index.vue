<script lang="ts" setup>
import { computed } from 'vue';
import { RouterLink } from 'vue-router';

import { useUserStore } from '@vben/stores';

import PasswordWorkbench from './PasswordWorkbench.vue';
import './identity.css';

const userStore = useUserStore();
const username = computed(() => userStore.userInfo?.username ?? '');
const realName = computed(() => userStore.userInfo?.realName ?? '');
</script>

<template>
  <main class="identity-workspace">
    <header class="identity-page-heading">
      <div>
        <h1>身份与安全</h1>
        <p>管理工作站登录密码与本机恢复。</p>
      </div>
      <RouterLink to="/profile">查看个人信息</RouterLink>
    </header>
    <div class="identity-workspace-grid">
      <aside class="identity-summary" aria-label="当前登录身份">
        <span class="identity-summary-label">当前登录身份</span
        ><strong>{{ username || '尚未确认' }}</strong
        ><span>{{ realName || '未提供姓名' }}</span>
        <dl>
          <div>
            <dt>凭据范围</dt>
            <dd>工作站登录</dd>
          </div>
          <div>
            <dt>交易账户</dt>
            <dd>单独核验</dd>
          </div>
          <div>
            <dt>认证方式</dt>
            <dd>用户名与密码</dd>
          </div>
        </dl>
        <p>
          登录身份与 Binance
          Demo、纸面研究及实盘交易账户分别管理。修改此密码不更改交易凭据，也不解除交易风险锁。
        </p>
      </aside>
      <PasswordWorkbench :key="username" :username="username" />
    </div>
  </main>
</template>
