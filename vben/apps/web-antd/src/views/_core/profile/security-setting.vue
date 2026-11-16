<script setup lang="ts">
const rows = [
  {
    label: '账户密码',
    state: '可修改',
    detail: '修改需验证当前密码，成功后重新登录。',
  },
  { label: '密保手机', state: '暂不提供', detail: '当前服务未提供手机绑定。' },
  {
    label: '备用邮箱',
    state: '暂不提供',
    detail: '当前服务未提供邮箱绑定或邮件恢复。',
  },
  { label: 'MFA 设备', state: '暂不提供', detail: '当前服务未提供二次验证。' },
];
</script>
<template>
  <div>
    <h2>安全状态</h2>
    <p class="identity-help">只展示当前服务支持的能力。</p>
    <table class="identity-security-table">
      <thead>
        <tr>
          <th scope="col">验证方式</th>
          <th scope="col">能力状态</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="row in rows" :key="row.label">
          <th scope="row">{{ row.label }}</th>
          <td>
            {{ row.state }}
            <p>{{ row.detail }}</p>
          </td>
        </tr>
      </tbody>
    </table>
  </div>
</template>
