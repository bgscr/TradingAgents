# 离线查看分析停止原因

`scripts/summarize_analysis_stop.py` 读取指定有界会话中已停止的分析，输出安全的 JSON 摘要。
它仅使用 Python 标准库，不导入分析程序，不读取凭据或数据库，不联网，也不写回历史记录。

在项目根目录运行，用实际路径替换最后一个参数：

```powershell
.\.venv\Scripts\python.exe -I -B .\scripts\summarize_analysis_stop.py --run-dir '.\reports\bounded-runs\<session_id>\analysis\<ticker>\<as-of>\runs\<run_id>'
```

`--run-dir` 必须指向本项目下的精确 `runs/<运行ID>` 目录，不能只指定会话目录。
摘要契约为 `analysis-stop-summary-v2`。支持以下两种停止记录：

- 分析员的 `critical_evidence_failure`：保留原停止标记和分析员诊断的核验。
- 图执行的 `operational_failure`：仅在没有分析员停止标记，且最终运行状态、最后一次
  失败模型传输、经 SHA256 验证的操作异常类型链相互对应时，输出
  `reason=model_transport_failure`。节点和阶段须匹配固定白名单，例如
  `failure_node=Bear Researcher`、`failure_stage=research_debate`；此时 `analyst=null`。

存在但损坏或不受支持的停止标记不会被第二条路径绕过。缺少可靠关联、未知契约、
最后一次传输已成功或异常类型不对应时，不推断传输停止原因。

分析员摘要中的 `failure_stage` 来自本次运行日志引用、经 SHA256 验证的诊断制品。
图执行摘要使用最后一次传输的已知节点与阶段，不读取模型消息日志；操作异常制品仅用于
核验类型链，不输出异常正文。两种摘要均增加固定名称的 `failure_node`。
`transport_error_type`、`transport_error_family`、耗时与 HTTP 状态来自该运行的
`runtime_metrics.json`。只展示与停止记录一致的最后一次失败模型传输；后续成功不会被
较早的失败覆盖。未知类型不原样回显，缺失、损坏或不支持的记录会出现在 `limitations`。
摘要不显示服务地址、请求内容、请求标识、请求头、异常正文或配置。
`reason=model_transport_failure` 只是诊断字段，启动器原有 `application_stopped`、
异常类型、非零退出码、预算及不续跑限制保持原样；历史 `result.json` 不会补写。

`RemoteProtocolError` 说明记录了传输协议异常，不能单凭它判断是服务端、代理还是网络
故障；约 60 秒的耗时也不等于已证实超时。工具不会改变停止状态、预算、重试策略或证据
门槛。退出码 0 表示成功生成诊断摘要，不代表股票分析成功；退出码 2 表示无法生成摘要。

采用经过审查的接入版本后，有界分析启动器会在 CLI 失败收尾后，从本次运行捕获的精确
目录生成同一摘要，添加到新会话的 `result.json.stop_summary` 和终端 JSON。它不会扫描
旧会话、重启分析或覆盖原停止原因。缺失记录或摘要生成失败会显示固定的
`diagnostic_unavailable` 状态，原失败、非零退出码和不续跑限制仍保留。

独立命令仍可用于已有的历史运行。入口与摘要模块都应纳入
`config/bounded_mainland_release.json` 的精确哈希绑定；采用流程须遵守
[有界入口说明](bounded-mainland-entry.md)，不能机械刷新哈希。候选目录不代表已安装。

针对该工具的测试只使用合成记录，可通过标准库 unittest 独立运行，不加载项目的 pytest
应用夹具。执行本机验证前，将 `TRADINGAGENTS_TEST_TEMP_ROOT` 指向已授权的新测试目录：

```powershell
.\.venv\Scripts\python.exe -I -B -m unittest discover -s tests -p test_analysis_stop_summary.py -v
```
