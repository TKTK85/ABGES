# 随包 PLINK 2

本目录随 ABGES 分发 Windows 64 位 PLINK `v2.0.0-a.7.1`（4 May 2026）。
程序由原项目本地副本原样复制，未修改二进制；文件大小为 38,741,902 字节。

SHA-256：`247491bfca7512e070dc99d6565e9fc56f3a52ad5afc01286016271d34c4992f`

## 版权和许可证

PLINK 2 copyright (C) 2005-2026 Shaun Purcell, Christopher Chang.
PLINK 主程序依 GPL-3.0-or-later 分发，完整许可证见 [LICENSE](LICENSE)。
该程序不提供保证，包括适销性或特定用途适用性保证。
本说明不替 ABGES 其他代码指定许可证。

## 对应源码与构建说明

本版本对应上游标签 `v2.0.0-a.7.1`，提交 `9e589f164d2c1e4861f9631724abe6f439b958c1`。
该源码中的版本和发布日期与随包程序的 `--version` 输出相符。

- [完整版本源码下载（免费，无需账号）](https://github.com/chrchang/plink-ng/archive/9e589f164d2c1e4861f9631724abe6f439b958c1.tar.gz)
- [PLINK 2 源码与所含依赖](https://github.com/chrchang/plink-ng/tree/9e589f164d2c1e4861f9631724abe6f439b958c1/2.0)
- [该版本 Windows 构建文件](https://github.com/chrchang/plink-ng/tree/9e589f164d2c1e4861f9631724abe6f439b958c1/2.0/build_win)
- [官方开发与编译说明](https://www.cog-genomics.org/plink/2.0/dev)

源码中的各依赖保留各自许可证，请一并遵守。源码通过上述固定提交链接提供，
不是随时间变化的 master 分支。后续更换 EXE 时应同步核对对应源码、许可证、
本说明及 `tools/ENGINE_MANIFEST.json`；维护者应持续保证源码下载可用。

## 使用范围

本目录不附带育种数据。真实 ABGES 分析通过 Harness 和 PLINK 适配器调用本程序。
HIBLUP 引擎同样随包分发（见 [../HIBLUP/README.md](../HIBLUP/README.md)）；
随包引擎不表示整个分析环境已部署完成，仍需通过 Harness 校验和调用。
