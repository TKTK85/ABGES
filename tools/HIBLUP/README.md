# 随包 HIBLUP

本目录随 ABGES 分发 Windows 64 位 HIBLUP `v1.7.0` 主程序及其运行库 DLL。
程序由官方发布包原样复制，未修改二进制；`hiblup.exe` 大小为 32,205,312 字节。

SHA-256：`8a6cce4e0d2fa0d73d0032212a527214c2581a00601f2ab25c56b1752f2941ac`

文件清单（与 `tools/ENGINE_MANIFEST.json` 对应）：

- `hiblup.exe`：主程序
- `libiomp5md.dll`、`vcruntime140.dll`、`vcruntime140_1.dll`：运行库
- `api-ms-win-crt-*.dll`：Universal C 运行库组件

## 来源与许可

- 官方网站与下载：https://www.hiblup.com/
- HIBLUP 版权归其开发方所有，随包分发仅用于本项目配套计算引擎；
  使用与再分发请遵守官方许可条款。
- 后续更换 EXE 或 DLL 时，应同步更新本说明与 `tools/ENGINE_MANIFEST.json`
  中的版本、大小和 SHA-256。

## 使用范围

本目录不附带育种数据。真实 ABGES 分析通过 Harness 和 HIBLUP 适配器
（`hiblup_tool.py`）调用本程序，运行前由 Harness 校验校验和。
运行产生的 `hiblup.log` 等日志不随仓库分发。
