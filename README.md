# RIGOL MHO98 正弦扫频与多通道采集控制器

Windows 上的 RIGOL MHO98 PC 上位机。通过原生 USBTMC、PyVISA 或 LAN/SCPI 控制 GI/GII 正弦输出，执行任意范围的线性/对数扫频，并从同一冻结采集帧读取 CH1–CH4、实时写入 CSV 和生成可选通道的伯德图。

## 本次更新：新增功能与 Bug 修复

### 新增功能

- 扫频不再限定为固定的 300 点或固定频段：支持任意起止频率、线性总点数、对数每十倍频点数，以及起止频率相同的单频采集。
- 每个频点可分别设置稳定周期数和保存周期数 M；程序按仪器回读频率自动选择、回读并校验时基，只保存最新的 M 个完整周期。
- 支持任意勾选 CH1–CH4。程序先冻结一次采集，再依次读取所选通道，确保各通道来自同一冻结帧；参考通道和响应通道也可自由选择。
- 支持带直流偏置的正弦输出 `B + A·sin(2πft)`。默认 `B=0 V`；既可使用统一 A/B，也可从 CSV 为每个频点分别指定频率、A 和 B。
- 每次变频后重新设置并回读相位、执行 MHO98 硬件相位对齐；另提供双 AFG 相位验证和多通道回环校准工具，用于测量线缆及通道链路的相位差和固定延迟。
- CSV 新增请求值与仪器实际回读值，包括频率、幅度、偏置、相位、实际时基和采样间隔；各通道电压始终来自示波器真实采集，而不是软件设定值。
- 新增相对采样时间轴、捕获 UTC、估算样点 UTC 和实际扫频经过时间，明确区分硬件波形时间与主机保存时间。
- 新增 CH1–CH4 通用离线绘图、USB 回环验证、双 AFG 相位验证、通道校准脚本，以及不连接仪器即可执行的核心回归测试。

### Bug 修复

- 修复停止扫频后的输出关闭、时基恢复与快速重新启动之间的竞态：清理完成前禁止开始下一轮扫频。
- 修复用软件设定频率/幅度代替仪器实际接受值的问题：每个频点均执行 SCPI 回读并检查错误队列，CSV 和计算使用实际值。
- 修复多通道读取可能跨越不同采集时刻的问题：每个频点只执行一次 `:STOP`，全部通道读取完成后才恢复原 RUN/STOP 状态。
- 修复固定时基在不同频率下可能截不全目标周期或引入不一致的问题：逐频点计算时基，并在仪器档位向下量化导致覆盖不足时自动增大重试。
- 修复 `stored_timestamp` 仅表示写盘时间、容易被误认为采样时刻的问题：移除该字段并使用含义明确的时间列。
- 修复波形点数不足、通道时间轴不对齐时仍继续保存的问题：增加完整性校验与同频点重试，避免生成不完整或错位数据。
- 修复偏置和幅度切换顺序可能造成瞬时输出越界的问题：变更频点时先清零旧偏置，再设置新幅度和新偏置，并校验 `|B| + Vpp/2`。

## 功能

| 功能 | 实现 |
| --- | --- |
| 仪器连接 | Windows SetupAPI + `ausbtmc.sys` 原生 USBTMC；PyVISA 回退；LAN 5555/5025 |
| AFG 输出 | GI/GII 正弦波，2 mHz–100 MHz；设置后回读实际频率、幅度、相位并检查 SCPI 错误队列 |
| 直流偏置 | 支持 `B + A·sin(2πft)`；默认 `B=0 V`，每个频点可使用不同 A/B |
| 扫频 | 任意起止频率；线性按总点数；对数按每十倍频点数；起止相同可做单频采集 |
| 相位一致 | 每次变频都重设并回读 AFG 相位，再执行 MHO98 硬件“相位对齐”命令 |
| 周期控制 | 每点先等待用户指定的稳定周期，再保存用户指定的 M 个周期 |
| 自动时基 | 每个频点按 AFG 回读频率和 M 周期重新计算；回读实际 `s/div`，覆盖不足时自动增大 |
| 同帧采集 | 每点发送一次 `:STOP`，从冻结帧依次下载所选 CH1–CH4，再恢复原 RUN/STOP 状态 |
| 通道校准 | AFG 等长回环到所选输入，测量通道间幅值比、相位差和固定延迟 |
| 实时 CSV | 每个频点完成后立即追加并 `flush`；保存 AFG 设定/回读值、波形时间轴及最多四路实测电压 |
| 伯德图 | 参考通道和响应通道可在 CH1–CH4 中选择；默认 CH2 为信号源回读、CH1 为传感器响应 |
| 离线绘图 | 可绘制任意选择的 CH1–CH4、扫频频率轨迹和代表频点波形 |

## 硬件通道限制

MHO98 提供 4 个模拟输入通道和 2 个 AFG 输出。示波器的四个模拟输入可以在同一采集事件中采样；SCPI 下载仍然是逐通道传输，但程序先冻结一次采集，因此 CH1–CH4 来自同一个冻结记录。

界面允许任意勾选 1–4 路。例如可以采集“加速度 + 倾角 X/Y/Z”，也可以去掉 Z，换成一路 AFG BNC 实测回读。只要总数不超过四路，就不需要增加其他采集设备。若倾角传感器输出是 RS-485/CAN 等数字总线，则不能直接当作模拟电压通道。

## 安装与运行

要求 Windows 10/11、Python 3.10+ 和 RIGOL MHO98。

```bash
python -m pip install -r requirements.txt
python mho98_controller.py
```

不连接仪器即可运行核心测试：

```bash
python -m unittest discover -s tests -v
```

## 连接仪器

### USB

使用数据线连接示波器后面板 USB Device 口。点击“扫描并连接 USB”；程序优先查找 Windows 的 “USB Test and Measurement Device (IVI)” 接口并使用原生 USBTMC，PyVISA 仅作回退。

### LAN

电脑和示波器接入同一网段，填写示波器自身的 IP Address。程序依次尝试端口 `5555`、`5025`，并以 `*IDN?` 验证返回值包含 `RIGOL`。

## 扫频配置

| 设置 | 含义 |
| --- | --- |
| `方式=linear` | 起始和终止频率之间等间隔；使用“线性总点数” |
| `方式=log` | 频率按对数间隔；使用“对数点/十倍频” |
| 起始/终止 Hz | 2 mHz–100 MHz；两者相同则只采一个频率 |
| 保存周期 M | CSV 中保留的完整周期数 |
| 稳定周期 | 改变频率后、冻结采集前额外等待的周期数，不写入 CSV |
| 采集通道 | 勾选 CH1–CH4 中任意一路或多路；通道必须已在示波器上开启 |
| 偏置 B (V) | 未加载逐频点曲线时使用的统一直流偏置；默认 0 V |

每个频点的流程：

```text
设置下一频点 AFG → 回读实际频率/幅度/偏置/相位 → 执行硬件相位对齐 → 检查 SCPI 错误
    ↓
按实际频率和 M 周期计算时基 → 回读并检查实际 s/div → :RUN
    ↓
等待“稳定周期 + 保存周期” → :STOP 冻结一次
    ↓
下载所选 CH1–CH4 → 恢复 :RUN → 截取最新 M 周期
    ↓
实时追加 CSV → 再改变时基并进入下一个频点
```

仪器可能把请求的时基量化到支持的档位。程序不会直接相信请求值：它会查询实际接受的 `s/div`，确认屏幕时间跨度至少覆盖 M 个周期；若固件向下取整导致覆盖不足，会自动请求更大的档位。最终接受值写入 CSV 的 `timebase_scale_s`，实际采样间隔仍以波形前导参数中的 `sample_interval_s` 为准。

界面中的“预计纯驻留剩余时间”只是等待周期的进度估算，不参与数据分析。CSV 的频率使用 AFG 回读值，电压使用示波器实际采集值，实际扫频经过时间记录在 `sweep_elapsed_s`。

停止扫频时界面进入“正在停止”状态。AFG 输出关闭和原时基恢复完成前，开始按钮保持禁用，避免上一轮清理命令与下一轮配置交错。

## 带偏置正弦与逐频点 A/B 曲线

MHO98 的幅度命令使用峰峰值 Vpp，因此 GUI 中的正弦输出严格表示为：

```text
v(t) = B + (amplitude_vpp / 2) · sin(2πft)
```

也就是数学表达式 `B + A·sin(2πft)` 中的 `A = amplitude_vpp / 2`。默认偏置 `B=0 V`，与此前无偏置行为完全兼容。手动设置和扫频都会查询仪器实际接受的偏置。

统一 A/B 时，直接在 GUI 设置“幅度 (Vpp)”和“偏置 B (V)”。不同频率需要不同 A/B 时，点击“加载逐频点 A/B CSV…”。推荐使用数学峰值格式：

```csv
frequency_hz,a_peak_v,b_offset_v
10,0.5,0
100,0.4,0.1
1000,0.25,-0.2
```

也可以使用仪器原生峰峰值格式：

```csv
frequency_hz,amplitude_vpp,offset_v
10,1.0,0
100,0.8,0.1
1000,0.5,-0.2
```

两种幅度列只能选择一种；偏置列可以省略，省略时该行按 `B=0 V`。CSV 行顺序就是扫频顺序，频率不能重复。加载后，文件中的频率、幅度和偏置覆盖 GUI 的统一频率范围/A/B；稳定周期、保存周期和采集通道仍使用 GUI 设置。示例文件见 `examples/sweep_ab_profile_example.csv`。

简写表头 `f,A,B` 也支持；其中 `A` 按数学峰值 V、`B` 按偏置 V 解释。

偏置和幅度共享输出电压范围。程序检查 `|B| + Vpp/2`，切换频点时先把旧偏置归零，再设置新幅度和新偏置，最后逐项回读并检查仪器错误队列。实际限制还可能随 AFG 输出阻抗变化，仪器回读值与错误队列是最终依据。

## 变频时的相位一致性

MHO98 编程手册提供 `:SOURce<n>:PHASe`（0–360°，0.01° 分辨率）和 `:SOURce<n>:PHASe:SYNChronize`。正式扫频在每个频点把所选 AFG 相位设为 0°、查询仪器实际接受值，然后执行 `PHASe:SYNChronize` 和 `*WAI`，再进入稳定等待与同帧采集。CSV 的 `requested_phase_deg` / `phase_deg` 保存请求和回读相位。

这里保证的是每次变频后具有确定的 AFG 相位基准，以及同一冻结帧内参考/响应通道的相对相位可比较。不同频率记录之间的原始相位不会因为“都设为 0°”就保持同一个数值：线缆、AFG 双通道和示波器输入链路的固定时间延迟会产生 `相位 = -360° × 频率 × 延迟` 的线性斜率。

双 AFG 回环验证（AFG1→CH2、AFG2→CH1）：

```powershell
python validate_dual_afg_phase.py --yes `
  --frequencies 100000 200000 500000 1000000 2000000 5000000 10000000 `
  --passes 2 --repeats 3 --capture-cycles 8
```

奇数轮升频、偶数轮降频；每次重复都重新执行硬件相位对齐。输出原始 CSV、逐频点重复性 CSV 和包含综合延迟的 JSON。该综合延迟同时包含 AFG1/AFG2 通道偏斜、两根 BNC 的长度差以及 CH1/CH2 输入偏斜；若要把它解释成纯线缆长度差，必须先单独校准其他项。

## CH1–CH4 同帧采集

MHO98 编程手册允许 `WAVeform:SOURce` 选择 `CHANnel1`–`CHANnel4`。程序在每个频点只发送一次 `:STOP`，确认 `TRIGger:STATus?` 返回 `STOP` 后再逐通道读取 `NORMal/ASCii` 屏幕数据。因为采集已经冻结，下载顺序不会改变各通道所属的采集帧。

程序还会校验各通道：

- 显示状态已开启；
- `WAVeform:SOURce?` 与请求通道一致；
- `PREamble` 为 ASCII/NORMAL；
- 点数、`XINCrement` 和 `XORigin` 可对齐。

四通道开启时，示波器可能降低可用采样率或存储深度，这是硬件采集架构的正常限制，应在实机上核对状态栏和 CSV 中的 `sample_interval_s`。

## 时间字段说明

MHO98 SCPI 提供 `XINCrement`、`XORigin`、`XREFerence`，可以准确描述波形记录内的相对时间；官方命令没有提供每个样点的绝对硬件采样时间戳。`SYSTem:DATE?` / `SYSTem:TIME?` 只有秒级分辨率且手册明确说明有命令响应延迟，因此不能伪装成精确采样时刻。

新 CSV 不再写入原先无实际时序意义的 `stored_time_s` / `stored_timestamp`，改为：

| 列名 | 含义 |
| --- | --- |
| `instrument_time_s` | 根据波形前导参数计算的仪器时间轴，通常相对触发点 |
| `point_time_s` | 当前频点所保存 M 周期窗口内的相对时间 |
| `capture_timestamp_utc` | 示波器确认进入 STOP 后立即记录的主机 UTC 时刻；同一频点所有通道相同 |
| `estimated_sample_timestamp_utc` | 用捕获 UTC 减去样点到帧尾的相对时间得到的估算 UTC；不是硬件绝对时间戳 |
| `sweep_elapsed_s` | 从本轮扫频开始到该样点的主机单调时钟估算，包含稳定等待和频点间通信时间 |

对于增益和相位计算，应使用 `point_time_s`/`instrument_time_s`，不要依赖绝对 UTC。

## 采集 CSV

固定元数据列：

| 列名 | 含义 |
| --- | --- |
| `sample_index` | 样本序号 |
| `sweep_point` | 扫频点序号 |
| `requested_frequency_hz` | 软件请求的 AFG 频率 |
| `frequency_hz` | 仪器回读的实际 AFG 频率，也是拟合使用的频率 |
| `requested_amplitude_vpp` | 软件请求幅度 |
| `amplitude_vpp` | 仪器回读幅度 |
| `requested_sine_amplitude_peak_v` | 数学公式中的请求峰值 A，即请求 Vpp/2 |
| `sine_amplitude_peak_v` | 按仪器回读 Vpp 计算的实际峰值 A |
| `requested_offset_v` | 软件请求直流偏置 B |
| `offset_v` | 仪器回读直流偏置 B |
| `requested_phase_deg` | 每个频点软件请求的 AFG 相位 |
| `phase_deg` | 仪器回读相位；随后执行硬件相位对齐 |
| `timebase_scale_s` | 当前频点仪器实际接受的时基 `s/div` |
| `period_s` | 按回读频率计算的周期 |
| `cycle_number` | 当前保存窗口中的周期编号 `1..M` |
| `sample_interval_s` | 波形前导参数给出的采样间隔 |

电压列按勾选通道动态写入：`ch1_voltage_v`、`ch2_voltage_v`、`ch3_voltage_v`、`ch4_voltage_v`。这些值全部来自示波器采集；例如 CH2 通过 BNC 接回 AFG 输出时，`ch2_voltage_v` 就是该物理回路的实测电压，不是软件设定波形。

## 通道回环校准

校准目标是测量 CH1–CH4 模拟输入链路之间的固定幅值差、相位差和等效延迟。将一个 AFG 输出通过等长 BNC/分配器接到待校准通道，所有输入使用相同耦合、探头倍率、带宽限制和终端设置。建议全部使用 `1 MΩ`；不要把四个 `50 Ω` 输入直接并联到一个 AFG 输出。

自动 USB、CH1 作为参考、校准四通道：

```powershell
python calibrate_channel_loopback.py `
  --channels 1 2 3 4 --reference 1 `
  --start-hz 10 --stop-hz 100000 --spacing log `
  --points-per-decade 5 --capture-cycles 8 --repeats 3
```

LAN 和自定义频率示例：

```powershell
python calibrate_channel_loopback.py --lan 192.168.1.100 `
  --channels 1 2 4 --reference 1 `
  --frequencies 10 100 1000 10000 --amplitude-vpp 1
```

程序在每个频点设置独立时基、等待、冻结一次并读取全部所选通道，然后拟合正弦波。输出包括原始 CSV、汇总 CSV、JSON 校准参数和 PNG 图。汇总中的正延迟表示该通道比参考通道更晚；相位修正公式也写在 JSON 中。高频校准应使用实际测量所用线缆，或使用严格等长线缆把线缆差从通道校准中排除。

频率范围应覆盖正式测量范围；如果主要目的是分辨纳秒级固定延迟，还需要加入足够高的校准频率。多频率相位展开要求相邻校准点的相位变化小于约 180°，延迟较大时应增加频率点密度。

## 伯德图

界面可选择“参考/激励”和“响应”通道。对每个频点分别拟合：

```text
a·sin(2πft) + b·cos(2πft) + c
```

并计算：

- 幅值比：响应 / 参考；
- 增益：`20·log10(响应/参考)`；
- 相位：`φ响应 − φ参考`；
- 两通道拟合 R² 和有效点数。

按照 CH1=加速度、CH2=AFG BNC 回读的接线，选择参考 CH2、响应 CH1。伯德数据 CSV 使用通用字段 `reference_amplitude_v`、`response_amplitude_v`、`reference_fit_r2`、`response_fit_r2`，并保存通道编号。

## 辅助绘图

```bash
# 任意选择 CH1–CH4
python plot_waveform_csv.py data.csv --channels 1 2 3 4 --output-dir output/figures

# CH1/CH2 普通或等坐标轴图；同时兼容新旧时间列
python plot_ch1_ch2.py data.csv
python plot_ch1_ch2_equal_axes.py data.csv --time-limits 0 90 --voltage-limits -0.6 0.6
```

## 项目文件

| 文件 / 目录 | 说明 |
| --- | --- |
| `mho98_controller.py` | Tkinter GUI、扫频、同帧采集、CSV 与伯德图 |
| `calibrate_channel_loopback.py` | AFG 多通道回环相位/固定延迟校准工具 |
| `validate_dual_afg_phase.py` | AFG1→CH2、AFG2→CH1 双路相位对齐、往返扫频和线缆/链路综合延迟验证 |
| `validate_usb_loopback.py` | 可恢复的 USB 实机闭环、动态时基和同帧通道验证工具 |
| `examples/sweep_ab_profile_example.csv` | 逐频点 `B + A·sin(2πft)` 曲线示例 |
| `windows_usbtmc.py` | Windows 原生 USBTMC 传输 |
| `plot_waveform_csv.py` | CH1–CH4 通用扫频绘图工具 |
| `plot_ch1_ch2*.py` | 双通道对比绘图工具 |
| `tests/` | 无硬件核心回归测试 |
| `docs/FUNCTIONAL_REVIEW.md` | 功能、实现和硬件验证边界 |
| `docs/HARDWARE_VALIDATION.md` | 本机 MHO98 USB 回环实测结果 |
| `MHO98_ProgrammingGuide_EN.pdf` | RIGOL 官方英文编程指南 |

## 实机验证边界

代码级测试可以验证频点生成、周期截取、自动时基重试、四通道冻结读取命令顺序、AFG 回读字段、回环延迟拟合和 CSV 模式。仓库已经用一台真实 MHO98 完成 AFG1→CH2 的 10–10000 Hz 闭环和四通道同帧元数据验证，结果见 `docs/HARDWARE_VALIDATION.md`。不同仪器、固件和接线仍建议先运行同一验证工具。
