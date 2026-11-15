#!/usr/bin/env sh
# ============================================================================
# 康伴（SilverMate）· 本地 HTTPS 开发证书生成（Day 10 · A3-b / S7，dev-A）
#
# 用途：干净主机 clone 后，`docker compose up` 前先跑一次本脚本，生成
#   deploy/certs/ca.pem     本地开发根 CA（导入系统受信任根后浏览器才不告警）
#   deploy/certs/ca.key     CA 私钥（**绝不进 git、绝不进镜像**）
#   deploy/certs/server.pem nginx 443 用服务端证书（由上面的 CA 签发）
#   deploy/certs/server.key 服务端私钥（同上）
#   deploy/certs/server.csr / ca.srl：签发中间产物，可删
#
# 为什么是「CA + 签发」而不是 S6 的裸自签叶证书：浏览器只信任「由受信任根
# 签发」的证书；把裸自签叶证书塞进根存储会因缺 CA:TRUE 被 Chrome 拒绝。
#
# 用法：
#   sh deploy/gen-dev-cert.sh                      # 默认 SAN：localhost / 127.0.0.1 / host.docker.internal
#   SILVERMATE_LAN_IP=10.55.50.233 sh deploy/gen-dev-cert.sh   # 追加局域网 IP（多机/手机热点访问）
#   sh deploy/gen-dev-cert.sh --force             # 连根 CA 一起重建（旧 CA 需从系统根存储移除）
#
# 生成后按平台导入根 CA（**受控信任**，各人自己执行，脚本不改系统设置）：
#   Windows : certutil -addstore -user Root deploy\certs\ca.pem
#   macOS   : sudo security add-trusted-cert -d -r trustRoot -k /Library/Keychains/System.keychain deploy/certs/ca.pem
#   Linux   : sudo cp deploy/certs/ca.pem /usr/local/share/ca-certificates/silvermate-dev.crt && sudo update-ca-certificates
# 撤销信任（Windows）：certutil -delstore -user Root "SilverMate Local Dev CA"
#
# 验证（不带 -k）：curl --noproxy '*' -I https://localhost/pad/    → 200
# 证书与私钥不进 git：根 .gitignore 的 `certs/` / `*.pem` / `*.key` 已覆盖。
# ============================================================================

set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
CERT_DIR="$SCRIPT_DIR/certs"
FORCE=0
[ "${1:-}" = "--force" ] && FORCE=1

# Windows（Git Bash + 原生 openssl）不认 MSYS 的 /c/... 绝对路径；切到脚本目录后
# 一律用相对路径 certs/xxx，跨平台都能被 openssl 正确解析。
cd "$SCRIPT_DIR"
mkdir -p certs

# 可选：把宿主机局域网 IP 加进 SAN，供 pad 从另一台设备（手机热点）访问
SAN="DNS:localhost,DNS:host.docker.internal,IP:127.0.0.1,IP:::1"
if [ -n "${SILVERMATE_LAN_IP:-}" ]; then
    case "$SILVERMATE_LAN_IP" in
        *:*) SAN="$SAN,IP:$SILVERMATE_LAN_IP" ;;
        *)   SAN="$SAN,IP:$SILVERMATE_LAN_IP" ;;
    esac
fi

# 根 CA 默认复用：避免每次重跑都往系统根存储里堆一张新 CA
if [ "$FORCE" -eq 1 ] || [ ! -f "certs/ca.pem" ] || [ ! -f "certs/ca.key" ]; then
    echo "[cert] 生成本地开发根 CA（3650 天）"
    MSYS_NO_PATHCONV=1 openssl req -x509 -newkey rsa:2048 -sha256 -nodes -days 3650 \
        -keyout "certs/ca.key" -out "certs/ca.pem" \
        -subj "/C=CN/O=SilverMate Dev/CN=SilverMate Local Dev CA" \
        -addext "basicConstraints=critical,CA:TRUE" \
        -addext "keyUsage=critical,keyCertSign,cRLSign" 2>/dev/null
else
    echo "[cert] 复用已有根 CA（--force 可重建）"
fi

echo "[cert] 签发服务端证书（365 天，SAN=$SAN）"
EXT_FILE="certs/.server.ext.$$"
trap 'rm -f "$EXT_FILE" certs/server.csr' EXIT
printf '%s\n' \
    "subjectAltName=$SAN" \
    "basicConstraints=critical,CA:FALSE" \
    "keyUsage=critical,digitalSignature,keyEncipherment" \
    "extendedKeyUsage=serverAuth" > "$EXT_FILE"

MSYS_NO_PATHCONV=1 openssl req -new -newkey rsa:2048 -sha256 -nodes \
    -keyout "certs/server.key" -out "certs/server.csr" \
    -subj "/C=CN/O=SilverMate Dev/CN=localhost" 2>/dev/null

MSYS_NO_PATHCONV=1 openssl x509 -req -in "certs/server.csr" \
    -CA "certs/ca.pem" -CAkey "certs/ca.key" -CAcreateserial \
    -out "certs/server.pem" -days 365 -sha256 -extfile "$EXT_FILE" 2>/dev/null

echo "[cert] 完成：certs/{ca.pem,server.pem,server.key}"
MSYS_NO_PATHCONV=1 openssl x509 -in "certs/server.pem" -noout -subject -issuer -ext subjectAltName
echo "[cert] 下一步：把 ca.pem 导入系统受信任根（见本脚本头部说明），再启动 compose"
