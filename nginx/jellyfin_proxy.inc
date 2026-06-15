# Jellyfin reverse-proxy body — included by the public and LAN default_server vhosts.
proxy_pass http://${LAN_IP}:8096;
include /etc/nginx/conf.d/proxy_headers.inc;
