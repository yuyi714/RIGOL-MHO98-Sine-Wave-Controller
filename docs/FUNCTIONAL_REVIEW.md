# 功能与代码实现审查

审查基线：上游 `9746dc2` 加本地功能重构，日期：2026-08-21。

## 官方能力核对

仓库内的 RIGOL `MHO98_ProgrammingGuide_EN.pdf` 明确给出：

- `:WAVeform:SOURce` 支持 `CHANnel1`、`CHANnel2`、`CHANnel3`、`CHANnel4`；
- `:STOP` 停止示波器采集，`:RUN` 恢复运行；
- `:TRIGger:STATus?` 可返回 `STOP`，可用于确认冻结完成；
- `WAVeform:PREamble?` 返回 `XINCrement`、`XORigin`、`XREFerence` 等波形时间轴参数；
- `:SOURce<n>:PHASe` 可设置/查询 0–360° 相位，`:SOURce<n>:PHASe:SYNChronize` 可让双路 AFG 按预设频率和相位重新对齐；
- `SYSTem:DATE?` / `SYSTem:TIME?` 只有秒级字段，且手册提示存在命令响应延迟；没有每样点绝对采样时间戳命令。

因此，多通道同步采集的可靠实现是“冻结一次采集记录，再顺序下载四个通道”。SCPI 传输仍是顺序的，但四通道波形不再随传输过程更新。

## 当前实现

| 功能 | 代码路径 | 行为 |
| --- | --- | --- |
| 任意扫频 | `make_sweep_frequencies` | 线性总点数、对数点/十倍频、单频均支持 |
| 每点周期 | `cycle_duration` | 分别设置稳定周期和保存周期 M |
| AFG 设置核验 | `Instrument.configure_sweep_point` | 回读实际频率/幅度/偏置/相位，每次变频后执行硬件相位对齐，读取 SCPI 错误队列，按实际频率设置并复核时基，强制采集新帧 |
| 逐点 A/B | `load_sweep_ab_profile` | CSV 每行独立设置频率、数学峰值 A/峰峰值和偏置 B；默认 B=0 V |
| CH1–CH4 同帧 | `Instrument.acquire_channels` | 一次 STOP，确认停止，读取所选通道，恢复此前 RUN/STOP 状态 |
| 时间轴 | `extract_recent_cycles` | 校验多通道点数、X 间隔与 X 原点；生成仪器时间和频点内时间 |
| 实时 CSV | `App._append_live_csv` | 每点立即刷盘；区分请求/回读参数、捕获时刻和估算样点 UTC |
| 伯德图 | `calculate_bode` | 任意参考/响应通道的响应/参考增益与相位 |
| 停止状态机 | `App.stop_sweep` | 清理完成前保持 `sweep_stopping`，禁用重新开始 |
| 回环校准 | `calibrate_channel_loopback.py` | 同一 AFG 等长回环，拟合通道幅值比、相位差和相对固定延迟 |
| 双 AFG 相位验证 | `validate_dual_afg_phase.py` | AFG1→CH2、AFG2→CH1 往返扫频，检查每次变频后的相位对齐重复性和综合延迟 |

## 多通道物理限制

MHO98 只有 4 个模拟输入，程序允许任意选择其中 1–4 路。例如可以去掉倾角 Z，把该输入换成 AFG BNC 回读；总数不超过四路时不需要额外设备。

四通道同时开启时，仪器可能改变采样率或存储深度。程序会记录实际 `sample_interval_s`，但最终同步精度、通道间偏斜和相位误差仍须实机校验。

通道偏斜不再只依赖人工观察：回环校准程序使用同一 AFG 信号和同一冻结帧，在多个频率拟合 `相位 = 截距 - 2πf·延迟`，并输出每个通道相对参考通道的固定延迟。该结果同时包含测量线缆的延迟，因此应使用等长线缆，或直接使用正式测量线缆把整条路径一并校准。

## 时间戳结论

原实现的 `stored_timestamp = 记录开始时间 + 拼接保存时长` 已移除。新格式提供：

- `instrument_time_s`：示波器波形记录内的时间坐标；
- `point_time_s`：所保存 M 周期窗口内的相对时间；
- `capture_timestamp_utc`：确认 STOP 后立即记录的主机 UTC；
- `estimated_sample_timestamp_utc`：用相对时间反推的估算 UTC，字段名明确表示不是硬件时间戳；
- `sweep_elapsed_s`：基于主机单调时钟的实际扫频经过时间估算。

用于幅值/相位拟合的是相对时间和 AFG 回读频率，不依赖主机 UTC。

## CSV 测量语义

- `requested_frequency_hz` / `requested_amplitude_vpp` / `requested_offset_v` 是软件命令值；
- `frequency_hz` / `amplitude_vpp` / `offset_v` 是仪器回读值；
- `requested_phase_deg` / `phase_deg` 是 AFG 请求/回读相位；
- `timebase_scale_s` 是当前频点仪器实际接受的时基；
- `ch1_voltage_v`–`ch4_voltage_v` 是示波器下载的实测波形；
- 若 CH2 用 BNC 接回 AFG，CH2 列就是该物理输出的实测电压；
- 默认伯德图方向改为 CH1/CH2，即 CH2 参考、CH1 响应，也可在界面中任意选择。

## 已修复问题

- 扫频停止清理尚未完成时可快速重启，导致旧恢复命令与新配置交错；
- 扫频只支持固定 0.1–500 Hz、固定 300 点和固定 5/3 周期；
- 只支持 CH1/CH2 且在 RUN 状态分别读取；
- CSV 不区分设定值和仪器接受值；
- 合成 `stored_timestamp` 被误认为实际采样时刻；
- 绘图脚本写死作者电脑路径，且依赖清单遗漏 `pandas`；
- GUI 非数字输入异常和 LAN 非 RIGOL 响应诊断丢失。
- 扫频前示波器若为 STOP 会反复读旧帧；现在每个频点配置后明确进入 RUN，结束后恢复扫频前状态。
- 时基只写入请求值、不核对仪器档位；现在回读实际 `s/div`，覆盖 M 周期不足时自动增大。
- 变频后 AFG 相位起点不确定；现在每点写入并回读相位，再执行硬件 `PHASe:SYNChronize` 和 `*WAI`。

## 实机验证状态

已在 `RIGOL TECHNOLOGIES,MHO98,MHO9A273700214,00.01.01` 上验证：

- 原生 USBTMC 身份查询和 SCPI 错误队列；
- AFG1→CH2 的 10、100、1000、10000 Hz 自动时基闭环；
- 每点 1000 点屏幕帧、最近 3 周期截取和正弦拟合；
- CH1–CH4 一次冻结后各读取 1000 点，四路 X 间隔和 X 原点一致；
- AFG1→CH2、AFG2→CH1 在 100 kHz–10 MHz 升频/降频两轮相位对齐；每频点共 6 次，最差相位峰峰值 0.101627°，综合延迟 8.117798 ns；
- 加入相位同步后的 AFG1→CH2 100、1000、10000 Hz 正式扫频路径；
- 测试结束后 AFG 输出、频率/幅度、时基、通道显示和触发设置恢复。

实测同时发现并修复：低频屏幕帧可能暂时只返回 2 点；程序现在保持当前频率/时基连续 RUN，完整后再继续。高频若只按几个周期等待，可能仍处于配置过渡；程序增加每点至少 0.25 秒驻留。详细数据见 `HARDWARE_VALIDATION.md`。

## 仍需其他接线验证

1. `:STOP` 到实际最后采样点之间的仪器内部延迟；这决定估算 UTC 的系统误差。
2. 使用同一个 AFG 经功分器和等长线连接两个以上输入，运行 `calibrate_channel_loopback.py`，分离纯示波器通道偏斜与双 AFG/线缆综合延迟。
3. 极低/极高频率及大 M 值下，时基自动复核是否仍能取得完整窗口。
4. 50 Ω/高阻负载和高频幅度限制下，AFG 回读值与 CH2 实测 Vpp 是否一致。

## 验证命令

```bash
python -m compileall -q .
python -m unittest discover -s tests -v
git diff --check
```

自动化测试覆盖线性/对数/单频频点生成、任意周期截取、不完整屏幕帧重试、四通道冻结读取命令顺序、AFG 参数回读、通用伯德拟合、动态 CSV 列和时间字段语义。真实仪器已完成上述有限接线范围内的验收；未连接信号的通道不能据此声称幅值/相位已经校准。
