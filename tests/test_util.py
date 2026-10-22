from netmapper.util import is_unicast_mac, normalize_mac, octets_to_ipv4, octets_to_text


def test_normalize_mac_forms():
    expected = "aa:bb:cc:dd:ee:0f"
    for raw in ["aa-bb-cc-dd-ee-0f", "AA:BB:CC:DD:EE:0F", "aabb.ccdd.ee0f", "aabbccddee0f",
                bytes.fromhex("aabbccddee0f")]:
        assert normalize_mac(raw) == expected


def test_normalize_mac_rejects_non_macs():
    for raw in ["switch-1", "aa-bb-cc", "", "Gi1/0/1", b"\x01\x02"]:
        assert normalize_mac(raw) is None


def test_unicast():
    assert is_unicast_mac("00:11:22:33:44:55")
    assert not is_unicast_mac("ff:ff:ff:ff:ff:ff")
    assert not is_unicast_mac("01:00:5e:00:00:fb")
    assert not is_unicast_mac("00:00:00:00:00:00")


def test_octets():
    assert octets_to_text(b"core-sw\x00") == "core-sw"
    assert octets_to_text(bytes.fromhex("001122334455")) == "00:11:22:33:44:55"
    assert octets_to_ipv4(bytes([10, 0, 0, 1])) == "10.0.0.1"
