# 旧版前端退役（2026-11-17）

按用户“先把旧版前端全部移除”的要求，当前生产 web-antd 应用只保留
新版交易台、因子、订单流与价格、策略研究、实验任务、验收、数据、运维
工作区，以及新版纸面账户档案、Demo 设置、身份、登录和系统状态页。

## 删除范围

删除旧 overview、positions、market、pairlist、live、logs、ops、data、
backtest、tasks、iteration、research-ledger、detail 十三个页面目录及其
专属状态层和测试；删除 BotControls、TaskProgress 以及孤立的 useFtWs、
coinOrder、logText。应用壳不再导入或提供旧交易引擎命令插槽。

删除 dashboard、demos/antd、about 的九份模板页面；移除模板百度统计和
模板元数据。删除验证码、二维码、注册、忘记密码四条旧兼容路由及五份
包装组件。新版用户名密码登录和本机恢复保留。累计删除 78 个受跟踪文件。

旧量化地址和重复 strategy-focus 别名不再挂载业务组件；已认证访问进入
统一 404。未登录访问继续遵循原访问守卫。新版页面内旧链接和旧产物地址
生成器已移除。历史研究事实仍可由新版查询页面读取；共享 API 和后端不因
前端退役删除，正在运行的纸面研究、数据库和订单事实保留。

共享 @vben 包及非生产示例 app 是 monorepo 依赖/示例，不在此应用的
构建服务链中。旧静态备份只保留于 web 根目录之外，不作为可访问旧界面。

## 实际验证

- 路由和应用壳 57 项通过；新页面五份挂载测试 30 项通过；策略/实验
  相关六文件 57 项通过；认证与系统页 36 项通过。
- 最终完整 Vitest：122 文件、973 项通过；类型检查通过；生产构建
  11/11 成功。测试总数因移除旧组件专属测试减少。
- 真实构建 Playwright：98 项通过，覆盖四视口。14 个旧量化地址在
  1440/390 均进入 404，四个旧认证地址在四视口均无旧界面或额外认证。
  返回新版交易台后仅有八个工作区导航，不挂载旧引擎命令。
- 实际扫描 dist 与本机 web 中的 JS/HTML，旧路由名和 hm.baidu.com
  残留为零；原子替换本机 web，不重启服务。

本机静态目录：`/Users/shiyi/Documents/ChatGPT/量化/web`。
前一目录保留于 `logs/web-backups/20261116T211857Z-2feba282`。
新 index.html SHA-256：
`c0b1b2c4bc3886b06e9e7cea77f75ae51a538af19ba326f503b78bda997793c1`。
实际 GET `http://127.0.0.1:8888/` 返回 200，字节与新 web/index.html
完全一致，Cache-Control 为 no-store；不是只更新未服务的 dist。
浏览器证据单独输出至 `/private/tmp/quant-legacy-removal-full-browser`，
没有覆盖已跟踪截图。

## 正式发布回执

附注标签 v0.2.3 固定于 `42f7ad89cd0c29183bbda39b7c1acd61ce4615ab`。
精确 push CI 43959416690 成功，标签 CD 43960919815 的打包与 Release
job 均成功，GitHub Release 447645971 已发布。远端前端同样为 973 项
单测、11/11 构建和 98 项浏览器验证通过。

独立下载实际 CD 工件，TAR 共 149959962 字节，SHA-256 为
`2d7a65590600336d32f735bf43c82d7faaf3c17d7c484ba03af292a994ee0921`；
sidecar、Release 资产 digest 和 build.json 提交/版本/包名均一致。4015 个
归档成员路径唯一、均在包根内，无软硬链接和特殊类型。再次检查实际包内
85 个生产源码文件与 195 个编译文本文件，旧入口和模板统计检出为零；
78 个已删除文件、退役目录及七份并行后端新文件均未进入本版本。
证据保留于 `/Volumes/yidong/quant-release-audit-v0.2.3-20261117`。

本次界面退役不证明 P1 生产接线、P2 真实研究或自然 30 天验收完成。
