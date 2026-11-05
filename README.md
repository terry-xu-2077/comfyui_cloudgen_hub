# ComfyUI 云端生成：小云雀 + LibTV

一组 ComfyUI 节点，把 **小云雀（xyq.jianying.com）** 和 **LibTV（mcp.liblib.tv）** 的图片 / 视频生成接进工作流，并在侧边栏显示两边的剩余积分。

## 节点（分类：☁️ 云端生成）

| 节点 | 输出 | 说明 |
| --- | --- | --- |
| 小云雀 · 图片生成 | IMAGE, info | 模型 / 比例 / 分辨率 / 质量（effort）/ 张数，可接参考图 |
| 小云雀 · 视频生成 | VIDEO, video_path, info | 模型 / 模式（文生、参考、首尾帧、延长、编辑）/ 比例 / 分辨率 / 时长 / seed，可接首帧、尾帧、参考图、参考视频、音频 |
| LibTV · 图片生成 | IMAGE, info | 模型 / 质量（自动·高·中·低）/ 宽高比（含 2k、4k）/ W×H / 张数，可接参考图或参考图 URL |
| LibTV · 视频生成 | VIDEO, video_path, info | 模型 / 比例 / 分辨率 / 时长 / 音频 / seed，可接首帧、尾帧、参考图 |
| Lovart · 图片生成 | IMAGE, info | 模型（默认=Lovart 默认模型）/ 宽高比 / 张数，可接参考图；模型专属参数用 extra_json |
| Lovart · 视频生成 | VIDEO, video_path, info | 模型 / 比例 / 时长，可接首帧、尾帧、参考图（视频模型多数需要 Lovart 会员） |
| 云端积分 · 小云雀 / LibTV / Lovart | STRING | 在节点上直接显示两边的余额和到期时间 |

- 模型下拉框和标题栏右上角的 **⚡积分** 都从服务端实时读取。切换模型后，比例、分辨率、时长等选项会跟着换成该模型支持的值。
- 每次运行完，节点下方会显示 **本次实际消耗**（运行前后余额相减）和剩余积分。
- 结果同时保存到 `output/cloudgen/`。
- 节点的输入不变时，ComfyUI 会直接用缓存，不会重复扣积分。要重新生成，改一下 `seed`。

## 侧边栏「云端积分」（钱包图标）

- 两边的余额和到期时间，可以手动刷新
- 小云雀 Access Key 和团队 ID 设置
- LibTV 授权（浏览器里微信扫码）/ 退出 / 导出工具清单
- 各模型的单次积分表

## 配置

**小云雀**：先登录 xyq.jianying.com，在顶部【CLI/API】→【API】里新建秘钥（Access Key），再填进侧边栏。也可以设置环境变量 `XYQ_ACCESS_KEY`。要用团队空间的积分，就填团队 ID。

**Lovart**：点侧边栏「⚙ 配置 → 账户」里的「Lovart 授权」，在浏览器登录并同意即可。OAuth 2.1 + PKCE，使用预注册客户端 `codex-lovart`，回调地址 `127.0.0.1:8766`（占用时在 `config.json` 加 `"lovart_oauth_port"`）。token 自动续期，凭据存在 `lovart_credentials.json`（不入库）。模型参数 schema 可访问 `/cloudgen/lovart/describe?model=模型名` 查看。

**LibTV**：点侧边栏的「LibTV 授权」，浏览器会打开 LibTV 授权页，微信扫码即可。走的是 OAuth 2.1 + PKCE，回调地址 `127.0.0.1:8765`。token 会自动续期。如果端口被占用，在 `config.json` 里加 `"libtv_oauth_port": 8766`。如果这台机器上已经装过 dsh-libtv（`~/.libtv/credentials.json`），会直接复用它的授权。

凭据保存在本包目录的 `config.json` 和 `libtv_credentials.json`，已写进 `.gitignore`。

## LibTV 参数映射说明

LibTV 的工具参数由服务端 `tools/list` 下发。节点按工具的 inputSchema 自动对应 prompt、model、比例、质量、张数、参考图等字段。遇到对应不上的字段：

1. 在侧边栏点「导出工具清单」，打开本包目录下的 `libtv_tools.json` 查看真实字段；
2. 把需要的字段写进节点的 `extra_json`，例如 `{"params": {"style": "cinematic"}}`。它会合并进最终参数，并覆盖同名字段。

如果某个工具要求 `project_id`，节点会自动建一个名为「ComfyUI 生成」的 LibTV 项目，之后一直复用。也可以在 `config.json` 里用 `libtv_project_id` 指定项目。

## 接口来源

- 小云雀：照搬官方 `pippit-tool-cli`（github.com/Pippit-dev/cli）的 `/api/biz/v1/skill/*` 接口与请求体
- LibTV：Remote MCP `https://mcp.liblib.tv/mcp`，授权流程参考 dsh-libtv


> 侧边栏只显示已配置的平台（小云雀有 Access Key、LibTV / Lovart 已授权）；未配置的不会出现在积分卡片、套餐和积分价格页里。
