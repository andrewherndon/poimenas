#!/bin/bash
# Called via sudo by the FastAPI server when lock state changes.
cp "$(dirname "$0")/dnsmasq_current.conf" /etc/dnsmasq.d/poimenas.conf
systemctl restart dnsmasq
