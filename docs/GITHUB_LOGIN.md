# GitHub 登录配置

GitHub 登录接入代码已完成。当前尚未创建 OAuth App，也未配置真实 Client ID / Client Secret，因此登录页显示“GitHub 登录暂不可用”，账号密码登录正常保留。自动化已验证本地流程与模拟 GitHub HTTP 响应；未声称完成真实 GitHub 授权联调。

## 1. 创建专用 OAuth App

在 GitHub 的 Settings → Developer settings → OAuth Apps → New OAuth App 中创建。建议为本项目单独创建应用，不复用已经申请仓库权限的其他应用。步骤参考 [GitHub 官方创建指南](https://docs.github.com/en/apps/oauth-apps/building-oauth-apps/creating-an-oauth-app)。

本机开发填写：

| 配置项 | 值 |
| --- | --- |
| Application name | TraceCase Local |
| Homepage URL | `http://127.0.0.1:8501/` |
| Authorization callback URL | `http://127.0.0.1:8501/auth/github/callback` |

创建后取得 Client ID，并生成 Client Secret。Client Secret 只在本机配置，不发到聊天里、不提交 Git。无需启用 Device Flow。本项目只在授权时临时使用访问令牌获取身份，不长期保存或刷新 GitHub 令牌。

## 2. 配置环境变量并重启

在启动项目的同一终端设置以下环境变量。`.env.example` 仅为说明文件，应用不会自动读取 `.env`：

```bash
export GITHUB_CLIENT_ID='你的 Client ID'
export GITHUB_REDIRECT_URI='http://127.0.0.1:8501/auth/github/callback'
```

为避免 Secret 出现在命令历史中，可在 macOS 默认 zsh 使用隐藏输入：

```zsh
read -rs 'GITHUB_CLIENT_SECRET?GitHub Client Secret: '
export GITHUB_CLIENT_SECRET
```

确认没有正在执行的生成任务后，停止旧服务（启动终端中按 Ctrl+C），再在配置好环境变量的终端启动：

```bash
.venv/bin/python scripts/run_google.py
# 若已另行配置模型环境变量，也可以：
# .venv/bin/python scripts/run_local.py
```

刷新 `http://127.0.0.1:8501/`，登录页会出现可点击的“使用 GitHub 登录”。不能使用个人访问令牌（PAT）代替 Client Secret。

如果使用其他端口／域名，必须同步修改 GitHub 应用中的回调地址和 `GITHUB_REDIRECT_URI`，并从一致的主机名访问。例如不要在 `127.0.0.1` 与 `localhost` 间混用，否则浏览器绑定 Cookie 无法随回调返回。远程回调仅允许 HTTPS；HTTPS 部署仍须正确设置受信代理及 `SDLC_COOKIE_SECURE=1`，不能直接将本地原型当作公网生产服务。

## 3. 首次登录与已有账号绑定

- 新用户在登录页选择“使用 GitHub 登录”，授权成功后自动创建独立个人空间，再选择个人／团队工作。
- 重复登录按 GitHub 不变的数字用户 ID 找回同一个本地账号，GitHub 昵称或登录名变化不会创建另一份空间。
- 已有账号希望保留原项目和团队身份：先用原账号登录，在侧栏选择“登录账号与绑定”，点击“绑定 GitHub”，完成 GitHub 授权。该过程不会迁移项目或改变团队角色。
- 不根据同名或邮箱自动合并账号。已经绑定其他本地账号的 GitHub 身份不能再次绑定；绑定期间原登录失效则拒绝绑定。
- 直接用 GitHub 登录产生的新账号不会自动获得旧账号的数据，也不会绕过团队邀请码／管理员审批。升级前未归属的历史项目仍遵循现有的受控迁移规则。
- GitHub 新建账号没有本地密码，不展示修改密码表单；已绑定的原密码账号仍可使用密码登录。本轮不提供解绑、账号合并或密码恢复，避免移除唯一登录方式。

## 4. 实现与安全边界

授权码流程遵循 [GitHub 官方授权文档](https://docs.github.com/en/apps/oauth-apps/building-oauth-apps/authorizing-oauth-apps)，使用随机 state、S256 PKCE 和十分钟有效的一次性服务端流程记录。回调还需匹配浏览器的 HttpOnly / SameSite=Lax 临时 Cookie，主登录会话继续使用 HttpOnly / SameSite=Strict Cookie。回调成功先提交同源页面再进入工作台，认证令牌不放入页面 URL 或 localStorage。

只使用身份接口返回的 GitHub ID、登录名及显示名称，不申请 `repo`、`user:email` 等扩展 scope；[官方 scope 说明](https://docs.github.com/en/apps/oauth-apps/building-oauth-apps/scopes-for-oauth-apps)区分了公开资料与仓库等权限。每次登录都会重新通过 `/user` 确认身份，而不是信任前端提交的用户 ID。

GitHub Access Token、Refresh Token、Client Secret 和授权码不写入业务记录或数据库。PKCE verifier 只在服务端临时流程中保存，消费后删除，过期记录会在下一次发起时清理。浏览器中仅保存本地不透明会话；退出、改密、停用、空间隔离和成员权限检查继续使用现有机制。

外部请求使用固定 GitHub HTTPS 地址、超时、响应长度限制且不跟随重定向。网络失败、取消授权、state 错误、超时与重复回调显示安全提示，不回显服务商的敏感原始错误。本地会话不会因 GitHub 侧撤销应用而自动立即退出，最长仍受本地十二小时会话有效期约束。

## 5. 验证

```bash
.venv/bin/pytest tests/test_github_auth.py -q
.venv/bin/pytest -q
.venv/bin/ruff check .
```

专项覆盖首次／重复登录、昵称变化、state 与 PKCE、浏览器绑定、过期与重放、取消授权、显式绑定及身份冲突、账号停用、外部失败脱敏、缺失配置，以及旧密码账号的数据库迁移。真实联调需完成前述 OAuth App 配置后再验证。

本轮本地验证：全量 197 项测试通过，GitHub 专项 11 项通过，Ruff 与离线评测输入校验通过。测试替换了 GitHub 网络响应，没有使用真实 Client Secret；真实授权联调待 OAuth App 配置完成后进行。内置启动器关闭访问日志，避免记录 OAuth 回调中的临时授权码；使用其他反向代理时也应避免记录回调查询串。
