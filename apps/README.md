# apps 目录说明

`apps/` 目录集中放置本项目的应用级入口程序，覆盖 GUI 启动、无线电接收、无线电发射以及联调工具。

## 目录结构

```text
apps/
├── __init__.py              # apps 包初始化文件
├── gui_launcher.py          # 标准干扰波接收 GUI，负责参数配置、日志展示与进程托管
├── gui_launcher_info.py     # 默认直接按信息波模式解析的 GUI 变体
├── gui_launcher_onekey.py   # 一级解析干扰波密钥，随后转信息波解析的 GUI 变体
├── jam_rx_app.py            # 当前三个 GUI 共用的核心接收程序
├── rx_app.py                # 信息波接收命令行程序，面向链路调试
├── jam_tx_app.py            # 干扰波发射测试程序，用于模拟 0x0A06
├── tx_app.py                # 信息波发射测试程序，用于模拟 0x0A01 到 0x0A05
└── dual_rx_app.py           # 双接收联调启动器，可同时拉起信息波与干扰波接收
```

## 主要调用关系

- 当前三个 `gui_launcher` 最终都会启动 `jam_rx_app.py`。
- `jam_rx_app.py` 会调用 `parser/gnuradio_frame_parser.py`、`phy.py`、`protocol.py`、`radio_profiles.py` 与 `server_comm.py` 完成完整接收链路。
- `tx_app.py` 与 `jam_tx_app.py` 会调用 `phy.py`、`protocol.py`、`launch/message_value_generate.py` 等模块生成发射波形。

## Payload 解析策略

- `gui_launcher.py`
  采用 `default` 解析策略，信息波 payload 固定按小端序解析。

- `gui_launcher_info.py`
  采用 `info_only` 解析策略，直接按信息波模式解析，payload 固定按小端序解析，不再自动切换端序。

- `gui_launcher_onekey.py`
  采用 `onekey_then_info` 解析策略，一级阶段解析干扰波密钥，后续转信息波解析，payload 固定按小端序解析，不再自动切换端序。

- `jam_rx_app.py`
  作为底层接收程序，仍支持 `--payload-endian little|big|auto`，但当前 GUI 入口均固定传入 `little`。

## 使用建议

- 比赛或现场运行优先使用当前三个 GUI 启动器。
- 纯链路排查优先使用 `jam_rx_app.py`、`rx_app.py`、`jam_tx_app.py`、`tx_app.py` 做命令行联调。
- 双设备并行接收场景使用 `dual_rx_app.py`。
