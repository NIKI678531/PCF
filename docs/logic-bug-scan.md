# 逻辑风险扫描

GitHub Actions 的 **Logic Bug Scan** 使用内网 LLM 审查当前选定分支的 Python 代码，生成高、中、低三级候选风险。扫描结果供开发者评估，不自动修改代码、创建 issue 或执行应用。

## 运行方式

1. 在仓库的 **Actions → Logic Bug Scan → Run workflow** 中选择分支。
2. `fail_on_high` 默认关闭：发现风险仍允许 workflow 成功，开发者根据报告决定是否修复。开启后，发现保留的高风险候选会使扫描步骤失败。
3. 运行结束后，在运行页面的 **Summary** 阅读中文报告；在 **Artifacts** 下载 `logic-bug-report-运行ID-尝试次数`，包含 `report.md` 和 `report.json`。报告保留 30 天。

报告提供风险等级、模型置信度、固定到扫描 commit 的代码链接、代码证据、触发条件、影响、修复建议和验证方法。候选问题会再经过一次模型复核；被排除的候选和理由也保留在报告中。模型复核不等同于已执行测试。

## 配置

- Runner 标签：`[self-hosted, AWS+UAT_GR_01]`。该 runner 必须在线，且被授权访问本仓库；组织级 runner 需要在 runner group 中允许本仓库使用。
- 仓库 Actions Secret：`NEW_API_KEY`。在 **Settings → Secrets and variables → Actions** 中配置；不得写入代码、配置文件或日志。
- 内网接口：`https://amao-prd.csopasset.com/llm/v1/chat/completions`。
- 模型：`gpt-5.6-sol`。
- Python：3.11。扫描脚本只使用标准库，不需要安装应用依赖、pandas、Flask 或数据库驱动。
- Runner 需要访问上述内网接口，以及 GitHub 的 checkout、Python 安装与 artifact 上传服务。使用 Node 20 的 actions，runner 需支持该运行时；网络受限时可预装 Python 3.11 到 runner 工具缓存。

workflow 仅由有权限的开发者手动触发，不监听外部 PR。它使用 `contents: read` 权限，checkout 后不保留 Git 凭据。工作流必须先存在于默认分支，才会显示手动运行入口，参见 [GitHub 手动运行说明](https://docs.github.com/en/actions/how-tos/manage-workflow-runs/manually-run-a-workflow)。

## 扫描范围与失败处理

- 读取 `git ls-files` 列出的 Python 文件，排除扫描器及其专用测试。不扫描 `.env`、未跟踪文件、Git 历史或二进制文件。完整文件清单与内容 SHA-256 写入 JSON 报告。
- 所有目标行按批次覆盖，大文件分段并保留边界上下文；每批附带仓库结构与共享核心代码，重点审查业务计算、日期、状态机、事务、异常恢复及文件一致性。
- 提交到模型前遮盖常见密钥、密码和签名 URL；不记录原始 API 响应。模型引用的路径、行号和代码证据需通过本地校验。
- HTTP 超时/暂时性错误会有限重试。接口错误、响应格式错误、遗漏复核决定或批次失败会使扫描结果标记为不完整并令步骤失败，不能误报为“没有风险”。失败时仍尽量上传已完成的报告。
- 扫描预算 45 分钟，单次请求最多等待 180 秒；job 上限 60 分钟。被强制取消或 runner 离线时，只能保留此前已写出的内容，artifact 上传也可能无法完成。
- 报告是静态分析候选，不证明实际运行行为。依赖数据库字段类型、生产数据或业务规则的结论需要开发者验证。

## 本地验证

运行不访问网络的回归测试：

```text
python -m unittest discover -s tests -p "test_logic_bug_scan.py" -v
```

在能访问内网接口的机器上，通过环境变量注入 `NEW_API_KEY` 后执行：

```text
python scripts/scan_logic_bugs.py --output-dir bug-scan-report
```

退出码：`0` 表示扫描完整完成（默认不因发现风险而失败）；`1` 表示扫描失败或不完整；`2` 表示启用 `FAIL_ON_HIGH=true` 且保留了高风险候选。
