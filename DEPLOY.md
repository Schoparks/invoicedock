# 票栈源码部署

以下为 Linux 服务器上的通用 Conda、systemd 和 Nginx 部署参考；文中的域名和路径均为示例。真实部署信息保存在 Git 忽略的私有运维记录中，由服务器维护者与 Hermes 交接。

应用保持单进程运行：同一进程提供 HTTP 服务、使用 SQLite 并执行 IMAP 轮询。不要配置多个 Uvicorn/Gunicorn worker、多个服务副本或自动重载进程来访问同一数据目录。

## 1. 放置源码与安装环境

源码统一从 GitHub 获取。首次安装：

```sh
sudo install -d -o "$(id -un)" -g "$(id -gn)" /srv/invoice-desk
git clone https://github.com/Schoparks/invoicedock.git /srv/invoice-desk
```

公开项目名为票栈 · InvoiceDock；下文沿用 `invoice-desk` 作为环境名、服务名和示例目录名，以兼容现有部署。

安装 Conda，并将服务器环境放在服务账号可读取的位置。下文假设 Conda 位于 `/opt/miniconda3`；systemd 模板的 `ProtectHome=true` 会阻止读取 `/home` 下的环境，因此应使用 `/opt` 等位置，并相应修改模板中的 Python 路径。

```sh
cd /srv/invoice-desk
/opt/miniconda3/bin/conda env create -f environment.yml
```

`environment.yml` 指定 Python 3.12，并从 `requirements.lock.txt` 安装锁定的 Python 依赖。已有环境更新依赖：

```sh
/opt/miniconda3/envs/invoice-desk/bin/python -m pip install -r requirements.lock.txt
```

Ubuntu/Debian 的反向代理依赖（已使用 Caddy 的服务器沿用 Caddy）：

```sh
sudo apt update
sudo apt install nginx
```

OCR 使用 requirements.lock.txt 中的 RapidOCR 和随包安装的本地模型，无需另装 Tesseract 或在线下载模型。没有可用 OCR 时仍能保留有效原件，识别字段需手工补全。应用本身不需要 Node.js 或前端构建步骤。

## 2. 服务账号、数据目录与配置

创建专用服务账号；如已存在则直接复用：

```sh
sudo useradd --system --home /srv/invoice-desk --shell /usr/sbin/nologin invoice
sudo install -d -o invoice -g invoice -m 700 /srv/invoice-desk/data
cd /srv/invoice-desk
sudo cp .env.example .env
sudo chown invoice:invoice .env
sudo chmod 600 .env
sudoedit .env
```

源码及 Conda 环境需允许 `invoice` 账号读取和执行；运行数据写入 `data/`。`run.py` 和 `manage.py` 会读取项目中的 `.env`，无需在 shell 中手动导出它。已存在的进程环境变量优先于 `.env`。

生产配置示例：

```dotenv
INVOICE_MODE=production
INVOICE_ALLOWED_HOSTS=invoices.example.com,127.0.0.1,localhost
COOKIE_SECURE=true
ALLOW_REGISTRATION=false
INVOICE_DATA_DIR=/srv/invoice-desk/data
PORT=8765
INVOICE_MAIL_WORKER=1
INVOICE_MAIL_POLL_SECONDS=60
CLAIM_LEGACY_DATA=false
```

| 变量 | 含义 |
| --- | --- |
| `INVOICE_ALLOWED_HOSTS` | 允许的主机名，逗号分隔；填写真实域名，不含协议或端口 |
| `COOKIE_SECURE` | HTTPS 线上服务设为 `true`；本地纯 HTTP 使用 `false` |
| `ALLOW_REGISTRATION` | `false` 关闭公开注册，使用维护命令创建账号；程序未配置时默认 `true` |
| `INVOICE_DATA_DIR` | 数据目录绝对路径，需与 systemd 的 `ReadWritePaths` 一致 |
| `PORT` | 回环地址监听端口，需与 Nginx `proxy_pass` 一致 |
| `INVOICE_MAIL_WORKER` | `1` 启用后台收票，`0` 关闭；关闭后仍可在页面手动收取 |
| `INVOICE_MAIL_POLL_SECONDS` | 每轮收取结束后的等待秒数，允许 10–60，默认 60 |
| `INVOICE_SECRET_KEY` | 可选 Fernet 密钥；未设置时使用数据目录中的 `secret.key` |
| `INVOICE_MODE` | 页面区分本地/线上模式，不代替鉴权、HTTPS 或主机配置 |

默认密钥方式无需手动配置：首次启动会生成权限为 `600` 的 `data/secret.key`，用于加密邮箱授权码。该文件是恢复邮箱配置所必需的。

如确需通过环境变量提供密钥，可在**首次启动且尚未绑定邮箱时**执行以下命令，将生成的密钥直接追加到受保护的 `.env`，不会输出到终端：

```sh
sudo -u invoice /opt/miniconda3/envs/invoice-desk/bin/python -c 'from pathlib import Path; from cryptography.fernet import Fernet; p = Path("/srv/invoice-desk/.env"); p.write_text(p.read_text() + "\nINVOICE_SECRET_KEY=" + Fernet.generate_key().decode() + "\n")'
```

此操作只执行一次。已经绑定邮箱后不要用新密钥替换旧密钥；现有授权码会无法解密，服务会拒绝启动。使用外部环境或密钥管理器提供密钥时，也必须独立保留同一原密钥。

## 3. 创建账号与找回密码

公开注册默认在生产示例中关闭。维护者在服务器上创建账号，密码通过交互输入，不放入命令行参数：

```sh
sudo -u invoice /opt/miniconda3/envs/invoice-desk/bin/python /srv/invoice-desk/manage.py create-user person@example.com --name '使用者'
```

密码长度为 10–128 个字符。创建的都是普通账号，每个账号都可以创建组；不存在拥有全站票库访问权的应用管理员角色。组邀请码仅用于已有账号加入组。

账号忘记密码时：

```sh
sudo -u invoice /opt/miniconda3/envs/invoice-desk/bin/python /srv/invoice-desk/manage.py reset-password person@example.com
```

重置会撤销该账号的全部现有登录会话。当前没有邮件验证或邮件找回密码功能。

旧版本地数据库会升级表结构，但未归属数据不会自动分给第一个线上账号。`CLAIM_LEGACY_DATA` 保持 `false`。确有旧资料需认领时，应先备份并由维护者安排显式迁移：当前仅支持首个账号在同时满足显式启用该变量、回环客户端、localhost/回环主机名的本地注册中认领；CLI 创建账号不认领旧数据。不要在公网开放这个认领开关。新空库不需要此步骤。

## 4. systemd 单进程服务

编辑 `deploy/invoice-desk.service`，核对：

- `User`、`Group` 为上面创建的服务账号。
- `WorkingDirectory` 与 `ExecStart` 的源码路径正确。
- `ExecStart` 使用实际 Conda 环境内的 Python，不依赖交互式 `conda activate`。
- `ReadWritePaths` 与 `INVOICE_DATA_DIR` 相同；目录必须已创建。

安装并启动：

```sh
sudo cp deploy/invoice-desk.service /etc/systemd/system/invoice-desk.service
sudo systemctl daemon-reload
sudo systemctl enable --now invoice-desk
sudo systemctl status invoice-desk
sudo journalctl -u invoice-desk -n 80 --no-pager
```

`run.py` 始终只监听 `127.0.0.1`，由 Nginx 对外提供 HTTPS。可先在服务器只读检查服务状态：

```sh
curl http://127.0.0.1:8765/api/auth/status
```

未登录时预期返回 `user: null`，关闭注册时 `registration_open: false`。生产配置下登录应通过 HTTPS 域名进行，Secure Cookie 不适用于这个 HTTP 检查地址。

## 5. Nginx 与 HTTPS

先将域名解析到服务器并准备对应 TLS 证书。修改 `deploy/nginx.conf` 中的：

- `invoices.example.com`：替换为真实域名，与 `INVOICE_ALLOWED_HOSTS` 一致。
- `ssl_certificate` 和 `ssl_certificate_key`：替换为实际证书文件。
- `proxy_pass`：与 `PORT` 一致。

Ubuntu/Debian 的启用示例：

```sh
sudo cp deploy/nginx.conf /etc/nginx/sites-available/invoice-desk
sudo ln -s /etc/nginx/sites-available/invoice-desk /etc/nginx/sites-enabled/invoice-desk
sudo nginx -t
sudo systemctl reload nginx
```

如已有同名站点配置或软链接，更新现有项即可。模板将 HTTP 重定向到 HTTPS，允许 110 MB 请求，并为批量 OCR 设置较长代理等待时间。

保留模板的 `Host`、`X-Forwarded-For`、`X-Forwarded-Proto` 转发设置。应用仅信任来自回环代理的转发头，限速需要真实客户端地址。对外提供 80/443 即可，应用的 8765 端口保持回环监听。

打开你的 HTTPS 域名，用维护命令创建的账号登录。首次确认登录、上传一张样本并人工核对、导出，再按需创建组。

## 6. 邮箱收票配置

本工具绑定已有邮箱，不创建新邮箱地址，也不代发邮件。在邮箱服务商处启用 IMAP，申请应用专用密码或授权码，然后在页面“邮箱收票”中配置：

- IMAP 主机：服务商给出的公网域名，只支持 TLS 993，验证服务器证书。
- 登录账号与授权码：通常账号为完整邮箱地址；不支持 OAuth 登录。
- 收件文件夹：默认 `INBOX`，可填写实际文件夹名称。
- 保存范围：个人空间，或自己担任创建者/管理员的组。绑定后不能直接更换范围，需移除后重新绑定。

应用拒绝连接内网、回环或保留地址的 IMAP 服务。服务器需能访问服务商的公网 993 端口。服务商的授权码规则、IMAP 开关和连接限制需按其实际设置确认。

首次绑定会验证连接并记录当前邮件游标，只处理此后新收到的邮件。改变邮箱主机、登录账号或文件夹会重新从当时的新邮件开始；服务商重置 UID 时也会提示并跳过旧邮件，历史附件应手动导入。

收取使用只读文件夹与 `BODY.PEEK`，不删除邮件，不改变未读标记。只处理支持类型的命名附件，不抓取正文链接。单封完整邮件最多 20 MB；Base64 等编码会增加体积，因此附件总大小应小于这个限额。每轮最多处理 50 封，每封最多展开 200 个发票文件。

页面可立即收票、查看最近 50 条收票结果、重试失败邮件。失败附件不阻塞后续邮件，重试会跳过已入库附件；超限文件应拆分后重新发送。组邮箱只由组创建者/管理员管理，发票归最初绑定人并自动分享到该组。移除邮箱配置不会删除已经入库的票据。

## 备份、恢复与升级

页面下载的“个人数据备份”仅含当前账号的个人 JSON、Excel 和原件，不含整站数据库、其他账号、组报销单或邮箱凭据，也不能通过页面自动恢复。

整站恢复需要同一时间点的完整数据目录、邮箱加密密钥及部署配置。默认路径的维护备份示例：

```sh
sudo systemctl stop invoice-desk
sudo install -d -m 700 /srv/invoice-desk-backups
sudo sh -c 'umask 077; tar -czf "/srv/invoice-desk-backups/$(date +%Y%m%d-%H%M%S).tar.gz" -C /srv/invoice-desk data .env'
sudo systemctl start invoice-desk
```

自定义数据目录时相应修改备份路径。备份包括 SQLite 数据库及可能存在的 WAL/SHM 文件、`originals/` 和 `secret.key`；不要只复制单个正在使用的 SQLite 文件。若密钥只来自外部环境或密钥管理器，以上 `.env` 备份不会包含它，需通过受控方式另存相同密钥。备份与密钥只交由有权维护服务器的人保管，并保存一份对应的源码和锁文件版本。

恢复时：

1. 停止服务，先把当前完整数据目录另存，不覆盖唯一副本。
2. 将备份中的完整数据目录恢复到空目标目录，同时恢复对应的配置与原密钥；不要混入其他时间点的 WAL/SHM 文件。
3. 确认目录归服务账号所有，`secret.key`、`.env` 仅该账号可读；路径与 systemd 配置一致。
4. 启动服务，查看日志，核对登录、票据数量与原件、报销历史、邮箱配置。缺失或不匹配的邮箱密钥应从备份恢复，不能重新生成代替。

### 通过 Git 更新

GitHub `main` 为后续源码更新入口。不要再上传散文件覆盖服务器源码。现有 MCSManager、Caddy 等部署由 Hermes 核对实际路径和托管方式后操作，不直接套用 systemd 命令。

1. 检查 `git status --short` 与 `git remote -v`，确认源码仓库和工作区。若有改动先交由维护者处理，不强制重置。
2. 记录 `git rev-parse HEAD`，按上面的步骤停机并备份完整数据、原密钥和配置。建议将运行数据与私密配置放在源码目录外，通过 `INVOICE_DATA_DIR` 指向实际数据目录。
3. 在服务已停止且备份完成后更新：

   ```sh
   cd /srv/invoice-desk
   INVOICEDOCK_PYTHON=/opt/miniconda3/envs/invoice-desk/bin/python ./update.sh
   ```

   更新脚本要求 main 分支及干净工作区，内部执行 `git pull --ff-only origin main`；锁文件变化或依赖缺失/版本不符时才安装依赖。Shell 与 Windows 入口共用 `update.py`，不自动停止或启动服务，不覆盖运行配置和数据。

4. 通过现有托管器启动，检查提交号、单进程状态、`/api/auth/status`、登录与原件访问。数据库迁移在启动时执行。
5. 失败时保留日志，使用匹配的源码版本与完整备份恢复，不删除数据表或重新生成邮箱密钥。

首次从非 Git 目录切换时，先在独立目录 clone 仓库，再连接现有数据和私密配置；确认启动路径后由托管器切换，保留原目录供回退。不要将真实 `.env`、数据库或部署记录导入 Git 历史。

拉取仓库不会更新已发布的 Cloudflare Worker。仓库中的 `deploy/wrangler.toml` 是公共模板：发布前使用忽略目录内的独立配置填写真实入口和账户信息，保持原 Worker 名称与 Secret。Python 服务和 Worker 共用的 HMAC 密钥必须一致；不要用模板覆盖生产入口。

## 验证与已知范围

在源码目录运行：

```sh
/opt/miniconda3/envs/invoice-desk/bin/python -m unittest discover -s tests -v
node --check static/app.js
node --test tests/test_email_worker.mjs
```

Node.js 仅在前端语法与 Worker 检查时需要。自动化测试使用临时票库和模拟 IMAP；本地还使用合成 PDF/图像检验解析和 OCR 路径。这些验证不等同于真实邮箱服务商、你的真实票面、线上 TLS 与反向代理配置的联调，也不构成吞吐量或可用性承诺。

若只运行本地版本，直接按 [README](README.md) 启动即可，不需要 systemd、Nginx 或域名。


## 自有域名收票

`INVOICE_RECEIPT_DOMAIN=receipts.example.com` 与服务端私密的 `INVOICE_INBOUND_SECRET`（至少32字符随机值）启用域名地址。登录后可创建个人或管理员所在群组的地址，地址停用后保留归属，避免历史邮件误投。仅创建地址不会自动修改 DNS。

可信上游 POST 原始 RFC822 MIME 至 `/api/inbound/email`，最大20MB；以 SMTP envelope RCPT TO 决定归属，不能使用 To/Cc 等邮件头。请求签名格式见 `deploy/submit_email.py` 与 `deploy/cloudflare-email-worker.mjs`。此精确入口使用 HMAC 与5分钟时间窗鉴权；其余接口仍需登录。服务器持久化原文和任务后返回202，单进程后台识别。重启恢复未完成任务，失败附件可在收票记录中重试。成功原文清理，发票附件原件保留。

Cloudflare 路径需用户授权真实邮件经过 Cloudflare，并在管理账户配置：

1. `invoices.example.com` 指向现有 HTTPS 入口，反代至单进程应用。
2. 部署 `deploy/cloudflare-email-worker.mjs`。URL 配置见 `deploy/wrangler.toml`。服务密钥作为 Worker secret 配置，禁止写入源码。
3. Email Routing 配置该域名 MX，并将 catch-all 路由至 Worker。之后个人或群组只需在票栈中创建地址，无需逐个修改 Cloudflare。应用会拒绝未知或停用的地址。不要覆盖已有邮件服务的 MX。
4. Worker 直接投递至服务器，服务器确认原文与任务持久保存后才成功。服务器不可达或未确认保存时，Worker 会明确拒收，提示发件人稍后重发；没有云端离线补投队列。服务器已接收的邮件仍支持后台识别、失败重试和重启恢复。

本方案不使用 R2，不开通对象存储订阅。未提供或未登录 Cloudflare 管理入口时，不能完成公网域名收票验证。测试只能证明被实际执行的链路；本地签名测试不能替代外部发件至 MX 的端到端验收。
