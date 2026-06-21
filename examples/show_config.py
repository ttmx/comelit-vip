#!/usr/bin/env python3
"""Log in (via stored refresh token) and print the discovered devices + config.

(`comelit config` is the installed CLI equivalent.)
"""
from comelit import Auth, CcApi, Provisioning

auth = Auth()
prov = Provisioning(CcApi(auth), auth.owner_auth_id)

print("token ok, bearer prefix:", auth.bearer[:16], "...")
macs = prov.discover_macs()
print("devices:", macs)
for mac in macs:
    c = prov.connection(mac)
    print(f"\n=== {mac}  ({c.model_id}: {c.description}) ===")
    print(f"  mqtt        : {c.mqtt_server}  auth={c.mqtt_auth_methods}")
    print(f"  stun/turn   : {c.stun_servers}")
    print(f"  http duuid  : {c.http_duuid}  role={c.http_role}")
    print(f"  viper local : {c.local_address}:{c.local_tcp_port} (tcp) / {c.local_udp_port} (udp)")
    print(f"  viper remote: {c.remote_address or '(none)'}:{c.remote_tcp_port}")
    print(f"  activation  : code={c.activation_code} sub_address={c.sub_address}")
    print(f"  sdp topic   : {c.mqtt_topic_base}")
