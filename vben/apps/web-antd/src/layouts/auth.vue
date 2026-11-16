<script lang="ts" setup>
import { RouterView } from 'vue-router';
</script>

<template>
  <main class="workstation-access">
    <aside class="access-brief" aria-label="终端访问范围">
      <p class="terminal-name">量化交易工作站</p>
      <h2>账户风险、研究记录与运行证据</h2>
      <p class="access-boundary">
        登录身份不代表交易账户。进入后请核对环境、账户与策略版本。
      </p>
      <dl>
        <div>
          <dt>交易环境</dt>
          <dd>由账户事实核验，登录页不推断</dd>
        </div>
        <div>
          <dt>交易命令</dt>
          <dd>依据账户权限与风险批准执行</dd>
        </div>
        <div>
          <dt>时间基准</dt>
          <dd>北京时间交易日，记录保留原始时间</dd>
        </div>
      </dl>
      <p class="access-scope">
        查看研究与证据不代表策略已经通过验收或获准上线。
      </p>
    </aside>
    <div class="access-form-region">
      <div class="access-form-workspace"><RouterView /></div>
      <footer>本机认证服务 · 凭据状态以服务端响应为准</footer>
    </div>
  </main>
</template>

<style scoped>
.workstation-access {
  display: grid;
  grid-template-columns: minmax(320px, 0.9fr) minmax(0, 1.1fr);
  min-height: 100dvh;
  background: #0c1b2a;
  color: #e5edf4;
  font-family:
    'IBM Plex Sans', 'Noto Sans SC', 'Source Han Sans SC', sans-serif;
}
.workstation-access * {
  box-sizing: border-box;
}
h2,
p,
dl,
dd {
  margin: 0;
}
.access-brief {
  display: flex;
  flex-direction: column;
  justify-content: center;
  min-width: 0;
  padding: clamp(32px, 6vw, 100px);
  border-right: 1px solid #29485c;
  background: #132b3e;
}
.terminal-name {
  color: #69b4e4;
  font-size: 14px;
}
h2 {
  margin: 15px 0 24px;
  max-width: 15em;
  font-size: clamp(25px, 3vw, 38px);
  font-weight: 500;
  line-height: 1.5;
}
.access-boundary {
  max-width: 32em;
  color: #e5edf4;
  font-size: 14px;
  line-height: 1.9;
}
dl {
  display: grid;
  gap: 17px;
  margin-top: 36px;
  padding-top: 24px;
  border-top: 1px solid #29485c;
  font-size: 12px;
  line-height: 1.7;
}
dl div {
  display: grid;
  grid-template-columns: 68px minmax(0, 1fr);
  gap: 16px;
}
dt {
  color: #69b4e4;
}
dd {
  color: #a2b8c9;
}
.access-scope {
  margin-top: 30px;
  color: #a2b8c9;
  font-size: 12px;
  line-height: 1.8;
  max-width: 32em;
}
.access-form-region {
  min-width: 0;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  padding: 48px clamp(28px, 6vw, 100px);
  gap: 44px;
}
.access-form-workspace {
  width: 100%;
  max-width: 420px;
}
footer {
  width: 100%;
  max-width: 420px;
  border-top: 1px solid #29485c;
  padding-top: 18px;
  color: #a2b8c9;
  font-size: 11px;
  line-height: 1.7;
}
@media (max-width: 900px) {
  .workstation-access {
    grid-template-columns: minmax(260px, 0.8fr) minmax(0, 1.2fr);
  }
  .access-brief {
    padding: 36px 28px;
  }
  .access-form-region {
    padding: 40px 28px;
  }
}
@media (max-width: 650px) {
  .workstation-access {
    display: block;
  }
  .access-brief {
    display: block;
    padding: 24px;
    border-right: none;
    border-bottom: 1px solid #29485c;
  }
  .access-brief dl,
  .access-scope {
    display: none;
  }
  .access-brief h2 {
    margin: 8px 0 10px;
    font-size: 20px;
    max-width: none;
  }
  .access-boundary {
    font-size: 12px;
  }
  .access-form-region {
    padding: 32px 24px;
    gap: 30px;
  }
}
@media (prefers-reduced-motion: reduce) {
  * {
    scroll-behavior: auto;
  }
}
</style>
