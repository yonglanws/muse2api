# muse2api Cookie 导入插件

一个 Manifest V3 Chrome / Edge 插件：读取浏览器里 muse.ai 的登录 Cookie（包括 httpOnly 的），一键导入到你的 muse2api 网关。

## 为什么需要它

muse.ai 的核心 Cookie（`hatch_sess`、`hatch_native_auth_device`，以及存在时的 `hatch_gw` / `hatch_vml`）都是 `httpOnly`，普通网页 JavaScript 读不到，只有带 `cookies` 权限的浏览器插件才能访问。

## 安装（开发者模式）

1. 在 muse2api 控制台的「账号池」页点击 **下载插件**，得到 `muse2api-cookie-extension.zip`，解压。
2. 打开 `chrome://extensions`。
3. 打开右上角的 **开发者模式**。
4. 点击 **加载已解压的扩展程序**，选择解压后的目录。
5. 工具栏出现插件图标即安装完成。

## 使用

1. 在**这个浏览器**里登录 [muse.ai](https://muse.ai/)，直到能看到聊天界面。
2. 点击工具栏上的插件图标。
3. 填写：
   - **网关地址**：muse2api 的地址，例如 `http://your-server:18610`；
   - **Admin Key**：`/admin/*` 的密钥，即 `MUSE2API_ADMIN_KEY`；未设置时使用自动生成的 API Key（`m2a-…`，保存在 `data/api_key`）；
   - **账号标签**（可选）：给这个账号起个名字。
4. 点击 **读取并导入**。

插件会读取 muse.ai 的 Cookie 并 POST 到 `{网关地址}/admin/accounts`，返回结果里会显示新的账号 ID 和过期时间。

## 权限说明

- `cookies`：读取 muse.ai 的 Cookie；
- `storage`：保存网关地址和密钥，**不保存 Cookie**；
- `https://muse.ai/*`、`https://*.muse.ai/*`：读取该域名的 Cookie；
- 可选权限：你填写的网关地址（保存设置时申请，只申请这一个源）。

## Cookie 有效期

存在时，最短命的核心 Cookie 是 `hatch_vml`（约 2 天）；否则是 `hatch_sess`（约 30 天）。控制台账号卡片出现过期提醒时，重新导入一次即可。

## 语言

插件弹窗支持中文、English、Tiếng Việt，可在弹窗右上角切换；默认跟随浏览器语言。
