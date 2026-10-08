from conftest import BUNDLES
from harness.contracts import Kind, Manifest, Permissions, permissions_diff


def test_manifest_round_trip(tmp_path):
    m = Manifest.load(BUNDLES / "echo_ok")
    assert m.kind is Kind.CODE_TOOL and m.ref == "echo@v1"
    m.dump(tmp_path / "manifest.yaml")
    assert Manifest.load(tmp_path) == m


def test_permissions_diff():
    old = Permissions(network=["a.cz", "b.cz"])
    new = Permissions(network=["b.cz", "c.cz"], filesystem="registry_ro")
    assert permissions_diff(old, new) == {
        "added": {"network": ["c.cz"], "filesystem": ["registry_ro"]},
        "removed": {"network": ["a.cz"], "filesystem": ["none"]},
    }
    assert permissions_diff(None, Permissions(network=["a.cz"]))["added"] == {"network": ["a.cz"]}
