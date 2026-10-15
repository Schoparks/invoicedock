# Agent 工作约定

本文件是所有后续 agent 的必读入口。每次开始工作先阅读；完成工作前按实际结果同步维护本文件、README 和受影响的部署说明。

## 文档维护原则

- **只保留最终态**：直接修订现有条目，删除被取代或失效的内容，不累积对话记录、排查流水、临时方案或重复的完成清单。
- 代码、依赖、命令、权限、数据格式或部署方式改变时，同步修改对应文档。未执行或未验证的内容须明确标注，不把推测写成事实。
- 公开文档仅放可复用的项目说明；真实环境的部署状态、机器路径和交接详情保存在忽略的 `.deploy/operations/`，并交接给 Hermes。不要把私有资料复制进本文件。
- 不覆盖其他 agent 或用户尚未完成的改动；先检查 Git 状态和差异。

## 当前项目

- 名称：**票栈 · InvoiceDock**。个人与群组发票管理，支持本地运行及自行部署。
- 后端：Python 3.12、FastAPI、SQLite；前端：`static/` 中的原生 HTML/CSS/JavaScript，无构建步骤。
- 环境管理沿用 Conda：`environment.yml`，环境名 `invoice-desk`。运行依赖由 `requirements.lock.txt` 锁定，直接依赖声明在 `requirements.in`。`conda-osx-arm64.lock.txt` 仅用于 Apple Silicon 的 Conda 基础包复现，不适用于 Linux。
- 品牌名称已更新；兼容现有部署的 `INVOICE_*` 环境变量、Conda 环境名、`InvoiceDesk` 请求头与备份格式标记保持不变。不要因名称调整破坏已有客户端、数据或服务配置。
- 仓库源码公开，**暂未添加许可证**。未经用户决定，不擅自宣称采用 MIT、AGPL 或其他许可证。

## 模块与行为

- `app/main.py`：票库、导入、报销、额度及导出；`db.py`：建表与迁移。
- `auth.py`、`groups.py`、`permissions.py`：账号、群组及授权边界；`parsing.py`、`ocr_worker.py`：本地解析与 OCR；`matching.py`：整数分金额匹配。
- `mailbox.py`：IMAP 新邮件收取；`inbound.py`：签名入站、持久队列与收票地址；`deploy/cloudflare-email-worker.mjs`：Cloudflare 邮件上游。
- 发票所有者、指定群组共享及全局报销占用必须保持隔离。票面提取仅为建议，核对后才允许参与匹配及报销；撤销分享不删除历史报销快照。
- 同账号同票号归并，不清空票号绕过去重；有 PDF 时仅保留 PDF 原件，XML/OFD 仅补全未核对记录的空字段后丢弃。已核对字段不能被自动覆盖。
- 服务保持单进程：HTTP、SQLite、IMAP 与入站队列由同一进程管理；禁止多 worker 或多副本访问同一数据目录。

## 开发与验证

```sh
conda env create -f environment.yml
conda activate invoice-desk
python run.py
```

按变更选择必要检查，不做大量无用测试或冗余兜底。现有检查入口：

```sh
conda run -n invoice-desk python -m unittest discover -s tests -v
node --check static/app.js
node --test tests/test_email_worker.mjs
```

测试只能使用临时数据库、合成票据和示例域名。Node.js 用于开发检查，运行应用不需要它。勿在默认真实数据目录做写入测试；勿将本地检查描述成线上验收。

## 敏感信息与发布

- 不提交真实发票、邮件、数据库、导出、备份、授权码、登录凭据、密钥、`.env` 或实际部署记录。禁止在命令输出、日志、Issue 或提交说明中暴露它们。
- `.env.example`、部署模板及测试只用示例值。真实配置与私密文档留在忽略目录；新增私有文件类型时同步维护 `.gitignore`。
- `.gitignore` 不会保护已跟踪文件。每次公开推送前检查 `git diff --cached`、`git ls-files` 和新增文件内容；必要时同时检查历史。不要把全目录无审查上传当作发布流程。
- 保持现有 Git 作者身份，不擅自修改全局配置。公开仓库：`https://github.com/Schoparks/invoicedock`，主分支 `main`。

## 服务器更新与 Hermes 交接

- GitHub `main` 是后续源码更新入口。服务器从仓库 clone，并使用 `git pull --ff-only origin main` 更新；不再通过散文件覆盖或压缩包同步源码。
- 首次切换及具体路径、托管器、代理和健康检查由 Hermes 对接，在私有运维记录中保留**当前最终态**。先读取实际配置，不假定通用 systemd 模板就是现有线上管理方式。
- `.env`/`app.env`、数据目录和密钥不进入 Git，不被更新覆盖。更新前备份完整数据与配置，记录旧提交；按需要安装锁定依赖，通过现有托管器重启单进程服务并验证。
- Cloudflare Worker 的源码拉取与发布是两个步骤。拉取不会自动发布 Worker；部署时保留服务端对应密钥、真实入口、路由及必要兼容开关，不直接用示例配置覆盖生产设置。
- 失败时保留现场，使用匹配的源码版本与完整备份恢复；禁止用清库、生成新邮箱密钥、强制 Git 重置等方式掩盖问题。
