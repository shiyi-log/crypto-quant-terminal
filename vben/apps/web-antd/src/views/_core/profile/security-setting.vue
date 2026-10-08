<script setup lang="ts">
import { Card, Tag } from 'ant-design-vue';

/**
 * 安全设置 —— 只读。
 *
 * 之前是一排可以拨动的开关，外加「当前密码强度：强」「已绑定手机：138****8293」
 * 「已绑定邮箱：ant***sign.com」这些写死的文案 —— 开关没有后端（拨了不保存），
 * 文案是 Vben 示例残留（本项目根本没有手机/邮箱绑定功能）。
 * 这里只陈述真实状态。
 */
const rows = [
  {
    desc: 'PBKDF2-SHA256 加盐存储，可在「修改密码」中更改',
    label: '账户密码',
    ok: true,
    state: '已设置',
  },
  {
    desc: '本系统暂不提供手机绑定',
    label: '密保手机',
    ok: false,
    state: '未绑定',
  },
  {
    desc: '本系统暂不提供邮箱绑定',
    label: '备用邮箱',
    ok: false,
    state: '未绑定',
  },
  {
    desc: '本系统暂不提供二次验证',
    label: 'MFA 设备',
    ok: false,
    state: '未绑定',
  },
];
</script>
<template>
  <Card :bordered="false">
    <div class="space-y-4">
      <div
        v-for="r in rows"
        :key="r.label"
        class="flex flex-row items-center justify-between rounded-lg border p-4"
      >
        <div class="space-y-0.5">
          <div class="text-base font-medium">{{ r.label }}</div>
          <div class="text-sm text-muted-foreground">{{ r.desc }}</div>
        </div>
        <Tag :color="r.ok ? 'green' : 'default'">{{ r.state }}</Tag>
      </div>
    </div>
  </Card>
</template>
