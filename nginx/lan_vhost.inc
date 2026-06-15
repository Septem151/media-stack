# Shared listen/TLS/ACL for the LAN-only <svc>.lan vhosts. Served on :80 and :443
# with the self-signed LAN cert; restricted to the LAN + Docker ranges (172.16/12
# also covers on-box loopback, which Docker presents as the bridge gateway). The
# router-forwarded public traffic on :80/:443 is denied. Rendered with ${LAN_SUBNET}.
listen 80;
listen 443 ssl;
http2 on;
ssl_certificate     /etc/nginx/selfsigned/lan.crt;
ssl_certificate_key /etc/nginx/selfsigned/lan.key;
allow ${LAN_SUBNET};
allow 172.16.0.0/12;
deny all;
