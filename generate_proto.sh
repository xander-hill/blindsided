#!/usr/bin/env bash
set -e

# 1. Setup Paths
ROOT="$(cd "$(dirname "$0")" && pwd)"
PROTO_DIR="$ROOT/proto"
PY_OUT_DIR="$ROOT/proto/src"
TS_OUT_DIR="$ROOT/frontend/src/proto"
VENV_PYTHON="$ROOT/venv/bin/python"
PROTO_NAME="blindsided"

# Ensure output directories exist
mkdir -p "$PY_OUT_DIR"
mkdir -p "$TS_OUT_DIR"

# Clean out old frontend files to prevent "require is not defined" leftovers
rm -rf "$TS_OUT_DIR"/*

echo "🚀 Generating gRPC code for Triple-Threat Backend (Python)..."

# 2. Python Generation (Backend)
$VENV_PYTHON -m grpc_tools.protoc \
    -I "$PROTO_DIR" \
    --python_out="$PY_OUT_DIR" \
    --pyi_out="$PY_OUT_DIR" \
    --grpc_python_out="$PY_OUT_DIR" \
    "$PROTO_DIR/${PROTO_NAME}.proto"

# 3. Apply Python Import Fixes
echo "🛠️ Patching Python imports..."
if [[ "$OSTYPE" == "darwin"* ]]; then
    sed -i '' "s/import ${PROTO_NAME}_pb2/from . import ${PROTO_NAME}_pb2/" "$PY_OUT_DIR/${PROTO_NAME}_pb2_grpc.py"
else
    sed -i "s/import ${PROTO_NAME}_pb2/from . import ${PROTO_NAME}_pb2/" "$PY_OUT_DIR/${PROTO_NAME}_pb2_grpc.py"
fi

# 4. Modern TypeScript Generation (Frontend)
# Replaced old gRPC-web with ts-proto for Vite compatibility
# 4. Modern TypeScript Generation (Frontend)
TS_PLUGIN="$ROOT/node_modules/.bin/protoc-gen-ts"

protoc -I "$PROTO_DIR" \
  --plugin=protoc-gen-ts="$TS_PLUGIN" \
  --ts_out "$TS_OUT_DIR" \
  --ts_opt generate_dependencies \
  "$PROTO_DIR/${PROTO_NAME}.proto"

# 5. Package Initialization
touch "$PY_OUT_DIR/__init__.py"

echo "✅ Success!"
echo "   - Python files in: $PY_OUT_DIR"
echo "   - TypeScript files in: $TS_OUT_DIR"