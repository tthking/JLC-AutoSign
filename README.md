# 📬 jlc自动签到脚本

使用此 Python 脚本可以自动为多个jlc账号完成每日签到任务，并通过 [Bark](https://github.com/Finb/Bark) 推送签到结果至 Apple 设备。

## ✨ 项目功能

* ✅ 支持 **多个账号并发签到**
* 🎁 自动判断是否为**第七天签到**并领取8个金豆
* 💰 获取并显示当前账号金豆数量
* 📊 将账号按通知 `SendKey` 分组，分组推送签到结果
* 📲 使用 **Bark** 实时推送签到通知

## 🔧 使用前准备

### 1️⃣ 获取 AccessToken

1. 打开 [jlc官网](https://m.jlc.com)
2. 登录账号，按 `F12` 打开浏览器开发者工具
3. 在「Network」中找到任意请求
4. 查看请求头 Header 中的 `X-JLC-AccessToken`，复制其值
5. 多个账号用英文逗号 `,` 分隔
6. 建议使用手机抓包，请勿手动退出账号，否则会导致token失效

### 2️⃣ 获取 SendKey（Bark）

1. 在 App Store 中搜索并下载 **Bark** 应用
2. 打开应用后，获取默认服务器地址后面的 Key（例如 `https://api.day.app/your_key`，则 `SendKey` 取 `your_key`）
3. 也可以直接填入完整的推送链接（如自己部署的Bark：`https://你的域名/your_key`）
4. 多个账号用英文逗号 `,` 分隔，**数量需与 TOKEN\_LIST 一一对应**

## ⚙️ 配置说明

### GitHub Actions 配置

1. Fork 本仓库
2. 进入仓库 Settings → Secrets → Actions
3. 添加以下 Secrets：

| 变量名           | 示例值        | 说明                     |
| --------------- | ------------- | ------------------------ |
| TOKEN\_LIST     | token1,token2 | 多个 token 用英文逗号分隔 |
| SEND\_KEY\_LIST | key1,key2     | 与 token 一一对应         |

### 本地运行配置

```ini
TOKEN_LIST这样填：your_token1,your_token2
SEND_KEY_LIST这样填：your_key1,your_key2
```

## 📋 执行流程

1. 自动签到获取金豆
2. 检查是否为第七天签到
3. 查询当前金豆余额并推送

## ⏱️ GitHub Actions 定时运行

### 创建 `.github/workflows/jlc-signin.yml`

```yaml
name: JLC Auto Sign

on:
  schedule:
    - cron: '0 0 * * *'
  workflow_dispatch:

jobs:
  sign:
    runs-on: ubuntu-latest
    steps:
    - name: Checkout
      uses: actions/checkout@v3

    - name: Run Sign Script
      env:
        TOKEN_LIST: ${{ secrets.TOKEN_LIST }}
        SEND_KEY_LIST: ${{ secrets.SEND_KEY_LIST }}
      run: |
        python main.py
```

## 📄 License

本项目遵循 MIT License，欢迎自由使用和修改。
