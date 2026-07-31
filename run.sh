#!/bin/bash
# ポケカ先着枠の空き監視を起動する。停止は Ctrl-C。
# caffeinate で監視中は Mac がスリープしないようにしている。
cd "$(dirname "$0")" || exit 1
exec caffeinate -i python3 watch.py "$@"
