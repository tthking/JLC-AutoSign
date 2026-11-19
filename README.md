# 📬 jlc自动签到脚本

使用此 Python 脚本可以自动为多个 jlc 账号完成每日签到任务，并通过 [Bark](https://github.com/Finb/Bark) 推送签到结果与异常告警至 Apple 设备。

## ✨ 项目功能

* ✅ 自动签到获取每日金豆（支持多账号与网络异常自动重试）
* 🎁 **第 7 天按优先级自动选领**：默认优先领取 **纸盒券 → FPC 券 → 8 金豆兜底**（自动识别本月上限 `isCanExchange`，遇到券列表异常/改名/领券失败自动兜底金豆并推送告警详情）
* 🔍 精确识别 Token 失效（HTTP 401/未登录），触发 **Bark 失效告警** 并以非零状态码退出（避免 GitHub Actions 静默假成功）
* 📊 支持将多个账号按 `SendKey` 分组推送，也支持只填 **1 个** `SendKey` 接收全部账号汇总
* ⏱️ 内置月度保活工作流（`keepalive.yml`），防止公开仓库因 60 天无提交导致 GitHub Actions 定时任务被自动停用
* 📲 支持手动触发时开启 `notify_always` 强制推送当前签到状态，方便排查测试

## 🔧 使用前准备

### 1️⃣ 获取 AccessToken

> 💡 **Mac 一键自动提取并同步（推荐）**：直接双击仓库内的 `一键更新JLC_Token.command`，会自动弹出独立的 Edge 手机视图窗口打开 `https://m.jlc.com`。登录后脚本会自动提取 `X-JLC-AccessToken`、校验有效性、加密写入本仓库 GitHub Secrets 的 `TOKEN_LIST`，并触发一次云端验证。

手动抓包方式（备用）：

> ⚠️ **注意**：当前直接用电脑浏览器打开 `https://m.jlc.com` 会自动 302 跳转到 PC 官网，请务必先切到手机模式。

1. 打开浏览器无痕窗口，按 `F12` 打开开发者工具，并按 `Ctrl+Shift+M`（macOS 为 `Cmd+Option+M`）切换为**手机模拟视图**
2. 访问 [https://m.jlc.com](https://m.jlc.com) 并登录你的嘉立创账号
3. 在「Network（网络）」面板中筛选 `m.jlc.com` 的任意接口请求（如 `selectPersonalAssetsInfo`）
4. 查看请求头 **Request Headers** 中的 `X-JLC-AccessToken`，复制其值
5. 多个账号用英文逗号 `,` 分隔（抓包后请勿在网页端点击“退出登录”，否则 Token 会立即作废）

### 2️⃣ 获取 SendKey（Bark）

1. 在 App Store 中搜索并下载 **Bark** 应用
2. 打开应用后，获取默认服务器地址后面的 Key（例如 `https://api.day.app/your_key`，则 `SendKey` 取 `your_key`）
3. 也可以直接填入完整的推送链接（如自建 Bark：`https://你的域名/your_key`）
4. 多个账号可用英文逗号 `,` 分隔与 `TOKEN_LIST` 一一对应；若所有账号共用同一个 Bark 通知，仅填写 **1 个** `SendKey` 即可

## ⚙️ 配置说明

### GitHub Actions 配置

1. 进入仓库 **Settings → Secrets and variables → Actions**
2. 添加或更新以下 Secrets：

| 变量名                 | 示例值          | 说明                                                               |
| ---------------------- | --------------- | ------------------------------------------------------------------ |
| `TOKEN_LIST`           | `token1,token2` | 多个 token 用英文逗号分隔                                          |
| `SEND_KEY_LIST`        | `key1,key2`     | 与 token 一一对应，或只填 1 个 key 供全部账号共用                  |
| `SEVENTH_DAY_PRIORITY` | `纸盒,FPC,金豆` | （可选）第 7 天领取顺序，未设置时默认 `纸盒,FPC,金豆`              |

3. 确认仓库 **Settings → Actions → General → Workflow permissions** 允许工作流写入（或已配置 `permissions: contents: write`），以便月度保活工作流自动维持定时任务激活状态。
4. 如需清理旧运行记录，可在仓库 **Actions → Cleanup Old Workflow Runs** 中确认工作流处于启用（Enable）状态。

### 本地运行与测试

```bash
pip install -r requirements.txt
export TOKEN_LIST="your_token1,your_token2"
export SEND_KEY_LIST="your_key1"
python main.py
```

运行单元测试：

```bash
python -m unittest -v test_main.py
```

## 📋 执行流程

每日定时触发两次（避开整点队列高峰：**北京时间 08:23 主签到 + 16:23 兜底补签**）：

1. 校验 `AccessToken` 有效性并查询当前金豆余额
2. 预检当日签到与领奖状态，执行每日签到或第七天奖励领取
3. 按 `SendKey` 分组推送（含 Bark 瞬态失败自动重试）：
   * 领到金豆：推送今日获取与当前总数
   * Token 失效或请求异常：推送异常告警，并以退出码 `1` 标记工作流失败
   * 今日已签到且无异常：默认静默跳过（因此下午兜底运行不会重复打扰）；手动触发勾选 `notify_always` 时推送当前状态

## 📄 License

本项目遵循 MIT License，欢迎自由使用和修改。
