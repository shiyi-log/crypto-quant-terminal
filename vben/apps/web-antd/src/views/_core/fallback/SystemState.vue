<script lang="ts" setup>
import { computed } from 'vue';
import { RouterLink, useRoute, useRouter } from 'vue-router';

const props = defineProps<{
  status: '403' | '404' | '500' | 'coming-soon' | 'offline';
}>();

const route = useRoute();
const router = useRouter();
const states = {
  '403': {
    code: '403',
    title: '当前账户无权访问',
    reason:
      '当前登录身份没有此页面的访问权限。请确认账户，或联系系统管理员核对权限。',
    next: '返回可访问的页面后，可从账户菜单核对登录身份。',
  },
  '404': {
    code: '404',
    title: '找不到这个页面',
    reason:
      '当前地址没有对应页面。历史链接可能已调整，请核对地址，或从本地演示入口重新进入。',
    next: '若这是历史运行或交易记录，请保留原始编号，并在对应证据入口查询。',
  },
  '500': {
    code: '读取异常',
    title: '页面暂时无法读取',
    reason:
      '本次页面请求未能完成，当前无法确认最新账户与服务事实。请稍后重试原地址。',
    next: '页面读取失败不代表交易命令被撤销或已经完成。请通过后续账户与订单对账确认。',
  },
  offline: {
    code: '连接中断',
    title: '暂时无法连接服务',
    reason: '页面当前无法取得服务响应。请检查网络与本机服务，再重试原地址。',
    next: '连接恢复前，账户、风险与订单状态保持未知，不能据此判断交易已停止。',
  },
  'coming-soon': {
    code: '尚未开放',
    title: '此功能尚未开放',
    reason:
      '此入口尚未提供可用功能。当前页面没有提交任务，也没有修改账户或交易配置。',
    next: '可返回本地演示预览查看固定样本。',
  },
} as const;

const state = computed(() => states[props.status]);
const canRetry = computed(
  () => props.status === '500' || props.status === 'offline',
);
// 地址仅展示路由路径与片段；查询参数不作为错误原因，也不反射其中的任意内容。
const currentLocation = computed(() => route.path + route.hash);

function back() {
  // 不依据浏览器总 history.length 离开应用；只有已知本标签路由位置才后退。
  const { back: previous, position } = router.options.history.state;
  if (
    typeof position === 'number' &&
    Number.isInteger(position) &&
    position > 0 &&
    typeof previous === 'string' &&
    previous.startsWith('/') &&
    !previous.startsWith('//')
  ) {
    router.back();
  } else {
    void router.push('/quant/demo-preview');
  }
}

function retry() {
  // 仅重新加载原页面，不重发业务命令，不改写原地址或读取错误参数。
  router.go(0);
}
</script>

<template>
  <main class="system-state" aria-labelledby="system-state-title">
    <section class="state-panel">
      <header class="state-header">
        <span>量化交易终端</span>
        <span class="state-code">{{ state.code }}</span>
      </header>
      <div class="state-content">
        <h1 id="system-state-title">{{ state.title }}</h1>
        <p class="state-reason">{{ state.reason }}</p>
        <dl class="state-location">
          <dt>当前地址</dt>
          <dd data-current-location>{{ currentLocation }}</dd>
        </dl>
        <p class="state-next">{{ state.next }}</p>
        <nav class="state-actions" aria-label="页面恢复操作">
          <button
            v-if="canRetry"
            type="button"
            data-action="retry"
            @click="retry"
          >
            重试当前页面
          </button>
          <button v-else type="button" data-action="back" @click="back">
            返回上一页
          </button>
          <RouterLink to="/quant/demo-preview">返回本地演示</RouterLink>
        </nav>
      </div>
      <footer>本地演示仅展示固定样本，不代表真实账户、订单或风险事实</footer>
    </section>
  </main>
</template>

<style scoped>
.system-state {
  display: grid;
  min-width: 0;
  min-height: 100vh;
  min-height: 100dvh;
  align-items: center;
  padding: 40px 24px;
  background: #0c1b2a;
  color: #e5edf4;
  font-family:
    'IBM Plex Sans', 'Source Han Sans SC', 'Noto Sans SC', sans-serif;
}
.system-state * {
  box-sizing: border-box;
}
.state-panel {
  width: min(100%, 760px);
  min-width: 0;
  margin: 0 auto;
  border: 1px solid #29485c;
  background: #132b3e;
}
.state-header {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  padding: 18px 28px;
  border-bottom: 1px solid #29485c;
  color: #a2b8c9;
  font-size: 13px;
}
.state-code {
  color: #f0c17b;
  font-variant-numeric: tabular-nums;
}
.state-content {
  min-width: 0;
  padding: 36px 28px;
}
h1 {
  margin: 0 0 18px;
  font-size: clamp(24px, 3vw, 32px);
  font-weight: 600;
  line-height: 1.4;
}
p {
  margin: 0;
}
.state-reason,
.state-next {
  max-width: 60ch;
  font-size: 14px;
  line-height: 1.8;
}
.state-next {
  color: #a2b8c9;
}
.state-location {
  margin: 24px 0;
  border-left: 2px solid #69b4e4;
  padding-left: 14px;
}
dt {
  margin-bottom: 7px;
  color: #a2b8c9;
  font-size: 12px;
}
dd {
  min-width: 0;
  margin: 0;
  overflow-wrap: anywhere;
  color: #e5edf4;
  font:
    12px/1.7 'IBM Plex Mono',
    'SFMono-Regular',
    monospace;
}
.state-actions {
  display: flex;
  flex-wrap: wrap;
  gap: 12px;
  margin-top: 28px;
  align-items: center;
}
button,
a {
  display: inline-flex;
  min-height: 42px;
  align-items: center;
  justify-content: center;
  border: 1px solid #29485c;
  border-radius: 4px;
  padding: 9px 16px;
  font: inherit;
  font-size: 13px;
  line-height: 1.5;
  text-decoration: none;
  cursor: pointer;
}
button {
  color: #0c1b2a;
  background: #69b4e4;
  border-color: #69b4e4;
}
a {
  color: #69b4e4;
  background: transparent;
}
button:hover,
a:hover {
  border-color: #a2d1ef;
}
button:focus-visible,
a:focus-visible {
  outline: 2px solid #e5edf4;
  outline-offset: 3px;
}
footer {
  border-top: 1px solid #29485c;
  padding: 14px 28px;
  color: #a2b8c9;
  font-size: 12px;
  line-height: 1.6;
}
@media (max-width: 600px) {
  .system-state {
    padding: 24px 12px;
  }
  .state-header {
    padding: 14px 18px;
  }
  .state-content {
    padding: 26px 18px;
  }
  footer {
    padding: 14px 18px;
  }
  .state-actions {
    align-items: stretch;
    flex-direction: column;
  }
}
@media (prefers-reduced-motion: reduce) {
  button,
  a {
    transition: none;
  }
}
</style>
