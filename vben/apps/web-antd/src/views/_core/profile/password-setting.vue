<script setup lang="ts">
import type { VbenFormSchema } from '#/adapter/form';

import { computed } from 'vue';

import { ProfilePasswordSetting, z } from '@vben/common-ui';

import { message } from 'ant-design-vue';

import { changePasswordApi } from '#/api/core/auth';

const formSchema = computed((): VbenFormSchema[] => {
  return [
    {
      fieldName: 'oldPassword',
      label: '旧密码',
      component: 'VbenInputPassword',
      componentProps: {
        placeholder: '请输入旧密码',
      },
    },
    {
      fieldName: 'newPassword',
      label: '新密码',
      component: 'VbenInputPassword',
      componentProps: {
        passwordStrength: true,
        placeholder: '请输入新密码',
      },
    },
    {
      fieldName: 'confirmPassword',
      label: '确认密码',
      component: 'VbenInputPassword',
      componentProps: {
        passwordStrength: true,
        placeholder: '请再次输入新密码',
      },
      dependencies: {
        rules(values) {
          const { newPassword } = values;
          return z
            .string({ error: '请再次输入新密码' })
            .min(1, { message: '请再次输入新密码' })
            .refine((value) => value === newPassword, {
              message: '两次输入的密码不一致',
            });
        },
        triggerFields: ['newPassword'],
      },
    },
  ];
});

/**
 * 真正调用认证服务的改密接口。
 *
 * 这里原来只有 `message.success('密码修改成功')` —— 不管后端发生了什么，
 * 点一下都提示成功，但密码根本没改。属于最危险的一类「写死」。
 */
async function handleSubmit(values: Record<string, any>) {
  try {
    const res = await changePasswordApi(values.oldPassword, values.newPassword);
    message.success(res?.message ?? '密码已修改，请重新登录');
    // 密码已变更，当前会话令牌立即作废
    setTimeout(() => {
      localStorage.removeItem('ft_access_token');
      location.href = '/';
    }, 1800);
  } catch (error: any) {
    message.error(error?.message ?? '修改失败');
  }
}
</script>
<template>
  <ProfilePasswordSetting
    class="w-1/3"
    :form-schema="formSchema"
    @submit="handleSubmit"
  />
</template>
