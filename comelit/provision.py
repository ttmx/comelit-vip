"""Provisioning: read the per-device connection config from the cloud JSON file store.

Data model (tag = blob type):
  <MAC>            tag 'vip_connection'  -> servers/ports (mqtt, stun, viper-server local/remote)
  <MAC>.0.<n>      tag 'vip_activation'  -> per-app: activation-code, mqtt sdp topic base, sub-address

Services used:
  jfs/lst   list blobs (name/tag regex filter)
  jfs/get   fetch one blob by name (+ownerAuthId)
"""
from __future__ import annotations
from dataclasses import dataclass, field
from .api import CcApi


@dataclass
class VipConnection:
    mac: str
    model_id: str
    description: str
    mqtt_server: str                 # e.g. tls://hub-vc-generic.cloud.comelitgroup.com:443
    mqtt_auth_methods: list
    stun_servers: list
    http_duuid: str
    http_role: str
    local_address: str               # panel LAN ip (viper direct), e.g. 192.168.55.4
    local_tcp_port: int
    local_udp_port: int
    remote_address: str
    remote_tcp_port: int
    remote_udp_port: int
    # filled from the vip_activation blob:
    activation_code: str = ""
    mqtt_topic_base: str = ""        # MSVF/<MAC>/vip/SBxxxxxx/sdp
    sub_address: int = 0
    raw: dict = field(default_factory=dict)


class Provisioning:
    def __init__(self, api: CcApi, owner_auth_id: str):
        self.api = api
        self.owner = owner_auth_id

    def list_blobs(self, name_regex: str, tag_regex: str | None = None) -> list:
        body = {"name": {"value": name_regex, "regex": True, "caseInsensitive": True}}
        if tag_regex:
            body["tag"] = {"value": tag_regex, "regex": True, "caseInsensitive": True}
        return self.api.call("jfs/lst", body)["entries"]

    def get_blob(self, name: str) -> dict:
        return self.api.call("jfs/get", {"name": name, "ownerAuthId": self.owner})

    def discover_macs(self) -> list:
        """Return the device MACs visible to this account (from vip_activation entries)."""
        macs = set()
        for e in self.list_blobs(".*", "VIP_ACTIVATION"):
            macs.add(e["name"].split(".")[0])
        return sorted(macs)

    def connection(self, mac: str, sub_address: int | None = None) -> VipConnection:
        conn = self.get_blob(mac)
        c = conn["content"]
        vp, vs = c["viper-p2p"], c["viper-server"]
        meta = conn.get("metadata", {})
        out = VipConnection(
            mac=mac,
            model_id=meta.get("model-id", ""),
            description=meta.get("description", ""),
            mqtt_server=vp["mqtt"]["server"],
            mqtt_auth_methods=vp["mqtt"].get("auth", {}).get("method", []),
            stun_servers=vp.get("stun", {}).get("server", []),
            http_duuid=vp.get("http", {}).get("duuid", ""),
            http_role=vp.get("http", {}).get("role", ""),
            local_address=vs.get("local-address", ""),
            local_tcp_port=vs.get("local-tcp-port", 64100),
            local_udp_port=vs.get("local-udp-port", 64100),
            remote_address=vs.get("remote-address", ""),
            remote_tcp_port=vs.get("remote-tcp-port", 64100),
            remote_udp_port=vs.get("remote-udp-port", 64100),
            raw=conn,
        )
        # pick an activation blob (our app instance) for the sdp topic base
        acts = self.list_blobs(f"{mac}\\..*", "VIP_ACTIVATION")
        chosen = None
        for e in acts:
            blob = self.get_blob(e["name"])
            content = blob.get("content", {})
            sa = content.get("viper-p2p", {}).get("sub-address")
            if sub_address is None or sa == sub_address:
                chosen = (e["name"], content)
                if sub_address is None:
                    break
        if chosen:
            content = chosen[1]
            out.activation_code = content.get("activation-code", "")
            out.mqtt_topic_base = content.get("viper-p2p", {}).get("mqtt", {}).get("base", "")
            out.sub_address = content.get("viper-p2p", {}).get("sub-address", 0)
        return out
