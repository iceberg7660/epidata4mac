#!/bin/zsh
# 双击启动 EpiData Mac 平替
cd "$(dirname "$0")"
exec /opt/miniconda3/envs/iceberg/bin/python app.py "$@"
