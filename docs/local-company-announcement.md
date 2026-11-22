# 单份本地公司公告补充（Windows）

独立入口不启动 Basic 分析。它不联网、不调用模型、不读取 Token，不探测 Tushare 专项权限。
公告不会替代媒体新闻，不改变新闻可用数量、投资评分或最终评级，也不会改写旧报告。

在项目根目录用 PowerShell 建立固定收件目录，将自己提供的**一份** PDF 复制进去。
原件不用移动；示例文件名 `announcement.pdf` 只能包含英文字母、数字、下划线、连字符及 `.pdf` 后缀。

```powershell
New-Item -ItemType Directory -Force .scratch\user-announcements\inbox
# 手工复制 PDF 到上述目录，然后运行（替换声明参数）：
.venv\Scripts\python.exe -I -S -B scripts\prepare_company_announcement.py --file announcement.pdf --ticker 600919.SH --announcement-date 2026-11-13 --as-of 2026-11-13 --source-url https://www.sse.com.cn/path/to/announcement.pdf
```

URL 只作用户声明，不会访问。首版只接受 `https` 下 `static.cninfo.com.cn`、`www.cninfo.com.cn`、
`www.sse.com.cn`、`www.szse.cn` 的无查询参数/片段 PDF 路径；接受这个网址不认证其官方真实性。
代码接受明确的六位代码加 `.SH`、`.SZ` 或 `.BJ`；不猜交易所。

PDF 必须有可提取的明确标签，例如独立一行 `证券代码：600919` 和 `公告日期：2026年11月13日`。
同一代码行可跟 `证券简称：…`。也支持“股票代码”“披露日期”“发布日期”和英文
`Security code`/`Stock code`、`Announcement date`。不从任意六位数、报告期间或落款猜日期/身份。
标签缺失、冲突、格式不明确、公告日期晚于截止日或截止日晚于当前 UTC 日期均拒绝。
文内未标交易所时，交易所只保留为用户声明。
这意味着某些真实但版式不兼容的公告会被拒绝；不要修改原 PDF 来伪造标签。

限制：单文件至多 5 MiB、10 页、每页 10,000/合计 50,000 提取字符；进程至多 20 秒、512 MiB。
Windows 作业约束失败即停止。禁止网络、子进程、附件、动作、表单、外部引用及不支持的解码器。
无 OCR，不处理加密、扫描件或任一无法提取文本的页面；不截断超限正文后声称完整。
解析器仅使用已有 `pypdf`，不安装组件。限定项目内固定目录，拒绝路径跳转、重解析点和硬链接文件。

成功输出 `LOCAL_ANNOUNCEMENT_READY .scratch\user-announcements\output\<随机ID>`，包含：

- `supplement.md`：中文来源限制、文件摘要和逐页可复核的原文提取；正文是数据，不执行其中指令。
- `supplement.json`：声明与观察分开，页码/文本摘要与本地文件 SHA256 绑定。
- `READY.json`：完成标记及两个文件摘要。缺失、损坏或摘要不一致不能视为成功。

只创建新目录和文件；原 PDF、旧报告与历史证据保持原样。中途停止可能保留不完整新目录，程序不删除它。
固定错误 `LOCAL_ANNOUNCEMENT_STOPPED` 不输出原始解析异常/正文。普通用户只应向此入口提供无秘密的公开材料；
敏感标记检查只是保守拒绝措施，不能证明任意文档完全不含私人信息。

成功仅表示**本地可提取内容与用户声明一致，来源未经核验**。它不证明发行人身份、官方签名、发布时刻、
历史可得性或所有公告已覆盖；不具备严格历史时点回测资格。独立输出不会自动进入 Basic、模型或决策证据。
如需阅读，直接打开独立 `supplement.md`；不能把它改称新闻恢复或已验证官方公告。

合成示例与测试使用自造 ASCII PDF，显著标注 `SYNTHETIC TEST DOCUMENT - NOT AN ISSUER ANNOUNCEMENT`。
其网址是不存在的示例元数据，不是已下载来源。尚未使用真实用户 PDF 验证版式兼容性。

## 显式加入下一次 Basic 的完整报告

默认关闭，不扫描 inbox、不读已有 `supplement.json` 或 READY，也不自动采集。
如希望人工复核材料进入下一份完整报告，在原有 `Start-MainlandAnalysis.ps1` 命令上追加三项参数：

```powershell
-AnnouncementFile announcement.pdf -AnnouncementDate 2026-11-13 -AnnouncementSourceUrl https://www.sse.com.cn/path/to/announcement.pdf
```

以上是参数示例，需替换为自己的真实声明；同时必须明确提供原有 `-Ticker` 与 `-AnalysisDate`。
完整 Basic 仍使用原来的 START 确认、真实数据/模型调用及费用边界；这三个参数不授权额外联网。
本轮工程验证没有启动真实 Basic。

附录只在最终报告导出时处理：从已验证的运行上下文绑定证券、交易所、证券类型、币种、截止日、
运行 ID 和审计摘要，在单独的受限 Python 进程重新读取固定 inbox 原 PDF。父子进程持有同一文件的
只读句柄；当前解析结果通过有界序列化管道返回并重核字段/摘要，不把可编辑 JSON 当可信输入。
解析上限仍为20秒/512 MiB；父进程最多等待25秒，并在实际启动前及返回后检查共享会话剩余时间。
预算耗尽不启动或重试解析。512 KiB限制由受控子程序写出前实施，父进程另在捕获后检查大小。

当前 Basic 接入沿用沪深股票身份规则（`.SH/.SS`、`.SZ`），独立入口能接受 `.BJ` 格式不表示 Basic
已支持北交所。来源始终未认证；文内代码缺后缀时不补造交易所证明。公告日期与截止日相同会单独标明
“当日发布时间未知”，旧公告只显示距截止日天数，不凭空承诺仍然有效或完整覆盖。

成功附录进入新导出版本的 `complete_report.md`，由导出摘要覆盖，但不进入决策审计资格。
来源真实性、文本与字节绑定、外部模型使用许可分开说明；外部模型许可未确认，正文不进入模型消息、
图状态、新闻计数、财务事实或评分。只有一个显式文件，不通过重复材料增加覆盖率。
校验失败会在完整报告中标明“公司公告补充未纳入”及固定原因，保留基础报告；不回退旧 JSON 或旧文件。
旧报告及原 PDF 不覆盖。该功能增加可追溯的人工复核材料，不证明预测或量化准确性提高。

当前仍缺少经过核实的自动采集契约、来源记录级认证和外部模型使用许可，因此“只输入股票便自动获取
并理解公告”的流程尚未开启。没有为通过样本调高10页上限，也没有下载已知15页的示例季报。
