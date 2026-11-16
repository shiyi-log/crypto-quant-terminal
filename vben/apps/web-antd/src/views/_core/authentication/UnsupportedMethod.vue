<script lang="ts" setup>
import { computed } from 'vue';
import { RouterLink, useRoute } from 'vue-router';

defineProps<{ method: string }>();
const route = useRoute();
// 只携带既有登录返回目标，实际跳转仍由统一守卫处理。
const loginQuery = computed(() =>
  typeof route.query.redirect === 'string'
    ? { redirect: route.query.redirect }
    : {},
);
</script>

<template>
  <section class="unsupported-method" aria-labelledby="unsupported-title">
    <p class="method-context">访问方式</p>
    <h1 id="unsupported-title">当前不支持{{ method }}</h1>
    <p>此终端使用用户名和密码登录。本机访问也可由认证服务核验后免密进入。</p>
    <p>
      如需恢复密码，请在运行终端的本机使用恢复入口；此服务不发送验证码或恢复邮件。
    </p>
    <nav aria-label="可用登录入口">
      <RouterLink :to="{ path: '/auth/login', query: loginQuery }"
        >返回用户名密码登录</RouterLink
      >
      <RouterLink
        :to="{
          path: '/auth/login',
          query: { ...loginQuery, recovery: 'local' },
        }"
        >本机恢复密码</RouterLink
      >
    </nav>
  </section>
</template>

<style scoped>
.unsupported-method {
  min-width: 0;
  color: #e5edf4;
  font-family:
    'IBM Plex Sans', 'Noto Sans SC', 'Source Han Sans SC', sans-serif;
}
h1,
p {
  margin: 0;
}
.method-context {
  color: #69b4e4;
  font-size: 13px;
}
h1 {
  margin: 12px 0 22px;
  font-size: 26px;
  font-weight: 600;
  line-height: 1.4;
}
p {
  color: #a2b8c9;
  font-size: 14px;
  line-height: 1.8;
}
p + p {
  margin-top: 12px;
}
nav {
  display: grid;
  gap: 12px;
  margin-top: 30px;
  padding-top: 24px;
  border-top: 1px solid #29485c;
}
a {
  display: block;
  padding: 12px 14px;
  border: 1px solid #49677c;
  color: #69b4e4;
  text-align: center;
  border-radius: 3px;
}
a:focus-visible {
  outline: 2px solid #69b4e4;
  outline-offset: 3px;
}
a:hover {
  border-color: #69b4e4;
  background: #132b3e;
}
</style>
