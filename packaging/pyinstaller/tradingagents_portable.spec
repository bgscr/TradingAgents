# -*- mode: python ; coding: utf-8 -*-

from PyInstaller.utils.hooks import collect_data_files, collect_submodules


block_cipher = None


def safe_collect_submodules(package_name):
    try:
        return collect_submodules(package_name)
    except Exception:
        return []


def safe_collect_data_files(package_name):
    try:
        return collect_data_files(package_name)
    except Exception:
        return []


HIDDEN_IMPORT_PACKAGES = [
    "akshare",
    "baostock",
    "certifi",
    "cli",
    "dotenv",
    "langchain_anthropic",
    "langchain_core",
    "langchain_experimental",
    "langchain_google_genai",
    "langchain_openai",
    "langgraph",
    "langgraph_checkpoint",
    "numpy",
    "openai",
    "pandas",
    "pydantic",
    "questionary",
    "redis",
    "requests",
    "rich",
    "stockstats",
    "tqdm",
    "tradingagents",
    "typer",
    "yfinance",
]

hiddenimports = []
for package_name in HIDDEN_IMPORT_PACKAGES:
    hiddenimports += safe_collect_submodules(package_name)

datas = []
for package_name in ["certifi", "cli"]:
    datas += safe_collect_data_files(package_name)


tradingagents_analysis = Analysis(
    ["packaging/pyinstaller/tradingagents_entry.py"],
    pathex=[],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)
tradingagents_pyz = PYZ(
    tradingagents_analysis.pure,
    tradingagents_analysis.zipped_data,
    cipher=block_cipher,
)
tradingagents_exe = EXE(
    tradingagents_pyz,
    tradingagents_analysis.scripts,
    [],
    exclude_binaries=True,
    name="tradingagents",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

candidate_analysis = Analysis(
    ["packaging/pyinstaller/ak_pick_a_stock_entry.py"],
    pathex=[],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)
candidate_pyz = PYZ(
    candidate_analysis.pure,
    candidate_analysis.zipped_data,
    cipher=block_cipher,
)
candidate_exe = EXE(
    candidate_pyz,
    candidate_analysis.scripts,
    [],
    exclude_binaries=True,
    name="ak_pick_a_stock",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    tradingagents_exe,
    candidate_exe,
    tradingagents_analysis.binaries,
    candidate_analysis.binaries,
    tradingagents_analysis.zipfiles,
    candidate_analysis.zipfiles,
    tradingagents_analysis.datas,
    candidate_analysis.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name="TradingAgents-Win64",
)
