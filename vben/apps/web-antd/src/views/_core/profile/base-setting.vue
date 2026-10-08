<script setup lang="ts">
import { computed } from 'vue';

import { useUserStore } from '@vben/stores';

import { Descriptions, Tag } from 'ant-design-vue';

/**
 * 基本信息 —— 只读展示。
 *
 * 之前这里是一张可编辑表单 +「更新基本信息」按钮，但既没有后端接口、
 * 组件也没监听 submit：改完点保存没有任何反应，角色下拉还是写死的
 * MOCK_ROLES_OPTIONS（管理员/用户/测试）。现在改为只读展示真实账号信息。
 */
const userStore = useUserStore();
const info = computed<any>(() => userStore.userInfo ?? {});
const roles = computed<string[]>(() => info.value.roles ?? []);
</script>
<template>
  <Descriptions :column="1" bordered size="small">
    <Descriptions.Item label="用户名">
      {{ info.username || '—' }}
    </Descriptions.Item>
    <Descriptions.Item label="姓名">
      {{ info.realName || '—' }}
    </Descriptions.Item>
    <Descriptions.Item label="角色">
      <Tag v-for="r in roles" :key="r">{{ r }}</Tag>
      <span v-if="!roles.length">—</span>
    </Descriptions.Item>
    <Descriptions.Item label="说明">
      {{ info.desc || '—' }}
    </Descriptions.Item>
  </Descriptions>
  <div class="mt-3 text-xs text-muted-foreground">
    账号由认证服务维护（<code>auth/users.json</code>），本页不提供在线编辑。
    修改密码请使用「修改密码」标签页。
  </div>
</template>
