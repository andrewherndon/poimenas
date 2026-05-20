#!/bin/bash
# Run once on the RPi to set up dnsmasq for Heimdall.
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

echo "Installing dnsmasq..."
sudo apt-get update -qq
sudo apt-get install -y dnsmasq

echo "Disabling dnsmasq default config..."
sudo mkdir -p /etc/dnsmasq.d
sudo bash -c 'echo "conf-dir=/etc/dnsmasq.d/,*.conf" > /etc/dnsmasq.conf'

echo "Writing initial (unlocked) DNS config..."
ROUTER_IP=${ROUTER_IP:-192.168.1.1}
echo -e "# heimdall - unlocked\nserver=$ROUTER_IP\ncache-size=1000" \
    | sudo tee /etc/dnsmasq.d/heimdall.conf > /dev/null

echo "Setting up sudoers rule..."
chmod +x "$SCRIPT_DIR/apply_dns.sh"
echo "$(whoami) ALL=(ALL) NOPASSWD: $SCRIPT_DIR/apply_dns.sh" \
    | sudo tee /etc/sudoers.d/heimdall > /dev/null
sudo chmod 440 /etc/sudoers.d/heimdall

echo "Enabling and starting dnsmasq..."
sudo systemctl enable dnsmasq
sudo systemctl restart dnsmasq

echo ""
echo "Done. Set ROUTER_IP in server/.env (default: 192.168.1.1), then restart the heimdall server."
echo "On the Windows machine, set DNS to this RPi's local IP."
