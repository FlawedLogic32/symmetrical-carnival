from netmapper.models import (ArpEntry, DiscoveryRun, FdbEntry, Host, Interface, Neighbor,
                              SnmpDevice)
from netmapper.oui import VendorLookup
from netmapper.topology import Topology, build_topology, render_tree

R_MAC, C_MAC, A_MAC, B_MAC = ("00:00:0a:00:00:01", "00:00:0c:00:00:02",
                              "00:00:0a:00:00:03", "00:00:0b:00:00:04")
PHONE, PC_BEHIND_PHONE = "00:11:22:33:44:55", "00:50:56:00:00:10"
UNMANAGED = ["a4:5e:60:00:00:01", "a4:5e:60:00:00:02", "a4:5e:60:00:00:03"]
H1, H5, H6 = "3c:52:82:00:00:01", "3c:52:82:00:00:05", "3c:52:82:00:00:06"
AP, WIFI_CLIENT = "00:0b:86:00:00:aa", "f0:18:98:00:00:01"
LONELY = "3c:52:82:00:00:99"


def fdb(port, *macs, vlan=1):
    return [FdbEntry(mac=m, port=port, vlan=vlan) for m in macs]


def office() -> DiscoveryRun:
    """router -- core -- access -- edge (edge has no LLDP), plus a phone, an AP,
    an unmanaged switch and some hosts."""
    router = SnmpDevice(
        ip="192.168.1.1", sys_name="gw", interfaces=[Interface(1, "ge-0/0/1", R_MAC)],
        neighbors=[Neighbor("lldp", "ge-0/0/1", "core-sw.office.local", "Gi1/0/24", C_MAC,
                            None, None, ["bridge", "router"])],
        arp=[ArpEntry("192.168.1.10", H1), ArpEntry("192.168.1.11", UNMANAGED[0]),
             ArpEntry("192.168.1.1", R_MAC)],
    )
    core = SnmpDevice(
        ip="192.168.1.2", sys_name="core-sw", chassis_id=C_MAC,
        interfaces=[Interface(i, f"Gi1/0/{i}") for i in (1, 2, 3, 4, 24)],
        neighbors=[
            Neighbor("lldp", "Gi1/0/24", "gw", "ge-0/0/1", R_MAC, "192.168.1.1", None, ["router"]),
            Neighbor("lldp", "Gi1/0/1", "access-sw", "24", A_MAC, "192.168.1.3", None, ["bridge"]),
            Neighbor("cdp", "Gi1/0/2", "SEP001122334455", "Port 1", None, "192.168.1.40",
                     "Cisco IP Phone", ["host", "telephone"]),
        ],
        fdb=fdb("Gi1/0/24", R_MAC) + fdb("Gi1/0/1", A_MAC, B_MAC, H5, H6, AP, WIFI_CLIENT)
        + fdb("Gi1/0/2", PHONE, PC_BEHIND_PHONE) + fdb("Gi1/0/3", *UNMANAGED) + fdb("Gi1/0/4", H1),
    )
    access = SnmpDevice(
        ip="192.168.1.3", sys_name="access-sw", chassis_id=A_MAC,
        neighbors=[Neighbor("lldp", "5", "office-ap", "eth0", AP, None, None, ["wlan-ap"])],
        fdb=fdb("24", C_MAC, R_MAC, H1) + fdb("2", H5) + fdb("8", B_MAC, H6)
        + fdb("5", AP, WIFI_CLIENT),
    )
    edge = SnmpDevice(  # no LLDP at all
        ip="192.168.1.4", sys_name="edge-sw", chassis_id=B_MAC,
        fdb=fdb("1", A_MAC, C_MAC, R_MAC) + fdb("3", H6),
    )
    hosts = [Host(None, m, ["fdb"]) for m in
             [PHONE, PC_BEHIND_PHONE, *UNMANAGED, H1, H5, H6, AP, WIFI_CLIENT, R_MAC, A_MAC]]
    hosts.append(Host("192.168.1.99", LONELY, ["arp-cache"]))
    return DiscoveryRun(started_at="2026-10-22T16:00:00+00:00",
                        devices=[router, core, access, edge], hosts=hosts)


def edges(topo: Topology) -> set[tuple[str, str]]:
    label = {n.id: n.label for n in topo.nodes}
    return {tuple(sorted((label[lk.source], label[lk.target]))) for lk in topo.links}


def test_office_topology():
    topo = build_topology(office())
    kinds = {n.label: n.kind for n in topo.nodes}
    assert kinds["gw"] == "router"
    assert kinds["core-sw"] == "switch"
    assert kinds["access-sw"] == "switch"
    assert kinds["edge-sw"] == "switch"
    assert kinds["office-ap"] == "access-point"
    assert kinds["SEP001122334455"] == "phone"

    e = edges(topo)
    ums = next(n.label for n in topo.nodes if n.kind == "unmanaged-switch")
    assert ums == "Unmanaged switch (core-sw Gi1/0/3)"
    expected = {
        ("core-sw", "gw"),
        ("access-sw", "core-sw"),
        ("SEP001122334455", "core-sw"),
        ("00:50:56:00:00:10", "SEP001122334455"),  # PC plugged into the phone
        ("core-sw", ums),
        ("192.168.1.11", ums), (ums, UNMANAGED[1]), (ums, UNMANAGED[2]),
        ("192.168.1.10", "core-sw"),
        ("access-sw", "edge-sw"),  # from MAC tables, no LLDP
        (H5, "access-sw"),
        (H6, "edge-sw"),
        ("access-sw", "office-ap"),
        (WIFI_CLIENT, "office-ap"),
        ("192.168.1.99", "Location unknown"),
    }
    assert e == {tuple(sorted(x)) for x in expected}

    link = next(lk for lk in topo.links if {topo.node(lk.source).label, topo.node(lk.target).label}
                == {"access-sw", "edge-sw"})
    assert link.inferred and link.evidence == "mac-table"
    ports = {topo.node(link.source).label: link.source_port,
             topo.node(link.target).label: link.target_port}
    assert ports == {"access-sw": "8", "edge-sw": "1"}

    # LLDP seen from both ends fills in both port names.
    gw_link = next(lk for lk in topo.links if {topo.node(lk.source).label,
                                                topo.node(lk.target).label} == {"gw", "core-sw"})
    assert {gw_link.source_port, gw_link.target_port} == {"ge-0/0/1", "Gi1/0/24"}
    assert any("not in any switch MAC table" in n for n in topo.notes)


def test_router_ip_and_phone_ip():
    topo = build_topology(office())
    phone = next(n for n in topo.nodes if n.kind == "phone")
    assert phone.ip == "192.168.1.40"
    assert phone.mac == PHONE


def test_arp_only_run_puts_everything_in_unknown_segment():
    run = DiscoveryRun(started_at="t", hosts=[Host("10.0.0.1", "00:00:0c:00:00:01", ["arp-cache"]),
                                              Host("10.0.0.2", "00:00:0c:00:00:02", ["arp-cache"])])
    topo = build_topology(run)
    assert len(topo.links) == 2
    assert all(lk.source == "segment:unknown" for lk in topo.links)
    assert "no managed switches" in topo.notes[0]


def test_vendors_and_names():
    vendors = VendorLookup({"3c5282": "Hewlett Packard"})
    topo = build_topology(office(), vendors, resolve=lambda ips: {"192.168.1.10": "printer-1"})
    h1 = next(n for n in topo.nodes if n.mac == H1)
    assert (h1.label, h1.vendor) == ("printer-1", "Hewlett Packard")
    rand = vendors.vendor("da:00:00:00:00:01")
    assert rand.startswith("Private")


def test_round_trip_and_tree():
    topo = build_topology(office())
    again = Topology.from_dict(topo.to_dict())
    assert again == topo
    tree = render_tree(topo)
    assert tree.splitlines()[0].startswith("gw (router")
    assert "[Gi1/0/24] -> core-sw" not in tree  # port shown is the parent's port
    assert "[ge-0/0/1] -> core-sw" in tree
    assert "[8] -> ~ edge-sw" in tree


def test_oui_csv(tmp_path):
    path = tmp_path / "oui.csv"
    path.write_text('Registry,Assignment,Organization Name,Organization Address\n'
                    'MA-L,3C5282,Hewlett Packard,"11445 Compaq Center Drive Houston US 77070"\n'
                    'MA-L,000B86,"Aruba, a Hewlett Packard Enterprise Company",somewhere\n',
                    encoding="utf-8")
    vendors = VendorLookup.from_csv(path)
    assert vendors.vendor(H1) == "Hewlett Packard"
    assert vendors.vendor(AP) == "Aruba, a Hewlett Packard Enterprise Company"
    assert vendors.vendor("00:00:00:00:00:01") is None
    assert len(VendorLookup.load_if_present(tmp_path / "missing.csv")) == 0


def test_tree_starts_at_queried_switch_when_there_is_no_router():
    from fake_snmp import FakeSnmp
    from test_snmp_device import core_switch

    from netmapper.collectors.snmp_device import collect_device

    dev = collect_device(FakeSnmp(core_switch()), "192.168.1.2")
    hosts = [Host(None, f.mac, ["fdb"]) for f in dev.fdb]
    tree = render_tree(build_topology(DiscoveryRun(started_at="t", devices=[dev], hosts=hosts)))
    # The inferred unmanaged switch has more links, but the real switch comes first.
    assert tree.startswith("core-sw (switch")


VM_A, VM_B, VM_C = "00:50:56:aa:00:01", "00:15:5d:aa:00:02", "52:54:00:aa:00:03"
SERVER = "3c:52:82:00:00:50"


def one_port_run(*macs) -> DiscoveryRun:
    sw = SnmpDevice(ip="10.0.0.2", sys_name="sw", chassis_id=C_MAC, fdb=fdb("7", *macs))
    return DiscoveryRun(started_at="t", devices=[sw], hosts=[Host(None, m, ["fdb"]) for m in macs])


def test_vms_behind_a_physical_server_make_it_a_vm_host():
    topo = build_topology(one_port_run(SERVER, VM_A, VM_B))
    kinds = {n.mac: n.kind for n in topo.nodes}
    assert kinds[SERVER] == "vm-host"
    assert not any(n.kind == "unmanaged-switch" for n in topo.nodes)
    assert edges(topo) == {tuple(sorted(e)) for e in [("sw", SERVER), (SERVER, VM_A), (SERVER, VM_B)]}
    link = next(lk for lk in topo.links if topo.node(lk.target).mac == SERVER)
    assert link.source_port == "7" and not link.inferred


def test_all_virtual_macs_infer_a_vm_host():
    topo = build_topology(one_port_run(VM_A, VM_B, VM_C))
    vm_host = next(n for n in topo.nodes if n.kind == "vm-host")
    assert vm_host.inferred and vm_host.label == "VM host (sw 7)"
    assert edges(topo) == {("VM host (sw 7)", "sw")} | {
        tuple(sorted(("VM host (sw 7)", m))) for m in (VM_A, VM_B, VM_C)}
    vendors = {n.mac: n.vendor for n in topo.nodes if n.mac}
    assert vendors[VM_A] == "VMware virtual NIC"
    assert vendors[VM_C] == "QEMU/KVM virtual NIC"


def test_two_physical_macs_still_mean_an_unmanaged_switch():
    topo = build_topology(one_port_run(SERVER, H1, VM_A))
    assert any(n.kind == "unmanaged-switch" for n in topo.nodes)
    assert not any(n.kind == "vm-host" for n in topo.nodes)
