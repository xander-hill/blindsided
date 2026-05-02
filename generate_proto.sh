#!/usr/bin/env bash
set -e

# Get the directory where THIS script is located
ROOT="$(cd "$(dirname "$0")" && pwd)"

# Define paths relative to the script location
PROTO_DIR="$ROOT/proto/src"
OUT_DIR="$ROOT/proto/src"
VENV_PYTHON="$ROOT/venv/bin/python"

# VARIABLE: Change this if you rename the .proto file
PROTO_NAME="blindsided" 

echo "Generating gRPC code from ${PROTO_NAME}.proto..."

# Run the compiler using the venv python
$VENV_PYTHON -m grpc_tools.protoc \
    -I "$PROTO_DIR" \
    --python_out="$OUT_DIR" \
    --pyi_out="$OUT_DIR" \
    --grpc_python_out="$OUT_DIR" \
    "$PROTO_DIR/${PROTO_NAME}.proto"

# Ensure the directory is a Python package
touch "$OUT_DIR/__init__.py"

echo "Applying import fixes for ${PROTO_NAME}_pb2_grpc.py..."

# Fix the gRPC import bug (standard for Mac/Linux)
# This handles the 'import X_pb2' -> 'from . import X_pb2' fix
if [[ "$OSTYPE" == "darwin"* ]]; then
    sed -i '' "s/import ${PROTO_NAME}_pb2/from . import ${PROTO_NAME}_pb2/" "$OUT_DIR/${PROTO_NAME}_pb2_grpc.py"
else
    sed -i "s/import ${PROTO_NAME}_pb2/from . import ${PROTO_NAME}_pb2/" "$OUT_DIR/${PROTO_NAME}_pb2_grpc.py"
fi

# Add grpc.experimental import if missing
if ! grep -q '^import grpc.experimental$' "$OUT_DIR/${PROTO_NAME}_pb2_grpc.py"; then
    if [[ "$OSTYPE" == "darwin"* ]]; then
        sed -i '' '/^import grpc$/a\
import grpc.experimental
' "$OUT_DIR/${PROTO_NAME}_pb2_grpc.py"
    else
        sed -i '/^import grpc$/a import grpc.experimental' "$OUT_DIR/${PROTO_NAME}_pb2_grpc.py"
    fi
fi

echo "Success! Files generated in $OUT_DIR"