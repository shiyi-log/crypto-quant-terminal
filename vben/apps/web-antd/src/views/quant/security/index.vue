<script lang="ts" setup>
import { computed, reactive, ref } from 'vue';

import { useUserStore } from '@vben/stores';

import { Button, Card, Form, Input, message, Result, Tag } from 'ant-design-vue';

import { changePasswordApi, resetPasswordApi } from '#/api/core/auth';

const userStore = useUserStore();
/** 用户名来自登录后写入的 userInfo，不再写死 admin */
const username = computed(() => userStore.userInfo?.username ?? '');
const realName = computed(() => userStore.userInfo?.realName ?? '');

const form = reactive({
  confirm: '',
  newPassword: '',
  oldPassword: '',
});
const loading = ref(false);
const done = ref(false);
const resetLoading = ref(false);

const rules = {
  oldPassword: [{ required: true, message: '请输入当前密码' }],
  newPassword: [
    { required: true, message: '请输入新密码' },
    { min: 6, message: '新密码至少 6 位' },
  ],
  confirm: [
    { required: true, message: '请再次输入新密码' },
    {
      validator: (_rule: any, value: string) =>
        value === form.newPassword
          ? Promise.resolve()
          : Promise.reject(new Error('两次输入的密码不一致')),
    },
  ],
};

async function submit() {
  if (!form.oldPassword || !form.newPassword) {
    return;
  }
  if (form.newPassword !== form.confirm) {
    message.error('两次输入的密码不一致');
    return;
  }
  loading.value = true;
  try {
    const res = await changePasswordApi(form.oldPassword, form.newPassword);
    message.success(res?.message ?? '密码已修改');
    done.value = true;
    form.oldPassword = '';
    form.newPassword = '';
    form.confirm = '';
    // 密码已变更，当前会话令牌应立即作废，强制重新登录
    setTimeout(() => {
      localStorage.removeItem('ft_access_token');
      location.href = '/';
    }, 1800);
  } catch (error: any) {
    message.error(error?.message ?? '修改失败');
  } finally {
    loading.value = false;
  }
}

async function reset() {
  resetLoading.value = true;
  try {
    const res = await resetPasswordApi(username.value, undefined);
    message.success(`已重置，新密码：${res.password}`, 10);
  } catch (error: any) {
    message.error(error?.message ?? '重置失败');
  } finally {
    resetLoading.value = false;
  }
}
</script>

<template>
  <div class="p-4">
    <div class="grid grid-cols-1 gap-4 lg:grid-cols-2">
      <!-- 修改密码 -->
      <Card :bordered="false" class="shadow-sm" title="修改密码">
        <Result
          v-if="done"
          status="success"
          sub-title="即将退出登录，请使用新密码重新登录…"
          title="密码修改成功"
        />
        <template v-else>
          <Form :model="form" :rules="rules" layout="vertical" @finish="submit">
            <Form.Item label="用户名">
              <Input :value="username || '—'" disabled />
            </Form.Item>
            <Form.Item label="当前密码" name="oldPassword">
              <Input.Password
                v-model:value="form.oldPassword"
                autocomplete="current-password"
                placeholder="请输入当前密码"
              />
            </Form.Item>
            <Form.Item label="新密码" name="newPassword">
              <Input.Password
                v-model:value="form.newPassword"
                autocomplete="new-password"
                placeholder="至少 6 位"
              />
            </Form.Item>
            <Form.Item label="确认新密码" name="confirm">
              <Input.Password
                v-model:value="form.confirm"
                autocomplete="new-password"
                placeholder="再次输入新密码"
              />
            </Form.Item>
            <Button :loading="loading" block html-type="submit" type="primary">
              确认修改
            </Button>
          </Form>
          <div class="mt-3 text-xs text-muted-foreground">
            密码保存在本机 <code>auth/users.json</code>，使用 PBKDF2-SHA256 加盐哈希，
            不保存明文。修改后需要重新登录。
          </div>
        </template>
      </Card>

      <!-- 重置密码 -->
      <Card :bordered="false" class="shadow-sm" title="重置密码">
        <div class="mb-4 text-sm text-muted-foreground">
          忘记密码时使用。重置会生成一个随机新密码，旧密码立即失效。
          <template v-if="username">
            当前账号：<b>{{ username }}</b><template v-if="realName">（{{ realName }}）</template>
          </template>
        </div>
        <div class="mb-4">
          <Tag color="warning">仅允许从本机操作</Tag>
        </div>
        <Button :loading="resetLoading" danger @click="reset">
          生成随机新密码
        </Button>
        <div class="mt-3 text-xs text-muted-foreground">
          重置结果会以提示形式显示，请及时复制保存。
          也可通过命令行重置：
          <pre class="mt-2 overflow-x-auto rounded bg-gray-100 p-2 text-xs dark:bg-gray-800">curl -s -X POST http://127.0.0.1:8890/auth/reset-password \
  -H "Content-Type: application/json" \
  -d '{"username":"admin"}'</pre>
        </div>
      </Card>
    </div>
  </div>
</template>
