# RIGOL MHO98 正弦波控制器

<div align="center">

**RIGOL MHO98 系列示波器的 PC 上位机**——通过 Windows 原生 USBTMC 或 LAN 连接，实现双通道正弦波输出、0.1–500 Hz 线性扫频、CH1/CH2 波形采集与伯德图分析。

![Python](https://img.shields.io/badge/Python-3.10+-3776AB?style=flat&logo=python&logoColor=white)
![Platform](https://img.shields.io/badge/Platform-Windows-0078D6?style=flat&logo=windows&logoColor=white)
![GUI](https://img.shields.io/badge/GUI-Tkinter-4B8BBE?style=flat)
![Instrument](https://img.shields.io/badge/Instrument-RIGOL%20MHO98-ED1C24?style=flat)

</div>

---

## ✨ 功能一览

| 功能 | 说明 |
| --- | --- |
| 🔌 **USB 原生连接** | 通过 Windows SetupAPI 自动发现并连接 USB Test and Measurement Device，走 `ausbtmc.sys` 原生 USBTMC 通道 |
| 🌐 **LAN 连接** | 自动尝试 RIGOL 常用 SCPI 端口 `5555` 与 `5025`，逐端口显示失败原因 |
| 🎛️ **双通道正弦波输出** | 控制 GI / GII 通道的频率与幅度，一键启用/关闭输出 |
| 📈 **线性扫频** | `0.1–500 Hz` 共 300 个频点，按频段自动切换示波器时基 |
| 📊 **CH1 / CH2 采集** | 每个频点采集 5 个完整周期，仅保存中间第 2–4 周期，数据实时写入 CSV |
| 📉 **伯德图分析** | 正弦拟合后计算幅值比、增益 dB 与相位差，并给出 R² 与有效点数 |
| 🖼️ **波形预览** | 界面内置 5 周期设定波形预览，扫频进度实时可见 |
| 💾 **灵活导出** | 采集 CSV、伯德数据 CSV、伯德图（PNG / SVG / PDF）均可导出 |

---

## 🔌 连接设备

### 方式一：USB（推荐）

1. 使用 USB 数据线连接示波器**后面板的 USB Device 口**与电脑。
2. 点击软件中的 **「扫描并连接 USB」**。
3. 程序通过 Windows SetupAPI 查找 “USB Test and Measurement Device (IVI)”，识别到唯一设备后自动连接。

> 程序优先使用 IVI `ausbtmc.sys` 原生 USBTMC 通道，不依赖 PyVISA 的资源枚举；PyVISA 仅作为兼容回退。
> 设备资源通常形如 `USB0::0x1AB1::0x0452::MHO9A273700214::INSTR`。

### 方式二：LAN

1. 将示波器与电脑接入**同一局域网**。
2. 在示波器网络设置页查看 IP Address，填入软件后点击 **「连接 LAN」**。

| 配置项 | 示例 | 说明 |
| --- | --- | --- |
| 示波器 IP Address | `192.168.1.88` | 在示波器网络设置页查看 |
| 电脑 IP | `192.168.1.100` | 必须与示波器同一网段 |
| 子网掩码 | `255.255.255.0` | 两者保持一致 |

> ⚠️ 只填写示波器 IP，不要填写 DNS 或网关地址（例如 `192.168.1.1` 通常是路由器地址，并不是示波器地址）。

### 连接验证

无论哪种方式，程序都会发送 `*IDN?` 验证设备确为 RIGOL 示波器；**验证失败则保持未连接状态，绝不发送任何输出命令**，确保不会误操作其他仪器。

---

## 🚀 快速开始

### 环境要求

| 项目 | 要求 |
| --- | --- |
| 操作系统 | Windows 10 / 11 |
| Python | 3.10+ |
| 仪器 | RIGOL MHO98 系列示波器 |
| 硬件连接 | USB Device 数据线，或与示波器同一局域网 |

### 安装与运行

```bash
# 1. 安装依赖
python -m pip install -r requirements.txt

# 2. 启动上位机
python mho98_controller.py
```

依赖说明：

- `pyvisa` / `pyvisa-py` —— SCPI 通信与兼容回退
- `numpy` / `matplotlib` —— 波形处理、伯德图计算与绘图
- `tkinter` —— GUI 框架（随官方 Python 安装，无需额外配置）

---

## 🧭 使用说明

### 1️⃣ 连接与验证

- **扫描并连接 USB**：自动发现并连接唯一设备。
- **连接 LAN**：按上文填写 IP 后连接，自动尝试端口 `5555` 与 `5025`。
- 每次连接均以 `*IDN?` 验证，未通过验证不会进入已连接状态。

### 2️⃣ 设置正弦波输出

1. 选择输出通道：**GI / CH1** 或 **GII / CH2**。
2. 通过滑条或文本框设置**频率**与**幅度**（滑条为线性映射，文本框可输入精确值）。
3. 点击 **「应用正弦波参数」**，勾选 **「启用输出」**。

| 参数 | 范围 | 备注 |
| --- | --- | --- |
| 频率 | 2 mHz – 100 MHz | — |
| 幅度 | 2 mVpp – 20 Vpp | 高于 50 MHz 时最大 10 Vpp；50 Ω 负载下可用幅度更低 |

核心 SCPI 指令：

```text
SOURce<n>:FUNCtion          SINusoid
SOURce<n>:FREQuency         <frequency>
SOURce<n>:VOLTage:AMPLitude <amplitude>
SOURce<n>:OUTPut:STATe      ON | OFF
```

### 3️⃣ 线性扫频（0.1–500 Hz）

点击 **「开始扫频（0.1–500 Hz）」**，程序分三段生成 300 个严格递增、不重复的频点：

| 频段 | 频点数 | 示波器时基 | 每点保持时间 |
| --- | --- | --- | --- |
| 0.1 – 10 Hz | 100 | 5 s/div | `5 / f`（恰好 5 个周期） |
| 10 – 100 Hz | 100 | 50 ms/div | `5 / f` |
| 100 – 500 Hz | 100 | 5 ms/div | `5 / f` |

- 时基仅在 10 Hz 与 100 Hz 频段边界切换一次，段内 100 点保持不变；扫频结束或停止时自动恢复开始前的时基。
- 所有频点理论保持时间约 **4 分 36 秒**，实际总时长还需加上 300 次波形传输与 CSV 写入时间。
- 运行期间再次点击扫频或记录按钮，会停止扫频并关闭当前 AFG 输出。

### 4️⃣ CH1 / CH2 电压采集

- 点击 **「开始记录」**（或直接开始扫频）后，先选择 CSV 保存位置，文件会立即在电脑上创建。
- 每个频点先运行完整 5 周期，再依次读取 CH1、CH2 屏幕原始波形；**仅第 2–4 周期写入 CSV**，第 1、5 周期丢弃。
- 数据用 `WAVeform:MODE NORMal` 依次读取，**不会**发送 `STOP` / `RUN`，也不会开关任何通道。
- 读取前会校验 `WAVeform:SOURce?` 与 `WAVeform:PREamble?`，确认通道已开启后再取数。
- 每个频点提取完成后**立即追加并刷新到硬盘**，无需等待整个扫频结束。
- 窗口底部的预览始终绘制 5 个周期，横轴随频率自动变化；预览代表**发送给 AFG 的设定值**，不是从示波器采集的实测波形。

> 💡 ASCII 波形点本身就是实际电压，程序不会对 CH2 任意减去直流偏置。
> 未连接的 1 MΩ 输入属于悬空输入，可能拾取噪声或串扰；如需验证零电平，应将输入接地或在示波器上临时选择 GND 耦合。

### 5️⃣ 伯德图分析

- 以 **CH1 为输入、CH2 为输出**，对每个频点分别拟合 `a·sin(2πft) + b·cos(2πft) + c`。
- 计算幅值比 `CH2/CH1`、增益 `20·log10(CH2/CH1)`、相位差 `φCH2 − φCH1`，并保存两通道拟合幅值、R² 和有效点数。
- 完整扫频结束后**自动生成**伯德图；也可点击 **「用当前采集数据绘制」** 处理已完成的频点。
- **「加载波形 CSV…」** 支持：
  - 本程序生成的完整 CSV；
  - 仅含 `frequency`、`CH1`、`CH2` 三列的简化 CSV（无时间列时按每频点等间隔 3 周期处理，推荐提供 `point_time_s` 或 `sample_interval_s` 以获得可靠相位）。
- **「保存伯德数据…」** 导出计算后的 CSV；**「保存伯德图…」** 导出 PNG / SVG / PDF。

---

## 📄 数据文件格式

### 采集 CSV 列

| 列名 | 含义 |
| --- | --- |
| `sample_index` | 样本序号 |
| `stored_time_s` | 存储时间（秒） |
| `stored_timestamp` | 存储时间戳 |
| `sweep_point` | 扫频频点序号 |
| `frequency_hz` | 频率（Hz） |
| `period_s` | 周期（秒） |
| `cycle_number` | 周期编号 |
| `point_time_s` | 频点内相对时间（秒） |
| `sample_interval_s` | 采样间隔（秒） |
| `ch1_voltage_v` | CH1 电压（V） |
| `ch2_voltage_v` | CH2 电压（V） |

### 伯德数据 CSV 列

| 列名 | 含义 |
| --- | --- |
| `frequency_hz` | 频率（Hz） |
| `ch1_amplitude_v` / `ch2_amplitude_v` | 两通道拟合幅值（V） |
| `gain_ratio` | 幅值比 CH2/CH1 |
| `gain_db` | 增益（dB） |
| `phase_deg` | 相位差（°） |
| `phase_wrapped_deg` | 折叠后的相位（°） |
| `ch1_fit_r2` / `ch2_fit_r2` | 两通道正弦拟合 R² |
| `sample_count` | 参与拟合的有效点数 |

---

## 🛠️ 辅助绘图脚本

| 脚本 | 用途 | 用法 |
| --- | --- | --- |
| `plot_waveform_csv.py` | 绘制扫频全貌（CH1/CH2 波形 + 频率阶梯）与代表频点细节图 | `python plot_waveform_csv.py 数据.csv [--output-dir 输出目录]` |
| `plot_ch1_ch2.py` | 单次采集的 CH1/CH2 电压–时间双面板图（PNG + PDF） | 先修改脚本内的 `CSV_PATH` 后直接运行 |
| `plot_ch1_ch2_equal_axes.py` | 双通道**等坐标轴**对比图，便于定量比较 | 先修改 `CSV_PATH`，可调整时间/电压刻度区间 |

---

## 📖 参考资料

| 文件 | 内容 |
| --- | --- |
| `MHO98_ProgrammingGuide_EN.pdf` | RIGOL MHO98 官方英文编程指南 |
| `manual.txt` | 使用手册与补充说明 |

---

## ⚠️ 常见问题

**Q：可以用 2.4 GHz USB 接收器连接仪器吗？**
A：不可以。2.4 GHz USB 接收器一般仅供无线鼠标/键盘使用，并不是 MHO98 的远程控制接口；请使用 USB Device 数据线或 LAN。

**Q：LAN 连接失败怎么排查？**
A：确认填写的是示波器 IP（不是 DNS/网关）、电脑与示波器在同一网段、防火墙未拦截端口 `5555` / `5025`；界面会显示逐端口失败原因。

**Q：为什么 CH2 有直流偏置？**
A：程序保留示波器返回的实际电压，不会人为减去偏置。若 CH2 悬空，可能拾取噪声或串扰；验证零电平请接地或选择 GND 耦合。

**Q：预览窗口显示的是实测波形吗？**
A：不是。预览是发送给 AFG 的**设定值**波形，实测数据以 CSV 记录为准。

---

## 🗂️ 项目文件

| 文件 / 目录 | 说明 |
| --- | --- |
| `mho98_controller.py` | 主程序（Tkinter GUI） |
| `windows_usbtmc.py` | Windows 原生 USBTMC 通信模块（SetupAPI + `ausbtmc.sys`） |
| `plot_waveform_csv.py` | 扫频波形通用绘图工具（命令行） |
| `plot_ch1_ch2.py` | CH1/CH2 电压–时间绘图示例 |
| `plot_ch1_ch2_equal_axes.py` | 等坐标轴对比绘图示例 |
| `requirements.txt` | Python 依赖清单 |
| `manual.txt` | 使用手册 |
| `MHO98_ProgrammingGuide_EN.pdf` | 官方编程指南 |
