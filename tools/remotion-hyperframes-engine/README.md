# 本地信息动效引擎

这是 Auto Video Engine 的本地 Remotion + HyperFrames 风格模板工程。

## 路径

- 工程目录：`D:/素材/自动生成/_engine/remotion-hyperframes`
- 输出目录：`D:/素材/自动生成/信息动效`

## 命令

```powershell
cd /d D:\素材\自动生成\_engine\remotion-hyperframes
cmd /c npm run studio
cmd /c npm run render:test
```

## 说明

`HyperFrames by HeyGen` 是 Codex 插件，不是可下载安装到本机的公开包。本工程使用 Remotion 做本地渲染，并在 `src/hyperframes` 里维护可复用的信息动效模板。

第一版模板包含：

- 数据流背景
- 玻璃拟态信息卡
- 关键词动效
- 信号扩散环

后续 Auto Video Engine 可以把脚本文案、关键词、配色和时长传给这个工程，渲染出 9:16 的信息动效素材。
