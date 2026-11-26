# 干净检出的离线评审

评审者可以在已安装项目测试依赖的环境中，从仓库根目录运行合成回归。以下选日和发布用例使用合成安装输入、内存采集器与临时报告目录。财务回放使用测试内构造的 47 条修订记录，覆盖 24 个季度。公告测试使用本地 SDK 和模拟 HTTP 响应。

```powershell
python -I -B -c "import os, sys, pathlib; os.environ['PYTEST_DISABLE_PLUGIN_AUTOLOAD']='1'; import pytest; p=pathlib.Path.cwd(); sys.path[:0]=[str(p),str(p/'scripts'),str(p/'tests')]; sys.exit(pytest.main(['--noconftest','-p','no:cacheprovider','-q','tests/test_review_publication_and_preflight.py','tests/test_cninfo_announcement_dates.py']))"
```

财务父进程的专门用例还检查 Python 的 `-I -S -B` 启动标志。请使用项目已准备的测试环境运行。回归通过能证明本地代码行为。任何真实数据覆盖、账户准入和供应商可用性均须在受审安装中单独验证。

普通 Full 的构建器 `scripts/build_ordinary_full.py` 先核对固定配方及 Git 源文件。对于配方声明的本机证据，还会核对原清单摘要、部署根目录、每条路径及其文件摘要。目标中的项目解释器及标准库也须通过校验。干净检出缺少这一整套受审输入时，安装校验会停止。不要从其他机器复制清单、凭据、数据库或历史回执来补齐。打包工具生成的候选收据只记录暂存结果，正式安装和真实调用各有独立授权。

`601328.SH` 和 `601328.SS` 的 2026-11-13 调度缺少可核实的终态。该日预检会报告 `prior_dispatch_unreconciled`。基础报告发布失败或导出摘要不符时，增强采集没有发送资格。基础发布回执写入后，每一次采集仍受原预算和权限约束。


The frozen operator admission batch requires the original role proposal, supplied locally at its reviewed fixture path. Its validator still checks its exact digest and identity. A source checkout does not provide that private input or grant permission to run the historical batch. The archived market-data research note is retained in the original private working materials. Neither input participates in ordinary Full startup.
