#!/bin/bash
# Slack CLI deploy hook - updates .env with deployed app tokens and restarts service
set -e

if [ -n "$SLACK_BOT_TOKEN" ]; then
    echo "Updating SLACK_BOT_TOKEN in .env..."
    sed -i "s|^SLACK_BOT_TOKEN=.*|SLACK_BOT_TOKEN=$SLACK_BOT_TOKEN|" /home/tanjim/coolton-private/.env
fi

if [ -n "$SLACK_APP_TOKEN" ]; then
    echo "Updating SLACK_APP_TOKEN in .env..."
    sed -i "s|^SLACK_APP_TOKEN=.*|SLACK_APP_TOKEN=$SLACK_APP_TOKEN|" /home/tanjim/coolton-private/.env
fi

echo "Restarting coolton service..."
systemctl restart coolton
echo "Done!"
