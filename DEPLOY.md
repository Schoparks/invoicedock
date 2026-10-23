# 票栈源码部署

以下为 Linux 服务器上的通用 Conda、systemd 和 Nginx 部署参考；文中的域名和路径均为示例。请按自己的运行环境修改示例。

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
- 发票归属：个人或自己所在的组。绑定后可编辑归属，下次收取的邮件按新归属入库；已导入发票不变。

应用拒绝连接内网、回环或保留地址的 IMAP 服务。服务器需能访问服务商的公网 993 端口。服务商的授权码规则、IMAP 开关和连接限制需按其实际设置确认。

首次绑定会验证连接并记录当前邮件游标，只处理此后新收到的邮件。改变邮箱主机、登录账号或文件夹会重新从当时的新邮件开始；服务商重置 UID 时也会提示并跳过旧邮件，历史附件应手动导入。

收取使用只读文件夹与 `BODY.PEEK`，不删除邮件，不改变未读标记。只处理支持类型的命名附件，不抓取正文链接。单封完整邮件最多 20 MB；Base64 等编码会增加体积，因此附件总大小应小于这个限额。每轮最多处理 50 封，每封最多展开 200 个发票文件。

页面可立即收票、查看最近 50 条收票结果、重试失败邮件。失败附件不阻塞后续邮件，重试会跳过已入库附件；超限文件应拆分后重新发送。个人和组邮箱配置、收票记录及管理操作仅最初绑定人可见，组管理员不能访问他人的邮箱；普通组成员也能绑定归入本组的邮箱。发票归最初绑定人并自动分享到所选组。移除邮箱配置不会删除已经入库的票据。退出或移除成员时，其创建的组邮箱会转回个人空间并暂停；解散时全组邮箱与域名收票地址均按此处理，凭据、记录和已接收队列保留，已接收附件继续入库到原账号。重新启用须由原账号在个人空间操作。

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

GitHub `main` 为源码更新入口。本说明以 systemd 为例，其他运行方式请按实际服务管理方式调整。

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

`INVOICE_RECEIPT_DOMAIN=receipts.example.com` 与服务端私密的 `INVOICE_INBOUND_SECRET`（至少32字符随机值）启用域名地址。登录后可创建个人或自己所在群组的地址，配置和记录仅创建者可见。名称与归属可编辑，域名地址本身保持不变；已接收队列冻结接收时归属。地址删除后隐藏并停收，永久保留原地址防止重新注册，避免历史邮件误投；已有发票和已接收队列保留。仅创建地址不会自动修改 DNS。

可信上游 POST 原始 RFC822 MIME 至 `/api/inbound/email`，最大20MB；以 SMTP envelope RCPT TO 决定归属，不能使用 To/Cc 等邮件头。请求签名格式见 `deploy/submit_email.py` 与 `deploy/cloudflare-email-worker.mjs`。此精确入口使用 HMAC 与5分钟时间窗鉴权；其余接口仍需登录。服务器持久化原文和任务后返回202，单进程后台识别。重启恢复未完成任务，失败附件可在收票记录中重试。处理完成的邮件原文保留至接收后 7 天，后台每轮有界清理到期原文；失败邮件保留供重试，发票附件原件不受此清理影响。原始邮件预览和 `.eml` 下载仅收票地址创建者可用，旧版已清理邮件不可恢复。

### 可选 Cloudflare 邮件上游

Cloudflare 邮件链路独立于票栈应用运行：

```text
发件人 → Cloudflare Email Routing → Worker email() → 私有 R2
Cloudflare Cron → Worker scheduled() → 签名 POST /api/inbound/email
票栈 → 本地持久队列 → 识别入库
```

R2 暂存、定时扫描、传输重试、分页和网络策略全部在 Worker 内完成。应用不拉取 R2、不保存云服务凭据、不同步云端地址、不限制 IPv4/IPv6，也不需要为此更新运行依赖或重启服务。已有签名接收协议保持不变。

Cloudflare 路径会处理和暂存邮件原文，需在管理账户配置：

1. 保持 Workers Free 计划；在 R2 开通页面核对免费额度及超额费用，涉及订阅、账单和条款的确认由账户持有人操作。R2 的免费额度不等于无限免费，不开通付费升级或额外产品。
2. 创建专用私有桶，例如 `invoice-receipt-mail`，通过 `MAIL_BUCKET` 绑定 Worker。禁止公开访问。为 `raw/` 前缀设置上传后 7 天删除的对象生命周期规则，作为原文清理保障；不要对 `queue/` 设置自动删除，以免丢失过期提醒。
3. 使用 `deploy/wrangler.toml` 的独立私有副本，保留既有 Worker 名称与 Secret，设置 `INVOICE_RECEIPT_DOMAIN` 和 HTTPS `INVOICE_INGRESS_URL`。`INVOICE_INBOUND_SECRET` 仅作为 Worker Secret，须与票栈接收端一致；禁止写入源码。设置 `INVOICE_NOTIFY_FROM` 为收票域名下的通知地址，`INVOICE_NOTIFY_TO` 为管理员已验证的外部邮箱，并添加 `EXPIRY_EMAIL` Send Email 绑定。模板用 `destination_address` 在绑定层限制目标；浏览器表单若只提供绑定名称，Worker 仍固定向 `INVOICE_NOTIFY_TO` 发送，不使用来信指定的通知目标。目标邮箱必须在 Email Routing 中验证；不给任意原发件人发信，不升级 Workers Paid。
4. 发布 `deploy/cloudflare-email-worker.mjs`，配置每分钟 Cron `* * * * *`，将域名 MX 和 Email Routing catch-all 指向该 Worker。仅创建票栈地址不会自动配置 DNS；不要覆盖其他邮件服务的 MX。
5. 观察 Worker 事件与 R2 的待投递对象。基础连通性可用示例域名的合成原文和任务验证：接收端在签名校验后返回 404，Worker 保留原文并更新重试次数和时间，不写入票库。模拟满 7 天的同一合成任务可验证原文清理和管理员提醒；发送接口成功不代表收件箱已确认到达。完整 SMTP、成功确认及附件入库联调须使用隔离临时票库和合成票据，不能将基础连通性检查描述成完整收票验收。

### 确认和重试边界

- Worker 以 SMTP 信封收件地址记录归属，最大原始邮件 20 MB，R2 写入成功才确认接收；写入失败仍拒收。
- 邮件按收件地址与原文 SHA-256 生成对象编号。原文保存在 `raw/<id>.eml`；小任务保存在 `queue/<id>.json`，记录 SMTP 信封地址、原文摘要、首次接收时间及重试状态。只有原文与任务都保存成功才确认 SMTP 接收。重复暂存不重置保留期限，投递前再次校验原文完整性，不读取票栈数据库或账号。
- Cron 每分钟扫描一页，最多 3 个任务，持续保存分页游标，避免失败或尚未到重试时间的邮件阻塞后续分页。待投递数量多时处理会延后。
- Worker 每次投递重新生成现有 HMAC 签名，保留原有 5 分钟鉴权窗口。网络超时、HTTP 错误、重定向或无效确认均保留原文；重试间隔由 1 分钟递增，最多 1 小时；首次接收满 7 天后停止投递，转入清理和管理员提醒流程。
- `queue/` 中的 JSON 记录尝试次数、下次重试时间、脱敏状态码与阶段；`state/cursor.json` 保存扫描位置。Worker 日志只输出批次数量或固定错误，不输出邮件原文、地址、密钥或底层异常。
- 只有 HTTP 200/202 且包含有效任务编号与任务状态，才作为本地持久化确认。Worker 先持久保存已送达阶段，再清理原文和队列任务；清理中断后继续清理。确认或阶段保存中断时可能重复投递，接收端已有 `(address_id, raw_hash)` 唯一约束及发票去重，不会重复入库。
- SMTP 接受代表云端保存成功，不代表票栈已经接受该地址。未知/停用地址由原有接收端拒绝，邮件在 7 天保留期内继续重试，`queue/` 记录状态（如 404），到期后通知邮件链路管理员核查；不在应用中增加云端状态或管理入口。
- 本地识别失败由票栈原有收票记录重试，成功附件跳过。Worker 在接收端保存成功后即清理云端副本，因此本地待处理原文仍须随全站数据备份。

### 7 天清理与管理员提醒

Worker 扫描到已满 7 天的待投递任务时，先持久保存 `expired` 阶段，再删除原文，随后通过 `EXPIRY_EMAIL` 向配置的已验证管理员发送提醒。提醒只包含信封发件/收件地址、首次接收时间、编号和最后投递状态，不附原邮件正文或发票。不给原发件人发送退信，也不需要第三方发信服务。

提醒失败时，原文已清理，只保留小任务按退避规则重试；提醒成功后标记 `notified` 并清理任务。发信成功确认或阶段保存中断可能导致重复提醒，不能承诺恰好一次。IPv6 入口或票栈不可用不阻塞到期清理与提醒。

R2 的 `raw/` 7 天生命周期可在 Worker 停止时继续清理原件，`queue/` 中的提醒信息保留，Cron 恢复后仍可发出提醒。生命周期删除由云平台异步执行，Worker 也按分页定时扫描，因此不能承诺恰好第 7 天的某一秒完成物理删除或收到通知；7 天后不再尝试业务投递。队列元数据写入失败时，Worker 会保留原文等待重试，不跳过持久提醒步骤直接删除。

7 天保留期限及成功后自动清理能减少占用，但没有设置总存储量硬上限；短时间大量来信和 API 操作仍可能超过免费额度。R2 按量计费，不应把此清理策略描述为绝不会收费。

### 邮件上游的 IPv6 策略

IPv6 是可选的 Worker 部署策略，不是票栈运行要求。需要避开 FRP 时，为投递配置专用 HTTPS 域名，DNS-only，仅设置直达服务器的 AAAA，不设置 A，也不使用 Cloudflare 代理或 FRP 中转域名。该域名须具有有效证书，并由既有反向代理和应用允许的主机配置接收。

在 Worker 设置 `INVOICE_IPV6_ONLY=true` 后，每次投递批次先通过 DNS-over-HTTPS 检查目标域名存在 AAAA、没有 A；检查失败保留邮件并等待下一轮，不改用 IPv4 入口。TLS 证书验证保持开启，HTTP 重定向不会跟随。此配置依赖实际 DNS、服务器 IPv6 入站和 HTTPS 可用性，需线上联调，不能仅凭 DNS 检查宣称已完成 IPv6 验收。

普通部署保持 `INVOICE_IPV6_ONLY=false`，继续使用正常 HTTPS 入口。主项目的 `.env` 只保留原有域名、签名密钥与本地队列配置，不增加 R2/IPv6 变量。

拉取源码不会自动更新已发布的 Worker、R2 绑定或 Cron，须独立部署；真实入口、账号和私密配置只保存在忽略目录。仓库回归使用临时票库、合成邮件和模拟 R2/网络，不等同于线上 Cloudflare、外部 SMTP 与 IPv6 联调。
