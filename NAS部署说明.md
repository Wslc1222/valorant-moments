# 无畏时刻 NAS 版

本分支在原版 Windows 本地视频墙基础上，增加 Docker/NAS 运行入口；保留原版页面与收藏、标签，不使用 AI、不联网上传录像。

## 数据流

```text
Windows 无畏时刻原始目录
  └─ nas-video-archiver 0.2.0
      ├─ 录像、图片：保留相对路径
      └─ 原始加密对局库 + manifest：保留历史快照
                  ↓ SMB
NAS 录像共享目录（网页容器只读）
                  ↓ 校验 → 解密 → 路径转换 → 合并历史
Docker 网页服务（每 120 秒重扫）
  └─ 收藏、标签、索引、配置：独立持久化目录
```

只上传 MP4 时仍可浏览和播放。英雄、地图、真实比赛日期、比分和 KDA 需要客户端原始数据库；缺少这些字段不意味着视频丢失。上传元数据后，下一次扫描自动补充分类。

## Compose 部署

1. 在 NAS 上建立独立程序目录、独立 data 目录，不与录像目录互相嵌套。
2. 获取本仓库源码，将 compose.example.yaml 复制为你自己使用的 docker-compose.yml，并设置 VM_MEDIA_PATH、VM_DATA_PATH、VM_BIND_ADDRESS、VM_HTTP_PORT、PUID、PGID。实际路径和凭据不提交到仓库。
3. data 目录必须能被 VM_UID:VM_GID 写入；录像目录给该用户读取/遍历权限。不要为此给整个 NAS 开放写权限。
4. 用 Docker Compose 构建并启动服务，浏览器访问所设置的 NAS 局域网地址和端口。

示例命令（在源码目录，需按自己的 NAS 修改路径）：

```bash
export VM_MEDIA_PATH=/your/nas/highlights
export VM_DATA_PATH=/your/nas/apps/valorant-moments/data
export VM_BIND_ADDRESS=192.168.1.100
export VM_HTTP_PORT=8765
export PUID=1000
export PGID=1000
docker compose -f compose.example.yaml up -d --build
```

不要将宿主机根目录、Docker socket、其他应用数据目录挂载给此容器。不需要 privileged、host network、开放 SSH、重启 NAS 或修改其它服务。持久化目录不要使用只存在于容器内部的临时路径。

## 环境变量

| 变量 | 默认值 | 用途 |
|---|---|---|
| VM_MEDIA_ROOT | /media | 容器内只读录像根，网页不能切换到其它位置 |
| VM_DATA_DIR | /data | 可写持久化状态根，与媒体不能互相嵌套 |
| VM_SCAN_INTERVAL | 120 | 自动扫描间隔，秒 |
| VM_NAS_MODE | 1 | NAS 安全数据源模式，运行入口强制启用 |
| TZ | Asia/Shanghai | 日期显示时区 |

入口为 nas_server.py，监听 0.0.0.0:8765。单个 Uvicorn worker，避免多个进程同时覆盖 JSON 状态。启动会构建索引；周期扫描失败时不应把已有正常索引换成失败结果。

## 与 Windows 0.2.0 的归档协议

媒体根下的结构：

```text
<录像/封面的相对路径>
.valorant-moments/snapshots/<来源键32hex>/<数据库SHA256>/
  ├─ WonderfulDb/<完整原账号文件名>  # 或 WeGameWonderfulDb
  └─ manifest.json
```

- manifest schema：valorant-moments-archive/v1。
- 只有完成标记存在、数据库大小/哈希校验通过的快照才能导入；.partial 不导入。
- 对局数据库按原始文件名派生 AES 密钥，复用原项目的完整客户端与 WeGame 解密规则。
- source_root / resolved_source_root 为原始 Windows 路径；NAS 按 Windows 路径语义匹配根，再映射到当前媒体根。
- 保留并合并全部有效历史快照，避免客户端清理旧条目后 NAS 历史对局消失。
- 视频身份按媒体根内的实际相对路径稳定生成；文件时间变化或后续补充元数据不应丢收藏/标签。
- 数据库最大 64 MiB，manifest 最大 64 KiB；损坏或越界快照记录诊断，不尝试猜路径或读取 NAS 其它文件。

数据库内包含账号、对局和本机路径信息，不要公开分享原始库或归档目录。客户端更新后可能需要调整逆向解密规则；“已经复制到 NAS”不等于“解密已经兼容当前客户端”。

## 安全边界

- 录像挂载只读；API 同时禁止删除、永久清空、还原、导出和损坏文件清理等媒体写操作。
- 收藏、标签、显示名称和隐藏仅修改独立 data 目录，不修改录像。
- 不提供上传接口、任意命令执行接口或切换到任意文件系统目录的入口。
- 媒体和快照不跟随符号链接；请放入真实文件，不用链接指向 NAS 其它位置。
- 原始元数据文件、状态 JSON、应用源码不得作为视频/封面返回。
- 当前是无需账号的家庭局域网服务，带浏览器跨站写入防护，**不是可直接暴露公网的多用户应用**。外网访问先使用 VPN，或另加有身份验证的 HTTPS 反向代理。
- 官方英雄、地图图片仍可能由浏览器从官方 CDN 获取；原始录像和加密对局库不发送到该 CDN。

## 验证与维护

本地合成测试使用 pytest，真实 NAS 验收另行执行。/health 用于容器进程及扫描状态检查；它不证明某个真实客户端数据库一定能解密，也不证明浏览器支持原片编码。

更新前先备份 data 目录；更新容器时保留媒体及 data 挂载。NAS 快照或第二份备份仍有必要，不能用网页索引代替视频备份。当前不提供自动转码；浏览器不支持的原片编码需另行转换。
