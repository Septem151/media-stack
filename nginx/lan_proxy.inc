# Shared body for the LAN-only <svc>.lan vhosts (see default.conf). Each location
# sets `$upstream` and calls `proxy_pass $upstream`; the Docker resolver resolves
# upstreams at request time so nginx starts/reloads even if a container is down.
resolver 127.0.0.11 valid=30s ipv6=off;
include /etc/nginx/conf.d/proxy_headers.inc;
