# 南京理工大学江阴校区Combat战队2026赛季雷达无线电部分

本项目为南京理工大学江阴校区 Combat 战队 2026 赛季雷达无线电链路相关代码，覆盖干扰波接收、信息波接收、协议编解码、波形生成、频点参数配置以及与主程序服务器的联动通信。本项目仅需要一块pluto sdr模块可实现对干扰波和信息波的接收，在开启gui_launcher.py时，东部赛区实测稳定解析2次干扰波，但信息波解析率较低。安装python依赖后即可部署。

## 项目作用

项目围绕雷达无线电链路解析信息波和干扰波两个核心任务展开：

- 干扰波链路：接收并解析 0x0A06 干扰波密钥，跟随主程序下发的干扰等级切换接收频点。
- 信息波链路：接收并解析 0x0A01 到 0x0A05 裁判系统数据，将0x0A01中各机器人坐标发送给主程序，用于覆盖视觉定位结果。
- 图形化运行：通过当前三个 `gui_launcher` 入口提供不同接收策略的图形界面，便于现场调试与比赛运行。

## 项目结构

```text
CombatRadarSdr/
├── README.md                      # 项目总说明
├── requirements.txt               # 当前三个 gui_launcher 的核心运行依赖
├── __init__.py                    # 包初始化文件
├── protocol.py                    # 协议常量、CRC、裁判系统帧与空口包封装
├── protocol_jam_key.py            # 干扰波密钥生成与试验逻辑
├── phy.py                         # 2-GFSK 物理层处理，包含发射 IQ 生成与 FM 解调
├── radio_profiles.py              # 红蓝双方不同等级下的无线电频点与参数配置
├── server_comm.py                 # 与雷达主程序的 TCP 双向通信模块
├── apps/                          # 应用入口目录，含 GUI、接收程序、发射程序与联调工具
│   ├── README.md                  # apps 子目录说明
│   ├── __init__.py                # apps 包初始化文件
│   ├── gui_launcher.py            # 标准干扰波接收 GUI，payload 固定按小端序解析
│   ├── gui_launcher_info.py       # 信息波接收 GUI，直接按信息波模式解析，payload 固定按小端序解析
│   ├── gui_launcher_onekey.py     # 一级干扰波密钥接收 GUI，后续转信息波解析，payload 固定按小端序解析
│   ├── jam_rx_app.py              # GUI 共用的核心接收程序
│   ├── rx_app.py                  # 信息波接收命令行程序
│   ├── jam_tx_app.py              # 干扰波发射测试程序
│   ├── tx_app.py                  # 信息波发射测试程序
│   └── dual_rx_app.py             # 双接收联调启动器
├── parser/                        # 协议流重组与解码目录
│   └── gnuradio_frame_parser.py   # 候选包切片、偏移选择、循环过滤与字段解码
├── launch/                        # 发射与仿真数据生成目录
│   ├── frame_generate.py          # 将协议 payload 组装为空口包
│   └── message_value_generate.py  # 构造 0x0A01 到 0x0A05 的模拟数据内容
├── images/                        # 协议说明图片与项目配图
├── logs/                          # GUI 运行日志输出目录
└── radio_logs/                    # 录波文件与录波元数据输出目录
```

`apps/` 目录中的 GUI 启动器与应用入口说明见 [apps/README.md]。

## 运行关系

当前仓库中的三个 GUI 启动器本身主要负责界面展示、参数配置、日志收集与进程管理，实际接收进程统一由 `apps/jam_rx_app.py` 承担。GUI 会优先尝试通过：

```bash
conda run --no-capture-output -n radio python3 -u -m radar.CombatRadarSdr.apps.jam_rx_app
```

启动接收程序，因此推荐保留名为 `radio` 的 Anaconda 环境。

### 安装 Python 依赖

```bash
pip install -r requirements.txt
```

## 解析策略

当前仓库内三个 GUI 的解析策略如下：

- `gui_launcher.py`：先解析两次干扰波，在干扰波等级到达三级后，转向解析信息波。
- `gui_launcher_onekey.py`：先解析一次干扰波，在干扰波等级到达二级后，转向解析信息波。
- `gui_launcher_info.py`：只解析信息波。

## 运行指南

项目根目录下
python3 ./apps/gui_launcher.py

交流请联系 Yanshu Wang QQ:2942349330
