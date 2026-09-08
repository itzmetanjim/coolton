#!/bin/bash
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [ -f "$SCRIPT_DIR/codechannelinternal.sh" ]; then
    response=$(bash "$SCRIPT_DIR/codechannelinternal.sh" "$@")
    channel=$(echo "$response"|jq -r '.channel.id')
    if echo "$response" | jq -e '.ok == true' > /dev/null; then
      echo "ok"
      echo "$channel"
    else
      echo "error"
      echo "$response"
    fi
else
    echo "error"
    echo "codechannelinternal.sh doesnt exist"
fi
