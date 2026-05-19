#!/bin/sh
set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
SOURCE="$SCRIPT_DIR/codex-provider"
DEST_DIR="${HOME}/.local/bin"
DEST="${DEST_DIR}/codex-provider"

if [ ! -f "$SOURCE" ]; then
  echo "错误：install.sh 旁边没有 codex-provider" >&2
  exit 1
fi

mkdir -p "$DEST_DIR"
cp "$SOURCE" "$DEST"
chmod 755 "$DEST"

echo "已安装：$DEST"
case ":$PATH:" in
  *":$DEST_DIR:"*) ;;
  *)
    echo ""
    echo "如果终端找不到 codex-provider，把这一行加入 shell profile："
    echo "  export PATH=\"\$HOME/.local/bin:\$PATH\""
    ;;
esac

echo ""
echo "运行："
echo "  codex-provider"
